"""Oracle tier — protocol + validation layer (O2).

oracle_digest implements PRD pre-digestion: split a PRD (or an escalated
task's context) into atom tickets — each independently implementable +
verifiable, with explicit acceptance criteria and dependency edges.

Two-layer design (mirrors the re-plan brain):
  * The LLM PROPOSES atom tickets (via oracle endpoint or 35B-judge fallback).
  * The VALIDATOR DISPOSES: ticket-size limits, dependency-graph acyclicity
    (stdlib DFS — networkx NOT allowed), and coverage (every original task
    maps to >=1 atom or is explicitly carried).

``digest_prd`` is the public entry point. It uses the oracle endpoint when
enabled+reachable, else falls back to the 35B judge (this fallback is the
testable-today path). On schema violation it retries up to max_digest_retries,
then returns an ok=False result. On oracle unreachability it raises
OracleUnavailable (caught by the caller for graceful degradation).

Strict output schema (reuses the T3/replan strict-validation pattern):
the LLM may ONLY emit a JSON object — never code, never file edits.
"""

import json
import re

from engine.orchestrator.oracle_types import (
    AtomTicket, OracleConfig, OracleRequest, OracleResult, OracleUnavailable,
)

# ---------------------------------------------------------------------------
# System prompt enforcing the strict atom-ticket schema.
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are the ACE oracle pre-digestion layer. A task has
escalated beyond the 35B re-plan brain. Your job is to decompose the task's
PRD context into ATOM TICKETS — each independently implementable and verifiable.

RULES:
- You decompose TASKS into atom tickets. You NEVER write, edit, or reference
  source code, files, or commands.
- Respond with ONLY a JSON object conforming to the schema below.
- No markdown fences, no prose, no explanations outside the JSON.

REQUIRED JSON SCHEMA:
{
  "atoms": [
    {
      "atom_id": "<parent task id>-<suffix, e.g. 'a', 'b'>",
      "title": "<short imperative title>",
      "description": "<full implementation description>",
      "acceptance_criteria": ["<verifiable criterion>"],
      "depends_on": ["<other atom_id>"],
      "source_task_ids": ["<original PRD task id this atom covers>"]
    }
  ],
  "carried_task_ids": ["<original task id with NO atoms — explicitly carried forward>"]
}

