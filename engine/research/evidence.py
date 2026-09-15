"""Staged EvidenceValidator for research verdicts (T04, GRILL-S4 deltas).

Mirrors ``engine/validator.py``'s stage pattern: short-circuit between
stages, report-all within a stage. Six deterministic stages:

  0 empty            — verdict_text non-empty
  1 structure        — required sections (## Verdict with position + confidence,
                        ## Sources, ## Claims, ## Contradictions)
  2 citation format  — every [claim-N]/[src-N] marker parses; ids unique
  3 claim coverage   — every cited claim has >=1 source edge (no orphans)
  4 sources          — no orphan sources, no self-support, NO CYCLES in
                        claim->source AND source->source graphs, claim density
                        >= 2/1000 tokens, contradictions carry resolutions
  5 answer-question  — TF-IDF cosine >= 0.15 between task question and the
                        ## Verdict section (local ~15-line implementation;
                        deliberately does NOT import engine.intent.similarity).

Pure Python — no LLM, no network. The deterministic floor for the
two-layer evidence gate (EPIC.md, ADR-0004).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# Shared negation-pair heuristic (reused by engine/memory/comparator.py)
# ---------------------------------------------------------------------------

_NEG_PAIRS = [("always", "never"), ("is", "is not"),
              ("increases", "decreases"), ("true", "false")]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CitationRecord:
    """A single claim->source citation edge (T02/T04 contract)."""
    claim_id: str
    source_ids: List[str]
    span: str = ""
    confidence: float = 0.0


@dataclass
class EvidenceResult:
    """Result of the staged EvidenceValidator.

    ``checks`` holds one dict per executed stage (short-circuit means
    fewer than 6 when an early stage fails). ``passed`` is True iff
    every executed stage passed.
    """
    passed: bool
    checks: List[dict] = field(default_factory=list)
    orphan_claims: List[str] = field(default_factory=list)
    orphan_sources: List[str] = field(default_factory=list)
    self_support: List[str] = field(default_factory=list)
    cycles: List[str] = field(default_factory=list)
    contradiction_count: int = 0
    verdict_position: str = ""
    verdict_confidence: float = 0.0
    citations: List[CitationRecord] = field(default_factory=list)
    claims: List[dict] = field(default_factory=list)
    sources: List[dict] = field(default_factory=list)
    contradictions: List[dict] = field(default_factory=list)
    _verdict_text: str = ""


# ---------------------------------------------------------------------------
# Marker + section parsing (shared by stages)
# ---------------------------------------------------------------------------

# [claim-N] / [src-N] / [source-N] markers.
_CLAIM_MARKER_RE = re.compile(r'\[claim-(\d+)\]', re.IGNORECASE)
_SRC_MARKER_RE = re.compile(r'\[src-(\d+)\]', re.IGNORECASE)
# A citation span: [claim-N, src-M, "span text", 0.8] or [N, M, span, conf]
_CITATION_LINE_RE = re.compile(
    r'\[\s*claim\s*:\s*(\d+)\s*,\s*src\s*:\s*(\d+)\s*,'
    r'\s*("[^"]*"|\'[^\']*\'|\S+)\s*,\s*([\d.]+)\s*\]',
    re.IGNORECASE,
)
# Verdict position line.
_POSITION_RE = re.compile(
    r'(?:Position|Verdict)\s*[:\-]?\s*(supported|refuted|inconclusive)',
    re.IGNORECASE,
)
_CONFIDENCE_RE = re.compile(r'Confidence\s*[:\-]?\s*([\d.]+)', re.IGNORECASE)


def _extract_section(text: str, header: str) -> str:
    """Content between ``## Header`` and the next ``##`` or EOF."""
    # Capture everything after the header line up to the next ``##`` header
    # or EOF. The key$ anchor (not bare $) prevents the non-greedy match
    # from stopping at an interior blank line.
    pattern = rf'(?:^|\n)##\s+{re.escape(header)}\s*\n(.*?)(?=\n##\s|\Z)'
    m = re.search(pattern, text, re.DOTALL)
    return m.group(1).strip() if m else ""


