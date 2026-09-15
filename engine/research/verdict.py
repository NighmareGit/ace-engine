"""Research verdict generation: researcher persona prompt, BeeLlama call, marker parsing.

Mirrors engine/generator.py generate_code(): transport use, session-log capture,
dataclass shape. Deterministic parsing only — no LLM/network at import time.
"""

import logging
import re
import time
from dataclasses import dataclass

log = logging.getLogger("engine.research.verdict")


@dataclass
class CitationRecord:
    """A single claim->source citation edge (T02 contract)."""
    claim_id: str
    source_ids: list[str]
    span: str | None = None
    confidence: float = 0.0


@dataclass
class GeneratedVerdict:
    """Result of research verdict generation from BeeLlama."""
    verdict_text: str
    verdict_path: str
    claims: list[str]
    sources: list[str]
    citations: list[CitationRecord]
    raw_response: str
    tokens: int = 0
    thinking_tokens: int = 0
    latency_ms: float = 0.0
    role: str = "researcher"
    finish_reason: str = ""
    exhausted: bool = False
    max_tokens_used: int = 0


class VerdictGenerator:
    """Generate a cited research verdict via BeeLlama (T02).

    Mirrors engine/generator.py generate_code(): build prompt, call
    transport.curl_beellama(), record session log, parse markers.
    """

    def __init__(self, config, transport):
        self.config = config
        self.transport = transport

    def _build_prompt(self, context, task, error_ctx=None) -> str:
        """Build researcher persona prompt. FORBIDS triple-backticks in content."""
        question = getattr(task, "description", "") or getattr(task, "title", "") or ""
        title = getattr(task, "title", "") or getattr(task, "id", "research")

        prompt = (
            f"# Research Task\n\n"
            f"## Question\n{question}\n\n"
            f"## Title\n{title}\n\n"
            f"## Instructions\n\n"
            f"You are a rigorous researcher. Produce a cited verdict answering "
            f"the question above. Follow these rules EXACTLY — the output is "
            f"parsed by a deterministic validator. Deviation = rejection.\n\n"
            f"### Required sections (ALL 5 are mandatory, in this order)\n\n"
            f"1. `## Verdict` — MUST contain exactly these two lines:\n"
            f"   `Position: supported` (or `refuted` or `inconclusive`)\n"
            f"   `Confidence: 0.XX` (a float 0.0-1.0)\n"
            f"   Followed by 2-4 sentences of reasoning.\n\n"
            f"2. `## Sources` — one bullet per source:\n"
            f"   `- [src-1] type: file uri: file://path title: Title text`\n"
            f"   `- [src-2] type: file uri: file://path title: Title text`\n\n"
            f"3. `## Claims` — one bullet per claim, each citing sources inline:\n"
            f"   `- [claim-1] Claim text here [src-1][src-2]`\n"
            f"   `- [claim-2] Another claim [src-1]`\n\n"
            f"4. `## Contradictions` — one bullet per contradiction with a "
            f"   `resolution:` marker, or a single line `None identified.`\n\n"
            f"5. `## Citations` — one line per claim-source edge:\n"
            f"   `[claim:1, src:1, \"evidence span text\", 0.XX]`\n"
            f"   `[claim:2, src:2, \"evidence span text\", 0.XX]`\n\n"
            f"### Concrete example (copy this structure exactly)\n\n"
            f"    ## Verdict\n\n"
            f"    Position: supported\n"
            f"    Confidence: 0.85\n"
            f"    The port should track preview-v0.4.7 because it captures the latest\n"
            f"    expert-path work while the local merge-base lags at v0.4.6.\n\n"
            f"    ## Sources\n\n"
            f"    - [src-1] type: file uri: dogfood-sources/upstream-drift.log title: Drift Log\n"
            f"    - [src-2] type: file uri: dogfood-sources/upstream-drift-stat.txt title: Drift Stat\n\n"
            f"    ## Claims\n\n"
            f"    - [claim-1] Upstream has drifted 220 commits from merge-base [src-1]\n"
            f"    - [claim-2] llama-graph.cpp drifted 310 changed lines [src-2]\n\n"
            f"    ## Contradictions\n\n"
            f"    - between claims 1 and 2 resolution: complementary — commit count and line count measure different aspects of drift.\n\n"
            f"    ## Citations\n\n"
            f"    [claim:1, src:1, \"220 commits between merge-base and main\", 0.9]\n"
            f"    [claim:2, src:2, \"310 changed lines in llama-graph.cpp\", 0.85]\n\n"
            f"CRITICAL RULES:\n"
            f"- Do NOT use triple-backticks (```) anywhere in your "
            f"response. Output plain markdown only. Code snippets must use "
            f"single-backtick inline spans, never fenced blocks.\n"
            f"- NEVER write literal `[claim-N]`, `[src-N]`, `[claim:N]`, or "
            f"`[src:M]` — ALWAYS use REAL numbers like `[claim-1]`, `[src-1]`, "
            f"`[claim:1, src:1, ...]`. The letters N and M are NOT valid markers.\n"
            f"- You MUST include ALL 5 sections. Missing any section = rejection.\n"
        )

        if error_ctx is not None:
            errs = getattr(error_ctx, "validation_errors", []) or []
            if errs:
                prompt += "\n## Previous attempt feedback\n"
                prompt += "A previous verdict was rejected. Address these issues:\n"
                for err in errs:
                    prompt += f"- {err}\n"
        return prompt

    @staticmethod
    def _extract_verdict(raw_response: str) -> str:
        """Take the largest fenced block if present; else raw minus fence lines.

        NOTE: This extraction rewards disobedience. The researcher persona
        prompt (``_build_prompt``) explicitly forbids triple-backtick fences,
        yet if the model disobeys and emits them, this method extracts the
        largest fenced block — effectively privileging fenced content over
        the plain markdown the prompt asked for. Behavior is left unchanged
        (the generator must not crash on fenced responses), but this is
        documented as a known contract tension.
        """
        blocks = re.findall(r'```[^\n]*\n(.*?)```', raw_response, re.DOTALL)
        if blocks:
            return max(blocks, key=len).strip()
        lines = raw_response.split("\n")
        cleaned = [ln for ln in lines if not re.match(r'^```', ln.strip())]
        return "\n".join(cleaned).strip()

    @staticmethod
    def _parse_markers(verdict_text: str) -> tuple[list[str], list[str], list[CitationRecord]]:
        """Parse [claim-N]/[src-N] markers into CitationRecords.

        Malformed markers skipped with logging.warning; never crashes.
        """
        claim_pat = re.compile(r'\[claim-(\d+)\]')
        src_pat = re.compile(r'\[src-(\d+)\]')

        claims = []
        sources = []
        for m in claim_pat.finditer(verdict_text):
            cid = f"claim-{m.group(1)}"
            if cid not in claims:
                claims.append(cid)
        for m in src_pat.finditer(verdict_text):
            sid = f"src-{m.group(1)}"
            if sid not in sources:
                sources.append(sid)

        # Build edges: for each claim, find sources on same line or next line.
        edge_map: dict[str, list[str]] = {}
        lines = verdict_text.split("\n")
        for i, line in enumerate(lines):
            for cm in claim_pat.finditer(line):
                cid = f"claim-{cm.group(1)}"
                sids = [f"src-{s.group(1)}" for s in src_pat.finditer(line, cm.end())]
                if not sids and i + 1 < len(lines):
                    sids = [f"src-{s.group(1)}" for s in src_pat.finditer(lines[i + 1])]
                if sids:
                    edge_map.setdefault(cid, [])
                    for s in sids:
                        if s not in edge_map[cid]:
                            edge_map[cid].append(s)

        # Explicit "Sources: [src-1], [src-2]" lines.
        for m in re.finditer(r'Sources:\s*((?:\[src-\d+\](?:\s*,\s*)?)+)',
                             verdict_text, re.IGNORECASE):
            sids = [f"src-{s}" for s in src_pat.findall(m.group(1))]
            prev = claim_pat.findall(verdict_text[:m.start()])
            if prev:
                cid = f"claim-{prev[-1]}"
                edge_map.setdefault(cid, [])
                for s in sids:
                    if s not in edge_map[cid]:
                        edge_map[cid].append(s)

        citations = []
        for cid, sids in edge_map.items():
            if not sids:
                continue
            span = None
            cm = re.search(rf'\[{re.escape(cid)}\]', verdict_text)
            if cm:
                chunk = verdict_text[cm.end():cm.end() + 400]
                end = re.search(r'\n\s*\n|\[claim-', chunk)
                span = chunk[:end.start()].strip() if end else chunk.strip()
                if len(span) > 200:
                    span = span[:197] + "..."
            citations.append(CitationRecord(claim_id=cid, source_ids=sids, span=span))

        # Warn on malformed markers (non-numeric id).
        for bad in re.findall(r'\[claim-[^\]]*[^\d\]][^\]]*\]', verdict_text):
            log.warning("Skipping malformed citation marker: %r", bad)
        for bad in re.findall(r'\[src-[^\]]*[^\d\]][^\]]*\]', verdict_text):
            log.warning("Skipping malformed citation marker: %r", bad)

        return claims, sources, citations

    def generate(self, context, task, error_ctx=None,
                 max_tokens_override=None, retrieved=None) -> GeneratedVerdict:
        """Generate a cited research verdict. Mirrors generate_code() structure.

        G4-T02 row 7 (DESIGN-G4 §2): *retrieved* is an optional list of
        ``engine.retrieval.prompt_assemble.Chunk`` objects.  When provided
        (research handler CONTEXT stage, ``enable_retrieval=True``),
        ``assemble_retrieved()`` injects them into the prompt with untrusted
        markers.  Additive — existing callers are unaffected when omitted.
        """
        from engine import task_role as _task_role

        prompt_text = self._build_prompt(context, task, error_ctx)

        # G4-T02: retrieval injection seam (DESIGN-G4 §2 row 7 / §3 flag B).
        # Only fires when the research handler passes retrieved chunks (which it
        # only does when enable_retrieval=True).  Uses assemble_retrieved() —
        # the G2 prompt-injection seam — so markers + budget are handled there.
        if retrieved:
            from engine.retrieval.prompt_assemble import assemble_retrieved
            atom_type = "research"
            budget = 1500  # assemble_retrieved looks up RETRIEVAL_BUDGETS by default
            prompt_text = assemble_retrieved(prompt_text, retrieved, budget, atom_type)

        port = 8080 if self.config.model_config.startswith("3090") else 8082
        role = _task_role(task)
        max_tokens = (
            max_tokens_override if max_tokens_override is not None
            else int(getattr(self.config, "max_tokens_by_role", {}).get(role, 8192)))

        messages = [{"role": "user", "content": prompt_text}]
        start = time.time()
        response = self.transport.curl_beellama(
            port, messages, max_tokens=max_tokens, temperature=0.3)
        latency_ms = (time.time() - start) * 1000

        raw_response = response.get("content", "") or ""
        tokens = response.get("total_tokens", 0)
        thinking_tokens = response.get("thinking_tokens", 0)
        finish_reason = response.get("finish_reason", "")

        # --- Session log capture (mirrors generator.py lines ~124-146) ---
        if getattr(self.config, "session_log_recorder", None) is not None:
            try:
                recorder = self.config.session_log_recorder
                _attempt = (error_ctx.attempt_number if error_ctx is not None else 1)
                recorder.record(
                    task_id=task.id, attempt=_attempt, stage="generate",
                    model=self.config.model_config, port=port,
                    prompt_text=prompt_text, response=response,
                    latency_ms=latency_ms, role=role, max_tokens_used=max_tokens)
            except Exception as _sl_exc:  # noqa: BLE001
                log.warning("session_log record failed: %s", _sl_exc)

        verdict_text = self._extract_verdict(raw_response)
        claims, sources, citations = self._parse_markers(verdict_text)

        # Reasoning exhaustion detection (mirror generator.py).
        exhausted = False
        if not raw_response.strip():
            has_reasoning = bool(response.get("reasoning_content")
                                 or response.get("thinking_tokens", 0))
            if has_reasoning and finish_reason in ("length", "", None):
                exhausted = True

        verdict_path = getattr(task, "module", "") or "verdict.md"
        if not verdict_path.endswith(".md"):
            verdict_path = "verdict.md"

        return GeneratedVerdict(
            verdict_text=verdict_text, verdict_path=verdict_path,
            claims=claims, sources=sources, citations=citations,
            raw_response=raw_response, tokens=tokens,
            thinking_tokens=thinking_tokens, latency_ms=latency_ms,
            role=role, finish_reason=finish_reason, exhausted=exhausted,
            max_tokens_used=max_tokens)