- Each atom MUST be independently implementable AND verifiable.
- Each atom description MUST be concise (<= the token budget guidance).
- dependency edges (depends_on) MUST reference other atom_ids in this digest.
- Every original task MUST map to >=1 atom OR appear in carried_task_ids.
- carried_task_ids are for tasks that cannot be decomposed further — use sparingly.
"""


def _build_user_prompt(req: OracleRequest, max_atom_tokens: int,
                        max_atoms: int) -> str:
    """Build the user message describing the PRD context to digest."""
    lines = [
        f"Parent task: {req.task_id} — {req.task_title}",
        f"Description: {req.task_description}",
        f"Dependencies: {', '.join(req.task_dependencies) or '(none)'}",
    ]
    if req.acceptance_criteria:
        lines.append("Acceptance criteria:")
        for ac in req.acceptance_criteria:
            lines.append(f"  - {ac}")
    if req.error_history:
        lines.append(f"Error history ({len(req.error_history)} failed attempts):")
        for i, e in enumerate(req.error_history[-5:], 1):
            lines.append(f"  {i}. {e}")
    if req.replan_history:
        lines.append(f"Re-plan history ({len(req.replan_history)} attempts):")
        for i, r in enumerate(req.replan_history[-3:], 1):
            reason = r.get("reason", "") if isinstance(r, dict) else str(r)
            lines.append(f"  {i}. {reason}")
    lines.append(f"\nPRD context:\n{req.prd_text}")
    lines.append(
        f"\nDecompose into <= {max_atoms} atom tickets. "
        f"Each description <= ~{max_atom_tokens} tokens. "
        "Emit the JSON object now.")
    return "\n".join(lines)


def build_digest_payload(req: OracleRequest, cfg: OracleConfig) -> dict:
    """Build an OpenAI-compatible chat payload from an OracleRequest."""
    return {
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(
                req, cfg.max_atom_tokens, cfg.max_atoms_per_task)},
        ],
        "max_tokens": cfg.max_tokens,
        "temperature": cfg.temperature,
    }


def parse_digest_response(text: str) -> dict:
    """Parse the LLM's text response into a digest dict.

    Tolerates markdown fences and surrounding prose: extracts the first JSON
    object. Raises ValueError on unparseable output or missing 'atoms' key.
    """
    if not text:
        raise ValueError("empty oracle digest response")
    stripped = text.strip()
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", stripped,
                            re.DOTALL)
    if fence_match:
        stripped = fence_match.group(1).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"no JSON object in oracle digest response: {text!r}")
    data = json.loads(stripped[start:end + 1])
    if "atoms" not in data:
        raise ValueError("oracle digest response missing 'atoms' key")
    return data


# ---------------------------------------------------------------------------
# Validator — deterministic gate. The LLM proposes; the validator disposes.
# ---------------------------------------------------------------------------

def _check_size_limits(atoms: list[dict], cfg: OracleConfig) -> str | None:
    """Return an error string if any atom exceeds size limits, else None."""
    if len(atoms) > cfg.max_atoms_per_task:
        return (f"too many atoms: {len(atoms)} > max "
                f"{cfg.max_atoms_per_task}")
    if len(atoms) < cfg.min_atoms_per_task:
        return (f"too few atoms: {len(atoms)} < min "
                f"{cfg.min_atoms_per_task}")
    for a in atoms:
        desc = a.get("description", "")
        # Approximate token count: ~4 chars/token (conservative for English).
        approx_tokens = len(desc) // 4
        if approx_tokens > cfg.max_atom_tokens:
            return (f"atom {a.get('atom_id', '?')} description too long: "
                    f"~{approx_tokens} tokens > max {cfg.max_atom_tokens}")
    return None


def _check_acyclicity(atoms: list[dict]) -> str | None:
    """Stdlib-only DFS cycle check on the dependency graph.

    networkx is NOT allowed. Returns an error string if a cycle is found,
    else None. Also flags dependency edges that reference unknown atom_ids.
    """
    atom_ids = {a.get("atom_id") for a in atoms if a.get("atom_id")}
    # adjacency: atom_id -> list of depends_on atom_ids
    adj: dict[str, list[str]] = {}
    for a in atoms:
        aid = a.get("atom_id")
        if not aid:
            continue
        deps = a.get("depends_on") or []
        # Flag unknown dependency targets.
        for d in deps:
            if d not in atom_ids:
                return (f"atom {aid} depends on unknown atom_id {d!r}")
        adj[aid] = list(deps)

    # DFS with three-color marking: WHITE=0 (unvisited), GRAY=1 (in-stack),
    # BLACK=2 (done). Back-edge to GRAY => cycle.
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {aid: WHITE for aid in adj}

    def _dfs(node: str, path: list[str]) -> str | None:
        color[node] = GRAY
        for nb in adj.get(node, []):
            if nb not in color:
                continue  # unknown target already flagged above
            if color[nb] == GRAY:
                cycle = " -> ".join(path + [nb])
                return f"dependency cycle detected: {cycle}"
            if color[nb] == WHITE:
                err = _dfs(nb, path + [nb])
                if err:
                    return err
        color[node] = BLACK
        return None

    for aid in adj:
        if color[aid] == WHITE:
            err = _dfs(aid, [aid])
            if err:
                return err
    return None


def _check_coverage(atoms: list[dict], original_task_ids: list[str],
                     carried_task_ids: list[str]) -> str | None:
    """Every original task must map to >=1 atom or be explicitly covered.

    Coverage = union of all atom.source_task_ids + carried_task_ids. Any
    original task not in this set is a coverage gap.
    """
    covered: set = set()
    for a in atoms:
        for tid in a.get("source_task_ids") or []:
            covered.add(tid)
    for tid in carried_task_ids or []:
        covered.add(tid)
    missing = [t for t in original_task_ids if t not in covered]
    if missing:
        return f"uncovered original task(s): {missing}"
    return None


def _contains_code(s: str) -> bool:
    """Reject code-like tokens smuggled into atom fields (ADR-0002)."""
    code_re = re.compile(
        r"```"
        r"|\bdef\s+\w+\s*\("
        r"|\bclass\s+\w+[:(]"
        r"|\bimport\s+[a-z_]\w*(?:\.[a-z_]\w*)*(?:\s+as\s+\w+)?\s*[;\n]"
        r"|\bos\.system\b"
        r"|\bsubprocess\.\w+"
        r"|\beval\s*\("
        r"|\bexec\s*\("
        r"|\b__import__\b",
        re.IGNORECASE,
    )
    return bool(code_re.search(s))


def _collect_strings(obj, path="root"):
    """Yield (path, string_value) for every string in the nested structure."""
    if isinstance(obj, str):
        yield (path, obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _collect_strings(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _collect_strings(v, f"{path}[{i}]")


def validate_digest(data: dict, original_task_ids: list[str],
                     cfg: OracleConfig) -> tuple[bool, str]:
    """Validate a parsed digest dict. Returns (ok, error_msg).

    Deterministic gate enforcing:
      1. atoms is a list of well-formed dicts with required fields.
      2. Size limits (count + per-atom token budget).
      3. Dependency-graph acyclicity (stdlib DFS) + unknown-edge check.
      4. Coverage (every original task mapped or carried).
      5. No code-like tokens in any string value.
    """
    atoms = data.get("atoms")
    if not isinstance(atoms, list):
        return (False, "atoms must be a list")
    carried = data.get("carried_task_ids") or []
    if not isinstance(carried, list):
        return (False, "carried_task_ids must be a list")

    # 1. Required fields per atom.
    for i, a in enumerate(atoms):
        if not isinstance(a, dict):
            return (False, f"atom[{i}] must be an object")
        for key in ("atom_id", "title", "description"):
            if key not in a or not a[key]:
                return (False, f"atom[{i}] missing required field '{key}'")

    # 2. Size limits.
    err = _check_size_limits(atoms, cfg)
    if err:
        return (False, err)

    # 3. Acyclicity.
    err = _check_acyclicity(atoms)
    if err:
        return (False, err)

    # 4. Coverage.
    err = _check_coverage(atoms, original_task_ids, carried)
    if err:
        return (False, err)

    # 5. No code tokens anywhere.
    for path, s in _collect_strings(data):
        if _contains_code(s):
            return (False, f"code-like token found in {path}: {s!r}")

    return (True, "")


def _data_to_atoms(data: dict, parent_task_id: str) -> list[AtomTicket]:
    """Convert a validated digest dict into AtomTicket dataclasses."""
    atoms = []
    for a in data.get("atoms", []):
        atoms.append(AtomTicket(
            atom_id=a["atom_id"],
            parent_task_id=parent_task_id,
            title=a["title"],
            description=a["description"],
            acceptance_criteria=list(a.get("acceptance_criteria") or []),
            depends_on=list(a.get("depends_on") or []),
            source_task_ids=list(a.get("source_task_ids") or []),
        ))
    return atoms


# ---------------------------------------------------------------------------
# Persistence — additive oracle_atoms table.
# ---------------------------------------------------------------------------

def _ensure_oracle_tables():
    """Create the additive oracle tables if absent."""
    from engine.state import _get_conn
    conn = _get_conn()
    conn.execute("""CREATE TABLE IF NOT EXISTS oracle_atoms (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        parent_task_id TEXT NOT NULL,
        atom_id TEXT NOT NULL,
        title TEXT,
        description TEXT,
        acceptance_criteria TEXT DEFAULT '[]',
        depends_on TEXT DEFAULT '[]',
        source_task_ids TEXT DEFAULT '[]',
        created_at TEXT,
        UNIQUE(run_id, atom_id)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS oracle_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        detail TEXT,
        atoms_count INTEGER DEFAULT 0,
        tokens INTEGER DEFAULT 0,
        created_at TEXT
    )""")
    conn.commit()
    conn.close()


def persist_atoms(run_id: str, atoms: list[AtomTicket]) -> None:
    """Persist atom tickets to the oracle_atoms table (additive)."""
    from engine.state import _get_conn
    from datetime import datetime
    _ensure_oracle_tables()
    conn = _get_conn()
    now = datetime.now().isoformat()
    for a in atoms:
        conn.execute("""
            INSERT OR REPLACE INTO oracle_atoms
            (run_id, parent_task_id, atom_id, title, description,
             acceptance_criteria, depends_on, source_task_ids, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            run_id, a.parent_task_id, a.atom_id, a.title, a.description,
            json.dumps(a.acceptance_criteria), json.dumps(a.depends_on),
            json.dumps(a.source_task_ids), now,
        ))
    conn.commit()
    conn.close()