def _parse_markers(text: str) -> Tuple[List[CitationRecord], List[dict], List[dict]]:
    """Parse [claim-N]/[src-N] markers and citation lines from text.

    Returns (citations, claims, sources). Claims and sources are the
    distinct ids referenced; CitationRecord carries the explicit edges.
    """
    claims: Dict[str, dict] = {}
    sources: Dict[str, dict] = {}
    citations: List[CitationRecord] = []

    # Parse claim lines from the Claims section to capture claim text.
    claims_section = _extract_section(text, "Claims")
    if claims_section:
        for m in re.finditer(
                r'(?:^|\n)\s*[-*]?\s*\[claim-(\d+)\]\s*(.*?)(?=\n\s*[-*]?\s*\[claim-|\Z)',
                claims_section, re.DOTALL):
            cid = m.group(1)
            raw_text = m.group(2).strip()
            # Strip any trailing [src-N] markers from the display text.
            clean_text = re.sub(r'\[src-\d+\]', '', raw_text).strip()
            if cid not in claims:
                claims[cid] = {"id": cid, "text": clean_text, "source_ids": []}
            # Infer source_ids from [src-N] markers in the claim line.
            for sm in _SRC_MARKER_RE.finditer(raw_text):
                sid = sm.group(1)
                if sid not in claims[cid]["source_ids"]:
                    claims[cid]["source_ids"].append(sid)

    # Parse source ids from the Sources section.
    sources_section = _extract_section(text, "Sources")
    if sources_section:
        for m in re.finditer(r'\[src-(\d+)\]', sources_section, re.IGNORECASE):
            sid = m.group(1)
            if sid not in sources:
                sources[sid] = {"id": sid}

    # Fallback: if Claims/Sources sections weren't parseable, scan full text.
    if not claims:
        for m in _CLAIM_MARKER_RE.finditer(text):
            cid = m.group(1)
            if cid not in claims:
                claims[cid] = {"id": cid, "text": "", "source_ids": []}
    if not sources:
        for m in _SRC_MARKER_RE.finditer(text):
            sid = m.group(1)
            if sid not in sources:
                sources[sid] = {"id": sid}

    for m in _CITATION_LINE_RE.finditer(text):
        claim_id, source_id, span_raw, conf = m.group(1), m.group(2), m.group(3), float(m.group(4))
        span = span_raw.strip('\'"')
        if claim_id in claims and source_id not in claims[claim_id]["source_ids"]:
            claims[claim_id]["source_ids"].append(source_id)
        citations.append(CitationRecord(
            claim_id=claim_id,
            source_ids=[source_id],
            span=span,
            confidence=conf,
        ))

    return citations, list(claims.values()), list(sources.values())


# ---------------------------------------------------------------------------
# Graph cycle detection (stage 4)
# ---------------------------------------------------------------------------

def _has_cycle_dfs(adj: Dict[str, List[str]]) -> Tuple[bool, List[str]]:
    """DFS cycle detection on a directed graph. Returns (has_cycle, path)."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: Dict[str, int] = {n: WHITE for n in adj}
    parent: Dict[str, Optional[str]] = {n: None for n in adj}

    def dfs(u: str) -> Optional[List[str]]:
        color[u] = GRAY
        for v in adj.get(u, []):
            if v not in color:
                color[v] = WHITE
                parent[v] = u
            if color[v] == GRAY:
                # Reconstruct cycle.
                cycle = [v, u]
                node = u
                while parent.get(node) is not None and parent[node] != v:
                    node = parent[node]  # type: ignore[assignment]
                    cycle.append(node)
                cycle.append(v)
                return list(reversed(cycle))
            if color[v] == WHITE:
                res = dfs(v)
                if res is not None:
                    return res
        color[u] = BLACK
        return None

    for node in adj:
        if color[node] == WHITE:
            res = dfs(node)
            if res is not None:
                return True, res
    return False, []


# ---------------------------------------------------------------------------
# Local TF-IDF cosine (stage 5) — ~15 lines, no sklearn import
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> List[str]:
    """Lowercase word tokens."""
    return re.findall(r'[a-z0-9]+', text.lower())


def _tfidf_cosine(a: str, b: str) -> float:
    """Local TF-IDF cosine similarity (unigram, IDF from the 2-doc corpus).

    Deliberately standalone (does NOT import engine.intent.similarity) to
    avoid cross-package import. Returns 0.0 on empty / disjoint input.
    """
    tokens_a, tokens_b = _tokenize(a), _tokenize(b)
    if not tokens_a or not tokens_b:
        return 0.0
    vocab = set(tokens_a) | set(tokens_b)
    if not vocab:
        return 0.0
    # Document frequency per term (0, 1, or 2).
    df: Dict[str, int] = {}
    for t in vocab:
        df[t] = (1 if t in tokens_a else 0) + (1 if t in tokens_b else 0)
    idf = {t: math.log((2 + 1) / (d + 1)) + 1 for t, d in df.items()}

    def tf_vec(tokens: List[str]) -> Dict[str, float]:
        tf: Dict[str, float] = {}
        for t in tokens:
            tf[t] = tf.get(t, 0.0) + 1.0
        total = len(tokens) or 1.0
        return {t: c / total for t, c in tf.items()}

    va, vb = tf_vec(tokens_a), tf_vec(tokens_b)
    # TF-IDF vectors.
    va_tfidf = {t: va.get(t, 0.0) * idf.get(t, 0.0) for t in vocab}
    vb_tfidf = {t: vb.get(t, 0.0) * idf.get(t, 0.0) for t in vocab}
    dot = sum(va_tfidf[t] * vb_tfidf[t] for t in vocab)
    na = math.sqrt(sum(v * v for v in va_tfidf.values())) or 1.0
    nb = math.sqrt(sum(v * v for v in vb_tfidf.values())) or 1.0
    return max(0.0, min(1.0, dot / (na * nb)))


# ---------------------------------------------------------------------------
# EvidenceValidator
# ---------------------------------------------------------------------------

# Acceptable verdict positions (anti-mush, GRILL-S4 delta 1).
_VERDICT_POSITIONS = ("supported", "refuted", "inconclusive")
# Minimum claim density (GRILL-S4 delta 5).
_MIN_CLAIM_DENSITY = 2.0 / 1000.0
# Minimum TF-IDF cosine for answer-the-question (GRILL-S4 delta 6).
_MIN_TFIDF_COSINE = 0.15


class EvidenceValidator:
    """Staged deterministic validator for research verdict documents.

    ``validate(verdict_md, question='')`` runs stages 0-5 and returns
    an ``EvidenceResult``. Short-circuits on the first failing stage
    (mirrors ``engine/validator.py``).
    """

    def validate(self, verdict_md: str, question: str = "") -> EvidenceResult:
        """Run the staged validation pipeline.

        Args:
            verdict_md: The full verdict markdown text (markers included).
            question: The research question (used by stage 5 TF-IDF check).

        Returns:
            EvidenceResult with ``passed``, ``checks``, and parsed artifacts.
        """
        result = EvidenceResult(passed=True, checks=[])
        text = verdict_md or ""
        # Store the raw verdict text so downstream consumers (source
        # verification) can parse file:// URIs from the Sources section.
        result._verdict_text = text

        # Stage 0: empty.
        if not self._stage_empty(text, result):
            return result

        # Stage 1: structure.
        if not self._stage_structure(text, result):
            return result

        # Stage 2: citation format.
        citations, claims, sources = self._stage_citations(text, result)
        if not result.checks[-1]["passed"]:
            return result

        # Stage 3: claim coverage.
        if not self._stage_claim_coverage(citations, claims, sources, result):
            return result

        # Stage 4: sources (orphans, self-support, cycles, density, contradictions).
        self._stage_sources(citations, claims, sources, text, result)
        if not result.checks[-1]["passed"]:
            return result

        # Stage 5: answer-the-question.
        self._stage_answer_question(text, question, result)

        return result

    # -- Stage 0 -----------------------------------------------------------

    def _stage_empty(self, text: str, result: EvidenceResult) -> bool:
        passed = bool(text and text.strip())
        result.checks.append({"stage": 0, "name": "empty", "passed": passed,
                              "error": None if passed else "verdict text is empty"})
        if not passed:
            result.passed = False
        return passed

    # -- Stage 1 -----------------------------------------------------------

    def _stage_structure(self, text: str, result: EvidenceResult) -> bool:
        errors: List[str] = []
        verdict_section = _extract_section(text, "Verdict")
        if not verdict_section:
            errors.append("missing '## Verdict' section")
        else:
            pos_m = _POSITION_RE.search(verdict_section)
            if not pos_m:
                errors.append("## Verdict missing Position: supported|refuted|inconclusive")
            else:
                result.verdict_position = pos_m.group(1).lower()
                if result.verdict_position not in _VERDICT_POSITIONS:
                    errors.append(f"invalid verdict position '{result.verdict_position}'")
            conf_m = _CONFIDENCE_RE.search(verdict_section)
            if not conf_m:
                errors.append("## Verdict missing Confidence: <float>")
            else:
                try:
                    result.verdict_confidence = float(conf_m.group(1))
                except ValueError:
                    errors.append(f"invalid confidence value '{conf_m.group(1)}'")
        for header in ("Sources", "Claims", "Contradictions"):
            if not _extract_section(text, header):
                errors.append(f"missing '## {header}' section")
        passed = not errors
        result.checks.append({"stage": 1, "name": "structure", "passed": passed,
                              "error": "; ".join(errors) if errors else None})
        if not passed:
            result.passed = False
        return passed

    # -- Stage 2 -----------------------------------------------------------

    def _stage_citations(self, text: str, result: EvidenceResult
                         ) -> Tuple[List[CitationRecord], List[dict], List[dict]]:
        errors: List[str] = []
        citations, claims, sources = _parse_markers(text)
        result.citations = citations
        result.claims = claims
        result.sources = sources

        # Detect malformed [claim-]/[src-] markers: things that look like a
        # claim/src marker (start with [claim or [src) but are NOT well-formed
        # [claim-N] / [src-N]. Citation lines like [claim:1, src:1, ...] are
        # valid and must NOT be flagged here.
        raw_markers = re.findall(r'\[(?:claim|src)[^\]]*\]', text, re.IGNORECASE)
        bad = []
        for m in raw_markers:
            # Well-formed marker: [claim-N] or [src-N].
            if re.match(r'\[(?:claim|src)-\d+\]', m, re.IGNORECASE):
                continue
            # Well-formed citation line: [claim:N, src:M, span, conf].
            if re.match(r'\[claim:\d+,\s*src:\d+', m, re.IGNORECASE):
                continue
            bad.append(m)
        if bad:
            errors.append(f"malformed markers (skipped with warning): {bad[:5]}")

        # Claim ids must be unique (dict key guarantees this); source ids too.
        # Verify citation-line claim/source ids reference known markers.
        known_claims = {c["id"] for c in claims}
        known_sources = {s["id"] for s in sources}
        for cit in citations:
            if cit.claim_id not in known_claims:
                errors.append(f"citation references unknown claim-{cit.claim_id}")
            for sid in cit.source_ids:
                if sid not in known_sources:
                    errors.append(f"citation references unknown src-{sid}")

        passed = not errors
        result.checks.append({"stage": 2, "name": "citation_format", "passed": passed,
                              "error": "; ".join(errors) if errors else None})
        if not passed:
            result.passed = False
        return citations, claims, sources

    # -- Stage 3 -----------------------------------------------------------

    def _stage_claim_coverage(self, citations: List[CitationRecord],
                              claims: List[dict], sources: List[dict],
                              result: EvidenceResult) -> bool:
        errors: List[str] = []
        cited_claims: Set[str] = set()
        for cit in citations:
            cited_claims.add(cit.claim_id)
        # A claim is "covered" if it has >=1 source edge (from citation lines
        # or inferred source_ids).
        covered: Set[str] = set()
        for cit in citations:
            covered.add(cit.claim_id)
        for claim in claims:
            if claim.get("source_ids"):
                covered.add(claim["id"])
        orphan = [c["id"] for c in claims if c["id"] not in covered]
        result.orphan_claims = orphan
        if orphan:
            errors.append(f"orphan claims (no source edge): {orphan}")
        passed = not errors
        result.checks.append({"stage": 3, "name": "claim_coverage", "passed": passed,
                              "error": "; ".join(errors) if errors else None})
        if not passed:
            result.passed = False
        return passed

    # -- Stage 4 -----------------------------------------------------------

    def _stage_sources(self, citations: List[CitationRecord],
                       claims: List[dict], sources: List[dict],
                       text: str, result: EvidenceResult) -> bool:
        errors: List[str] = []

        # Build claim->source adjacency from citations + inferred edges.
        cited_sources: Set[str] = set()
        claim_src_adj: Dict[str, List[str]] = {}
        for cit in citations:
            claim_src_adj.setdefault(cit.claim_id, []).extend(cit.source_ids)
            cited_sources.update(cit.source_ids)
        for claim in claims:
            for sid in claim.get("source_ids", []):
                claim_src_adj.setdefault(claim["id"], []).append(sid)
                cited_sources.add(sid)

        # Orphan sources: sources never referenced by any citation.
        known_sources = {s["id"] for s in sources}
        orphan_src = sorted(known_sources - cited_sources)
        result.orphan_sources = orphan_src
        if orphan_src:
            errors.append(f"orphan sources (never cited): {orphan_src}")

        # Self-support: a claim that supports itself with itself. Since claims
        # and sources share a numeric namespace (claim-1 citing src-1 is
        # legitimate), we detect self-support only when a citation's evidence
        # span IS the claim's own text verbatim (the claim cites itself as
        # evidence).  Self-support is when the span text equals the claim text
        # after whitespace + case normalization — NOT a character-length ratio
        # (which produces false positives on legitimate restating spans that
        # happen to be similar length).
        self_sup: List[str] = []
        for cit in citations:
            if cit.claim_id in cit.source_ids and cit.span.strip():
                # Find the claim text for this claim id.
                ctext = ""
                for c in claims:
                    if c["id"] == cit.claim_id:
                        ctext = c.get("text", "")
                        break
                span_norm = " ".join(cit.span.lower().split())
                text_norm = " ".join(ctext.lower().split())
                # Self-support ONLY when the span IS the claim's own text
                # verbatim (after whitespace + case normalization).
                if span_norm and text_norm and span_norm == text_norm:
                    self_sup.append(cit.claim_id)
        result.self_support = self_sup
        if self_sup:
            errors.append(f"self-support (claim cites itself): {self_sup}")

        # Cycle check: circular citation structures.
        #
        # A bipartite claim->source graph (edges only from claim nodes to
        # source nodes) can NEVER cycle because edges never return to the
        # claim partition.  The real semantic detects circular citation
        # structures: claim A cites source S, and source S is defined as
        # (or references) another claim B, and claim B cites source S'
        # which references claim A, etc.
        #
        # We build a unified graph over claim-nodes AND source-nodes:
        #   claim->source edges: from citations (as before)
        #   source->claim edges: a source node connects BACK to any claim
        #       it is cited BY when the source's uri/text itself references
        #       a claim-id (e.g. source uri = "claim-2", or source text
        #       contains "[claim-3]").  This captures the case where a
        #       "source" is really just another claim reference.
        #
        # A cycle in this unified graph means a circular citation chain.
        #
        # Additionally, the source->source cycle check (citation rings)
        # is retained below.

        # Build unified node set (claims + sources).
        uni_nodes: Set[str] = set()
        for cit in citations:
            uni_nodes.add(f"c{cit.claim_id}")
            for sid in cit.source_ids:
                uni_nodes.add(f"s{sid}")
        # Also add any source ids that appear in the sources list.
        for s in sources:
            uni_nodes.add(f"s{s['id']}")
        for c in claims:
            uni_nodes.add(f"c{c['id']}")

        uni_adj: Dict[str, List[str]] = {n: [] for n in uni_nodes}

        # claim->source edges from citations.
        for cit in citations:
            for sid in cit.source_ids:
                uni_adj.setdefault(f"c{cit.claim_id}", []).append(f"s{sid}")

        # source->claim edges: a source connects back to any claim whose id
        # is explicitly referenced in the source's uri/text/body (the source
        # aliases or is defined as another claim).  Parse the Sources section
        # for "uri:" / "text:" fields and inline [claim-N] references.
        # NOTE: we do NOT create edges merely because src-N and claim-N share
        # a numeric id — that shared namespace is legitimate (claim-1 citing
        # src-1 is normal).  The edge is only created when the source body
        # explicitly references a claim marker.
        sources_section = _extract_section(text, "Sources")
        if sources_section:
            for src_match in re.finditer(
                    r'\[src-(\d+)\]\s*(.*)', sources_section, re.DOTALL):
                sid = src_match.group(1)
                src_body = src_match.group(2)
                # Extract uri/text fields from the source definition.
                uri_match = re.search(r'uri\s*:\s*(\S+)', src_body, re.IGNORECASE)
                text_match = re.search(
                    r'(?:text|span|description)\s*:\s*["\']?([^"\n]+)',
                    src_body, re.IGNORECASE)
                uri_val = uri_match.group(1) if uri_match else ""
                text_val = text_match.group(1).strip() if text_match else ""
                combined = f"{uri_val} {text_val} {src_body}"
                # Find any [claim-N] references in the source body.
                for cm in _CLAIM_MARKER_RE.finditer(combined):
                    cid = cm.group(1)
                    uni_adj.setdefault(f"s{sid}", []).append(f"c{cid}")
                # Also detect bare "claim-N" references (without brackets)
                # in the source body, e.g. "uri: claim-2".
                for cm in re.finditer(r'\bclaim-(\d+)\b', combined, re.IGNORECASE):
                    cid = cm.group(1)
                    uni_adj.setdefault(f"s{sid}", []).append(f"c{cid}")

        has_cycle, cycle_path = _has_cycle_dfs(uni_adj)
        if has_cycle:
            result.cycles.append("circular_citation:" + "->".join(cycle_path))
            errors.append(
                f"circular citation structure: {'->'.join(cycle_path)}")

        # Cycle check: source->source graph (citation rings, GRILL-S4 delta 3).
        # Build from sources that cite other sources (heuristic: sources section
        # may list "cites: [src-M]" or sources appear in each other's spans).
        ss_adj: Dict[str, List[str]] = {f"s{s['id']}": [] for s in sources}
        if sources_section:
            # Match "cites/references/see [src-N]" or "[src-N] ... [src-M]".
            for m in re.finditer(
                    r'\[src-(\d+)\].*?(?:cites|references|see)\W+\[src-(\d+)\]',
                    sources_section, re.IGNORECASE | re.DOTALL):
                a, b = m.group(1), m.group(2)
                ss_adj.setdefault(f"s{a}", []).append(f"s{b}")
        has_ss_cycle, ss_path = _has_cycle_dfs(ss_adj)
        if has_ss_cycle:
            result.cycles.append("source->source:" + "->".join(ss_path))
            errors.append(f"cycle in source->source graph: {'->'.join(ss_path)}")

        # Non-empty evidence spans (GRILL-S4 delta 2): every citation must
        # have a non-empty span (after strip).  An empty span means the
        # citation provides no verifiable evidence.
        for cit in citations:
            if not cit.span or not cit.span.strip():
                errors.append(
                    f"empty_span: claim-{cit.claim_id} cites "
                    f"src-{','.join(cit.source_ids)} with empty evidence span")

        # Claim density >= 2/1000 tokens (GRILL-S4 delta 5).
        tokens = re.findall(r'\S+', text)
        n_tokens = len(tokens)
        n_claims = len(claims)
        if n_tokens > 0:
            density = n_claims / n_tokens
            if density < _MIN_CLAIM_DENSITY:
                errors.append(
                    f"claim density {density:.4f} < {_MIN_CLAIM_DENSITY:.4f} "
                    f"({n_claims} claims / {n_tokens} tokens)")

        # Contradictions must carry resolutions (GRILL-S4 delta 3 / Q3).
        contradictions_section = _extract_section(text, "Contradictions")
        result.contradiction_count = 0
        if contradictions_section.strip():
            # Count contradiction entries (lines starting with - or * or numbered).
            entries = re.findall(r'(?:^|\n)\s*(?:[-*]|\d+[.)])\s+(.+)', contradictions_section)
            result.contradiction_count = len(entries)
            # Boilerplate patterns that do NOT count as real resolutions.
            _BOILERPLATE_RE = re.compile(
                r'^(?:none|n/a|na|unknown|tbd|see above|\W)*$', re.IGNORECASE)
            for entry in entries:
                # A resolution is present if the entry contains a resolution marker.
                has_marker = bool(re.search(
                    r'(?:resolution|resolved|explanation)\s*[:\-]', entry,
                    re.IGNORECASE))
                # Even with a marker, the resolution content must be non-empty
                # and not just boilerplate (anti-gaming).  Extract the content
                # after the marker and verify it has real text.
                if has_marker:
                    after = re.split(
                        r'(?:resolution|resolved|explanation)\s*[:\-]',
                        entry, maxsplit=1, flags=re.IGNORECASE)[-1].strip()
                    if (not after
                            or _BOILERPLATE_RE.match(after)
                            or len(after) < 10):
                        errors.append(
                            f"contradiction resolution is boilerplate/empty: "
                            f"{entry[:80]!r}")
                else:
                    errors.append(
                        f"contradiction without resolution: {entry[:80]!r}")

        # Silent-contradiction heuristic: if the Contradictions section is
        # empty but two claims use obvious negation pairs (always/never,
        # is/is not) about a shared keyword, the contradiction is silently
        # ignored -> deterministic FAIL. This is a heuristic floor; the LLM
        # judge's contradiction_handling dimension catches subtler cases.
        if not result.contradiction_count and len(claims) >= 2:
            claim_texts_lower = [c.get("text", "").lower() for c in claims]
            found_silent = False
            for i in range(len(claim_texts_lower)):
                if found_silent:
                    break
                for j in range(i + 1, len(claim_texts_lower)):
                    ti, tj = claim_texts_lower[i], claim_texts_lower[j]
                    for pos, neg in _NEG_PAIRS:
                        if (pos in ti and neg in tj) or (neg in ti and pos in tj):
                            words_i = set(re.findall(r'[a-z]+', ti)) - {pos, neg}
                            words_j = set(re.findall(r'[a-z]+', tj)) - {pos, neg}
                            if words_i & words_j:
                                errors.append(
                                    "silent contradiction: claims appear to "
                                    "contradict but Contradictions section "
                                    "is empty")
                                found_silent = True
                                break

        passed = not errors
        result.checks.append({"stage": 4, "name": "sources", "passed": passed,
                              "error": "; ".join(errors) if errors else None})
        if not passed:
            result.passed = False
        return passed

    # -- Stage 5 -----------------------------------------------------------

    def _stage_answer_question(self, text: str, question: str,
                               result: EvidenceResult) -> bool:
        verdict_section = _extract_section(text, "Verdict")
        q = question or ""
        if not q.strip():
            # No question supplied — skip (pass by default).
            result.checks.append({"stage": 5, "name": "answer_question", "passed": True,
                                  "error": None, "note": "no question supplied"})
            return True
        if not verdict_section:
            result.checks.append({"stage": 5, "name": "answer_question", "passed": False,
                                  "error": "no ## Verdict section to compare"})
            result.passed = False
            return False
        sim = _tfidf_cosine(q, verdict_section)
        passed = sim >= _MIN_TFIDF_COSINE
        info = f"TF-IDF cosine={sim:.4f} threshold={_MIN_TFIDF_COSINE}"
        result.checks.append({"stage": 5, "name": "answer_question", "passed": passed,
                              "error": None if passed else info, "cosine": round(sim, 4)})
        if not passed:
            result.passed = False
        return passed