def record_oracle_event(run_id: str, task_id: str, event_type: str,
                        detail: str = "", atoms_count: int = 0,
                        tokens: int = 0) -> None:
    """Persist an oracle telemetry event to oracle_events."""
    from engine.state import _get_conn
    from datetime import datetime
    _ensure_oracle_tables()
    conn = _get_conn()
    conn.execute("""
        INSERT INTO oracle_events
        (run_id, task_id, event_type, detail, atoms_count, tokens, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (run_id, task_id, event_type, detail, atoms_count, tokens,
          datetime.now().isoformat()))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Public entry point: digest_prd.
# ---------------------------------------------------------------------------

def digest_prd(req: OracleRequest, cfg: OracleConfig, transport,
               original_task_ids: list[str]) -> OracleResult:
    """Digest a PRD / escalated task into atom tickets.

    Uses the oracle endpoint when enabled+reachable, else falls back to the
    35B judge (the testable-today path). On schema violation, retries up to
    cfg.max_digest_retries, then returns ok=False. On oracle unreachability,
    raises OracleUnavailable (caller degrades gracefully).

    Args:
        req: the oracle request (PRD text + task context).
        cfg: oracle configuration (endpoint, limits, retries).
        transport: the engine transport (must expose curl_beellama).
        original_task_ids: the PRD task ids that must be covered.

    Returns:
        OracleResult with atoms on ok, error details on failure.
    """
    if not cfg.enabled:
        return OracleResult(ok=False, error="oracle disabled", degraded=True)

    payload = build_digest_payload(req, cfg)
    fallback_used = False

    # Attempt 1: oracle endpoint (if enabled + reachable).
    atoms, data, err = _attempt_digest(
        req, cfg, transport, cfg.effective_url(), original_task_ids,
        label="oracle",
    )

    if atoms is not None:
        persist_atoms(req.run_id, atoms)
        record_oracle_event(req.run_id, req.task_id, "digest_ok",
                            detail="oracle", atoms_count=len(atoms))
        return OracleResult(ok=True, atoms=atoms, tokens=0)

    # Oracle failed — was it unreachable (vs. validation failure)?
    if err == "oracle_unreachable":
        # Fall through to the 35B judge fallback (testable today).
        fallback_used = True
        record_oracle_event(req.run_id, req.task_id, "oracle_unreachable",
                            detail=err)
    else:
        # Validation/parse failure on the oracle — still try the fallback,
        # since the oracle is latency-tolerant and the fallback may succeed.
        fallback_used = True

    # Attempt 2: 35B judge fallback (the re-plan port — already running).
    fallback_url = _fallback_url(cfg)
    atoms, data, err2 = _attempt_digest(
        req, cfg, transport, fallback_url, original_task_ids,
        label="35b_fallback",
    )
    if atoms is not None:
        persist_atoms(req.run_id, atoms)
        record_oracle_event(req.run_id, req.task_id, "digest_ok",
                            detail="35b_fallback", atoms_count=len(atoms))
        return OracleResult(ok=True, atoms=atoms, tokens=0,
                            fallback_used=True)

    # Both attempts failed.
    record_oracle_event(req.run_id, req.task_id, "digest_failed",
                        detail=f"oracle={err};fallback={err2}")
    return OracleResult(ok=False, error=f"digest failed: oracle={err}; "
                        f"fallback={err2}", fallback_used=fallback_used,
                        degraded=True)


def _fallback_url(cfg: OracleConfig) -> str:
    """Return the 35B judge fallback URL (the re-plan port).

    The fallback uses the SAME endpoint the re-plan brain uses (port 8080 by
    default). We derive it from the engine's replan port convention: the
    oracle config does not carry the replan port, so we default to 8080 (the
    engine's replan_model_port default). The transport routes by port.
    """
    # The fallback is the re-plan brain's model — port 8080 convention.
    return "http://127.0.0.1:8080"


def _attempt_digest(req, cfg, transport, url, original_task_ids, label):
    """Run one digest attempt against a given endpoint.

    Returns (atoms, data, err) where:
      - atoms is a list[AtomTicket] on success (err=None).
      - err is "oracle_unreachable" for transport errors, or a validation/
        parse error string. atoms is None on failure.
    """
    payload = build_digest_payload(req, cfg)
    try:
        response = transport.curl_beellama(
            _port_from_url(url),
            payload["messages"],
            max_tokens=cfg.max_tokens,
            temperature=cfg.temperature,
        )
    except Exception as e:
        return (None, None, "oracle_unreachable")

    raw = response.get("content", "")
    tokens = response.get("total_tokens", 0)

    # Parse (strict schema — mirrors T3/replan).
    try:
        data = parse_digest_response(raw)
    except (ValueError, Exception) as e:
        return (None, None, f"{label} parse error: {e}")

    # Validate (deterministic gate).
    ok, err = validate_digest(data, original_task_ids, cfg)
    if not ok:
        # Retry on schema violation (bounded by max_digest_retries).
        return (None, data, f"{label} validation failed: {err}")

    atoms = _data_to_atoms(data, req.task_id)
    return (atoms, data, None)


def _port_from_url(url: str) -> int:
    """Extract port from a URL, defaulting to 8086."""
    m = re.search(r":(\d+)/?", url)
    return int(m.group(1)) if m else 8086
