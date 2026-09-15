"""Tool-Gap Detector (TGD) — mechanical gap detection over engine.db (ORCH-7).

Scans engine.db telemetry and emits a structured GapReport when the engine
repeatedly hits signals that indicate a MISSING capability. Pure SQL + regex:
no LLM in the detection path, no mutation of runs/tasks/code.

Two entry points:
  * scan_run(run_id)   — cheap post-run scan of a single run's telemetry
  * scan_history()      — standalone scan over all runs

Every Finding carries concrete evidence rows; the detector never invents gaps.
G7/G9/G10 are IIL/skills-gated and degrade gracefully to empty results.
"""

import json
import re
import sqlite3
from datetime import datetime

from engine.state import _get_conn

from engine.orchestrator.toolgap_types import (
    EvidenceRow, Finding, GapReport, Severity, SignalId, TriageCategory,
    GATED_SIGNALS,
)

# ---------------------------------------------------------------------------
# Tuning constants (spec §signal table)
# ---------------------------------------------------------------------------
G1_REPEAT_THRESHOLD = 3          # same (stage, error_class) >= N times
G4_CONTEXT_BLOAT_FRACTION = 0.9  # prompt_tokens >= 90% of cap
G5_LOW_SCORE_THRESHOLD = 4       # sub-score <= this = "low" (scale 0-10)
G5_CLUSTER_MIN_TASKS = 2         # >= this many tasks low on one dimension

# Substrings marking a transient error (mirrors trigger.py classification).
_TRANSIENT_RE = re.compile(
    r"timed?\s*out|timeout|out\s*of\s*memory|oom|50[0-4]|"
    r"connection\s*(error|refused)|temporary\s*failure",
    re.IGNORECASE,
)

# G2: re-plan reasons that mention missing info / context.
_G2_MISSING_INFO_RE = re.compile(
    r"missing\s+(info|information|context|data|access|tool)|"
    r"lacks?\s+(info|context|data|access)|"
    r"no\s+(access|visibility|data)\s+to",
    re.IGNORECASE,
)

# G11: failures citing external references the sandbox cannot reach.
_G11_EXTERNAL_REF_RE = re.compile(
    r"https?://[^\s'\"]+|"
    r"(?:pypi|npm|crates|rubygems|github|gitlab)\.(?:com|io|org)/[^\s'\"]+|"
    r"(?:package|module|library|dependency)\s+['\"]?([a-z][\w\-.]*)['\"]?"
    r"\s+(?:not\s+found|unavailable|install\s+failed|404)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sig_key(stage: str, error_class: str) -> str:
    """Normalized (stage, error_class) signature string."""
    return f"{stage}:{error_class}"


def _classify_error(stage: str, error_msg: str) -> str:
    """Map (stage, raw error) -> normalized error class (mirrors trigger.py)."""
    msg = (error_msg or "").lower()
    if _TRANSIENT_RE.search(msg):
        return "transient"
    _STAGE_CLASS = {
        "empty": "empty", "ast": "syntax", "syntax": "syntax",
        "imports": "import", "execution": "exec",
    }
    return _STAGE_CLASS.get(stage, stage or "unknown")


def _validation_error_class(validation_result: str | None) -> tuple[str, str]:
    """Extract (stage, error_class) from a validation_result JSON blob.

    The validator stores stages as [{stage, file, passed, error}, ...]. We
    return the first failing stage's (stage, normalized error_class).
    """
    if not validation_result:
        return ("unknown", "unknown")
    try:
        stages = json.loads(validation_result)
    except (json.JSONDecodeError, TypeError):
        return ("unknown", "unknown")
    for s in stages:
        if not s.get("passed"):
            stage = s.get("stage", "unknown")
            err = s.get("error", "")
            return (stage, _classify_error(stage, err))
    return ("unknown", "unknown")


# ---------------------------------------------------------------------------
# Signal extractors — each returns a list of Finding (possibly empty)
# ---------------------------------------------------------------------------

def _extract_g1(run_id: str | None, conn) -> list[Finding]:
    """G1: same (stage, error_class) signature >= N times across tasks/runs.

    Source: engine_task_results.validation_result (failed validation stages).
    Aggregates across all runs when run_id is None, else scoped to one run.
    """
    if run_id:
        rows = conn.execute(
            "SELECT run_id, task_id, validation_result FROM engine_task_results "
            "WHERE run_id = ? AND validation_result IS NOT NULL",
            (run_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT run_id, task_id, validation_result FROM engine_task_results "
            "WHERE validation_result IS NOT NULL",
        ).fetchall()
    # Count signatures.
    sig_counts: dict[str, list] = {}  # sig -> [(run_id, task_id), ...]
    for r in rows:
        stage, eclass = _validation_error_class(r["validation_result"])
        if eclass == "transient":
            continue
        key = _sig_key(stage, eclass)
        sig_counts.setdefault(key, []).append((r["run_id"], r["task_id"]))
    findings = []
    for key, occurrences in sig_counts.items():
        if len(occurrences) >= G1_REPEAT_THRESHOLD:
            stage, eclass = key.split(":", 1)
            evidence = [
                EvidenceRow(run_id=rid, task_id=tid, signature=key,
                            count=len(occurrences))
                for rid, tid in occurrences
            ]
            # Deduplicate evidence rows per (run_id, task_id) keeping one row
            # per occurrence but capped for report size.
            evidence = evidence[:50]
            confidence = min(1.0, len(occurrences) / (G1_REPEAT_THRESHOLD * 2))
            findings.append(Finding(
                signal=SignalId.G1_REPEATED_FAILURE_SIGNATURE,
                severity=Severity.HIGH if len(occurrences) >= 6 else Severity.MEDIUM,
                confidence=round(confidence, 2),
                triage=TriageCategory.VALIDATOR_REPAIR,
                message=(
                    f"Repeated failure signature '{key}' observed "
                    f"{len(occurrences)}x (>= {G1_REPEAT_THRESHOLD}) — "
                    f"a kind of failure nothing repairs."),
                evidence=evidence,
            ))
    return findings


def _extract_g2(run_id: str | None, conn) -> list[Finding]:
    """G2: re-plan / escalation reasons mentioning missing info/context.

    Source: orchestrator_replans.trigger_reason. The re-plan brain logs the
    trigger reason for every attempt; reasons mentioning missing info/context
    indicate the brain lacked data — a read/grep/telemetry tool gap.

    DELTA: task_escalation events are emitted fire-and-forget (engine/events.py)
    and are NOT persisted to a table. We read the persisted re-plan telemetry
    instead, which captures the same "brain lacked data" signal via the
    trigger_reason field. This is a strict subset of the full escalation
    stream — see report for the gap.
    """
    if run_id:
        rows = conn.execute(
            "SELECT run_id, task_id, trigger_reason FROM orchestrator_replans "
            "WHERE run_id = ? AND trigger_reason IS NOT NULL",
            (run_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT run_id, task_id, trigger_reason FROM orchestrator_replans "
            "WHERE trigger_reason IS NOT NULL",
        ).fetchall()
    findings = []
    matches = []
    for r in rows:
        reason = r["trigger_reason"] or ""
        if _G2_MISSING_INFO_RE.search(reason):
            matches.append((r["run_id"], r["task_id"], reason))
    if matches:
        evidence = [
            EvidenceRow(run_id=rid, task_id=tid,
                        signature="missing_info_in_reason", detail=reason[:200])
            for rid, tid, reason in matches
        ]
        confidence = min(1.0, len(matches) / 3.0)
        findings.append(Finding(
            signal=SignalId.G2_ESCALATION_MISSING_INFO,
            severity=Severity.MEDIUM,
            confidence=round(confidence, 2),
            triage=TriageCategory.READ_RETRIEVAL_TOOL,
            message=(
                f"{len(matches)} re-plan(s) cited missing info/context — "
                f"the brain lacked data (read/grep/telemetry tool gap)."),
            evidence=evidence[:50],
        ))
    return findings


def _extract_g3(run_id: str | None, conn) -> list[Finding]:
    """G3: retry exhaustion -> FAIL, with no re-plan recovery.

    Source: engine_task_results (state=FAILED, attempts high) +
    orchestrator_replans (whether a re-plan was attempted). A task that fails
    after exhausting retries AND never got a successful re-plan is beyond the
    current capability set.
    """
    if run_id:
        failed = conn.execute(
            "SELECT run_id, task_id, attempts, error_message FROM engine_task_results "
            "WHERE run_id = ? AND state = 'FAILED' AND attempts >= 2",
            (run_id,),
        ).fetchall()
    else:
        failed = conn.execute(
            "SELECT run_id, task_id, attempts, error_message FROM engine_task_results "
            "WHERE state = 'FAILED' AND attempts >= 2",
        ).fetchall()
    findings = []
    exhausted = []
    for r in failed:
        # Check whether a successful re-plan happened for this task.
        replans = conn.execute(
            "SELECT applied FROM orchestrator_replans "
            "WHERE run_id = ? AND task_id = ? AND applied = 1",
            (r["run_id"], r["task_id"]),
        ).fetchall()
        if not list(replans):
            exhausted.append(r)
    if exhausted:
        evidence = [
            EvidenceRow(
                run_id=r["run_id"], task_id=r["task_id"],
                signature="retry_exhaustion_no_replan",
                count=r["attempts"],
                detail=(r["error_message"] or "")[:200],
            )
            for r in exhausted
        ]
        confidence = min(1.0, len(exhausted) / 3.0)
        findings.append(Finding(
            signal=SignalId.G3_RETRY_EXHAUSTION,
            severity=Severity.HIGH,
            confidence=round(confidence, 2),
            triage=TriageCategory.NEW_CAPABILITY,
            message=(
                f"{len(exhausted)} task(s) exhausted retries and failed with "
                f"no re-plan recovery — beyond current capability set."),
            evidence=evidence[:50],
        ))
    return findings


def _extract_g4(run_id: str | None, conn) -> list[Finding]:
    """G4: context bloat — prompt_tokens near the model's context cap.

    OBS-05: Upgraded from the code-blob proxy (large generated_code) to real
    ``prompt_tokens`` read from ``session_logs``.  Eliminates false positives
    from large generated files.

    A task triggers G4 when its max prompt_tokens across attempts >= 90% of
    the model's context cap.  If ``session_logs`` has no rows for the run,
    the signal degrades gracefully to [] (no finding).
    """
    # Model context caps (prompt_tokens threshold = ~90% of a 16K cap).
    G4_THRESHOLD_TOKENS = 14_400
    if run_id:
        rows = conn.execute(
            """\
            SELECT task_id, MAX(prompt_tokens) AS max_pt, model
            FROM session_logs
            WHERE run_id = ?
            GROUP BY task_id
            HAVING max_pt >= ?
            """,
            (run_id, G4_THRESHOLD_TOKENS),
        ).fetchall()
    else:
        rows = conn.execute(
            """\
            SELECT run_id, task_id, MAX(prompt_tokens) AS max_pt, model
            FROM session_logs
            GROUP BY run_id, task_id
            HAVING max_pt >= ?
            """,
            (G4_THRESHOLD_TOKENS,),
        ).fetchall()
    if not rows:
        return []
    evidence = [
        EvidenceRow(run_id=r["run_id"] if "run_id" in r.keys() else run_id,
                    task_id=r["task_id"],
                    signature="high_prompt_tokens", count=r["max_pt"])
        for r in rows
    ]
    confidence = min(1.0, len(rows) / 3.0)
    return [Finding(
        signal=SignalId.G4_CONTEXT_BLOAT,
        severity=Severity.HIGH,
        confidence=round(confidence, 2),
        triage=TriageCategory.READ_RETRIEVAL_TOOL,
        message=(
            f"{len(rows)} task(s) with prompt_tokens >= {G4_THRESHOLD_TOKENS} "
            f"(near model context cap) — real prompt_tokens from session_logs."),
        evidence=evidence[:50],
    )]


def _extract_g5(run_id: str | None, conn) -> list[Finding]:
    """G5: judge low sub-scores clustered on one dimension.

    Source: engine_scores (5 dims: completeness, correctness, quality,
    intelligence, role_fit). When >= G5_CLUSTER_MIN_TASKS tasks score <=
    G5_LOW_SCORE_THRESHOLD on the same dimension, the work is consistently
    weak on that axis — a productivity vs correctness gap.
    """
    dims = ["completeness", "correctness", "quality", "intelligence", "role_fit"]
    findings = []
    if run_id:
        scope_rows = conn.execute(
            "SELECT DISTINCT run_id FROM engine_scores WHERE run_id = ?",
            (run_id,),
        ).fetchall()
    else:
        scope_rows = conn.execute(
            "SELECT DISTINCT run_id FROM engine_scores",
        ).fetchall()
    # Group scores by run for per-run clustering.
    for run_row in scope_rows:
        rid = run_row["run_id"]
        scores = conn.execute(
            "SELECT task_id, completeness, correctness, quality, "
            "intelligence, role_fit FROM engine_scores WHERE run_id = ?",
            (rid,),
        ).fetchall()
        for dim in dims:
            low = []
            for s in scores:
                val = s[dim]
                if val is not None and val <= G5_LOW_SCORE_THRESHOLD:
                    low.append((s["task_id"], val))
            if len(low) >= G5_CLUSTER_MIN_TASKS:
                evidence = [
                    EvidenceRow(run_id=rid, task_id=tid,
                                signature=f"low_{dim}", count=val)
                    for tid, val in low
                ]
                confidence = min(1.0, len(low) / (G5_CLUSTER_MIN_TASKS * 2))
                findings.append(Finding(
                    signal=SignalId.G5_JUDGE_LOW_SUBCLUSTER,
                    severity=Severity.MEDIUM,
                    confidence=round(confidence, 2),
                    triage=TriageCategory.PRODUCTIVITY_TOOL,
                    message=(
                        f"Low '{dim}' scores (<= {G5_LOW_SCORE_THRESHOLD}) "
                        f"clustered on {len(low)} tasks in run {rid} — "
                        f"systematic weakness on this dimension."),
                    evidence=evidence[:50],
                ))
    return findings


def _extract_g6(run_id: str | None, conn) -> list[Finding]:
    """G6: dependency_allowlist additions requested via re-plan patches.

    Source: orchestrator_replans.raw_response (the patch JSON). Patches with
    dependency_allowlist_additions indicate the environment lacked deps the
    task needed — a dependency/tooling gap.
    """
    if run_id:
        rows = conn.execute(
            "SELECT run_id, task_id, raw_response FROM orchestrator_replans "
            "WHERE run_id = ? AND raw_response IS NOT NULL",
            (run_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT run_id, task_id, raw_response FROM orchestrator_replans "
            "WHERE raw_response IS NOT NULL",
        ).fetchall()
    matches = []
    for r in rows:
        try:
            patch = json.loads(r["raw_response"] or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        spec = patch.get("spec_patch") or {}
        additions = spec.get("dependency_allowlist_additions") or []
        if additions:
            matches.append((r["run_id"], r["task_id"], additions))
    if not matches:
        return []
    evidence = [
        EvidenceRow(run_id=rid, task_id=tid,
                    signature="allowlist_addition",
                    detail=str(additions)[:200])
        for rid, tid, additions in matches
    ]
    confidence = min(1.0, len(matches) / 3.0)
    return [Finding(
        signal=SignalId.G6_ALLOWLIST_ADDITION,
        severity=Severity.MEDIUM,
        confidence=round(confidence, 2),
        triage=TriageCategory.DEPENDENCY_TOOLING,
        message=(
            f"{len(matches)} re-plan patch(es) requested "
            f"dependency_allowlist_additions — environment lacked deps."),
        evidence=evidence[:50],
    )]


def _extract_g7(run_id: str | None, conn) -> list[Finding]:
    """G7: IIL router "uncertain" verdicts (below cosine threshold).

    Reads the additive ``intent_events`` table the IIL pipeline writes: rows
    WHERE uncertain=1 are router verdicts that fell below the cosine threshold
    or gap and were escalated to the native lane. A cluster of uncertain
    verdicts indicates the route table is degrading (feeds G4 drift alarm).

    Degrades gracefully: if the intent_events table doesn't exist (pre-IIL or
    IIL disabled), returns [].
    """
    # Does the IIL telemetry table exist?
    table_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='intent_events'"
    ).fetchone()
    if not table_exists:
        return []
    try:
        if run_id:
            rows = conn.execute(
                "SELECT run_id, task_id, action, confidence, uncertain "
                "FROM intent_events WHERE run_id = ? AND uncertain = 1",
                (run_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT run_id, task_id, action, confidence, uncertain "
                "FROM intent_events WHERE uncertain = 1",
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    if not rows:
        return []
    evidence = [
        EvidenceRow(run_id=r["run_id"], task_id=r["task_id"],
                    signature="router_uncertain", count=1,
                    detail=f"action={r['action']} conf={r['confidence']}")
        for r in rows
    ]
    confidence = min(1.0, len(evidence) / 3.0)
    return [Finding(
        signal=SignalId.G7_ROUTER_UNCERTAIN,
        severity=Severity.MEDIUM,
        confidence=round(confidence, 2),
        triage=TriageCategory.REGISTRY_ENTRY,
        message=(
            f"{len(evidence)} router verdict(s) uncertain (below threshold/gap) "
            f"— route table may be degrading (feeds G4 drift alarm)."),
        evidence=evidence[:50],
    )]


def _extract_g8(run_id: str | None, conn) -> list[Finding]:
    """G8: human interventions — stop-file, aborts, manual merges.

    Source: engine_runs (state=CANCELLED with stop-file signatures in
    error_message) + orchestrator_session (status). The spec says G8 is
    ALWAYS reported regardless of threshold — it is the strongest signal.

    DELTA: we can detect stop-file cancellations (error_message contains
    "cancelled by stop-file" / "stop-file") and manual aborts, but we cannot
    distinguish a manual merge of run branches from normal operation via
    existing telemetry. We report what we can observe.
    """
    STOPFILE_RE = re.compile(
        r"cancelled\s+by\s+stop[\-\s]?file|stop[\-\s]?file|manual\s+abort",
        re.IGNORECASE,
    )
    if run_id:
        rows = conn.execute(
            "SELECT id, state, error_message FROM engine_runs "
            "WHERE id = ? AND state = 'CANCELLED'",
            (run_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, state, error_message FROM engine_runs "
            "WHERE state = 'CANCELLED'",
        ).fetchall()
    matches = []
    for r in rows:
        msg = r["error_message"] or ""
        if STOPFILE_RE.search(msg):
            matches.append((r["id"], msg))
    if not matches:
        return []
    evidence = [
        EvidenceRow(run_id=rid, task_id=None,
                    signature="human_intervention", detail=msg[:200])
        for rid, msg in matches
    ]
    return [Finding(
        signal=SignalId.G8_HUMAN_INTERVENTION,
        severity=Severity.ALWAYS,
        confidence=1.0,
        triage=TriageCategory.HUMAN_INVESTIGATE,
        message=(
            f"{len(matches)} run(s) cancelled via stop-file/human abort — "
            f"the strongest signal of a gap the owner patched by hand."),
        evidence=evidence[:50],
    )]


def _extract_g9(run_id: str | None, conn) -> list[Finding]:
    """G9: skill-consult lookups that MISS (load_skill(name) no-hit).

    Reads the additive ``skill_lookups`` table the IIL skills tier writes:
    rows WHERE hit=0 are lookups for skill names that have no doc. A cluster
    of misses indicates a skill the brain needs but hasn't been documented.

    Degrades gracefully: if the skill_lookups table doesn't exist, returns [].
    """
    table_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='skill_lookups'"
    ).fetchone()
    if not table_exists:
        return []
    try:
        rows = conn.execute(
            "SELECT skill_id, hit FROM skill_lookups WHERE hit = 0"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    if not rows:
        return []
    evidence = [
        EvidenceRow(run_id=None, task_id=None,
                    signature="skill_lookup_miss", count=1,
                    detail=f"skill_id={r['skill_id']}")
        for r in rows
    ]
    confidence = min(1.0, len(evidence) / 3.0)
    return [Finding(
        signal=SignalId.G9_SKILL_LOOKUP_MISS,
        severity=Severity.MEDIUM,
        confidence=round(confidence, 2),
        triage=TriageCategory.SKILL_DOC,
        message=(
            f"{len(evidence)} skill-lookup miss(es) — skills the brain "
            f"consulted but has no doc for."),
        evidence=evidence[:50],
    )]


def _extract_g10(run_id: str | None, conn) -> list[Finding]:
    """G10: IIL logs a tool call for a tool NOT in the registry.

    Reads the additive ``intent_events`` table for rows whose action is not
    in the IIL tool registry (engine.intent.tools). These are hallucinated
    tool calls the model emitted but the registry doesn't know — the
    hallucination guard rejected them. A cluster indicates the model is
    drifting from the advertised tool surface.

    Degrades gracefully: if the intent_events table doesn't exist or the IIL
    registry can't be imported, returns [].
    """
    table_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='intent_events'"
    ).fetchone()
    if not table_exists:
        return []
    # Import the registry's known actions (graceful if IIL not installed).
    try:
        from engine.intent.tools import ToolRegistry
        known = set(ToolRegistry().actions)
    except Exception:  # noqa: BLE001
        return []
    try:
        if run_id:
            rows = conn.execute(
                "SELECT run_id, task_id, action, lane FROM intent_events "
                "WHERE run_id = ? AND action IS NOT NULL AND action != ''",
                (run_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT run_id, task_id, action, lane FROM intent_events "
                "WHERE action IS NOT NULL AND action != ''",
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    # Filter to actions NOT in the registry (hallucinations).
    hallucinated = [r for r in rows if r["action"] not in known]
    if not hallucinated:
        return []
    evidence = [
        EvidenceRow(run_id=r["run_id"], task_id=r["task_id"],
                    signature="hallucinated_tool_call", count=1,
                    detail=f"action={r['action']} lane={r['lane']}")
        for r in hallucinated
    ]
    confidence = min(1.0, len(evidence) / 3.0)
    return [Finding(
        signal=SignalId.G10_HALLUCINATED_TOOL_CALL,
        severity=Severity.HIGH,
        confidence=round(confidence, 2),
        triage=TriageCategory.REGISTRY_ENTRY,
        message=(
            f"{len(evidence)} tool call(s) for actions not in the registry "
            f"— model is hallucinating tool names (guard rejected them)."),
        evidence=evidence[:50],
    )]


def _extract_g11(run_id: str | None, conn) -> list[Finding]:
    """G11: failures citing external references the sandbox cannot reach.

    Source: engine_task_results.error_message. Regex matches URLs and
    package-install failures — tasks whose scope assumes egress capability
    that isn't wired.
    """
    if run_id:
        rows = conn.execute(
            "SELECT run_id, task_id, error_message FROM engine_task_results "
            "WHERE run_id = ? AND error_message IS NOT NULL",
            (run_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT run_id, task_id, error_message FROM engine_task_results "
            "WHERE error_message IS NOT NULL",
        ).fetchall()
    matches = []
    for r in rows:
        msg = r["error_message"] or ""
        if _G11_EXTERNAL_REF_RE.search(msg):
            matches.append((r["run_id"], r["task_id"], msg))
    if not matches:
        return []
    evidence = [
        EvidenceRow(run_id=rid, task_id=tid,
                    signature="external_reference_failure", detail=msg[:200])
        for rid, tid, msg in matches
    ]
    confidence = min(1.0, len(matches) / 3.0)
    return [Finding(
        signal=SignalId.G11_EXTERNAL_REFERENCE_FAILURE,
        severity=Severity.LOW,
        confidence=round(confidence, 2),
        triage=TriageCategory.EGRESS_TOOL,
        message=(
            f"{len(matches)} task(s) failed citing external references "
            f"(URLs/packages) the sandbox cannot reach — egress gap."),
        evidence=evidence[:50],
    )]


# ---------------------------------------------------------------------------
# Extractor registry
# ---------------------------------------------------------------------------

_EXTRACTORS = {
    SignalId.G1_REPEATED_FAILURE_SIGNATURE: _extract_g1,
    SignalId.G2_ESCALATION_MISSING_INFO: _extract_g2,
    SignalId.G3_RETRY_EXHAUSTION: _extract_g3,
    SignalId.G4_CONTEXT_BLOAT: _extract_g4,
    SignalId.G5_JUDGE_LOW_SUBCLUSTER: _extract_g5,
    SignalId.G6_ALLOWLIST_ADDITION: _extract_g6,
    SignalId.G7_ROUTER_UNCERTAIN: _extract_g7,
    SignalId.G8_HUMAN_INTERVENTION: _extract_g8,
    SignalId.G9_SKILL_LOOKUP_MISS: _extract_g9,
    SignalId.G10_HALLUCINATED_TOOL_CALL: _extract_g10,
    SignalId.G11_EXTERNAL_REFERENCE_FAILURE: _extract_g11,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scan_run(run_id: str) -> GapReport:
    """Scan a single run's telemetry. Cheap, same-process (post-run hook)."""
    conn = _get_conn()
    all_findings: list[Finding] = []
    for sig, fn in _EXTRACTORS.items():
        try:
            all_findings.extend(fn(run_id, conn))
        except Exception:  # noqa: BLE001 — a broken signal must not kill the scan
            pass
    conn.close()
    return GapReport(
        run_id=run_id,
        scanned_runs=1,
        findings=all_findings,
        generated_at=datetime.now().isoformat(),
    )


def scan_history() -> GapReport:
    """Standalone scan over ALL runs in engine.db."""
    conn = _get_conn()
    rows = conn.execute("SELECT COUNT(*) AS n FROM engine_runs").fetchall()
    n_runs = rows[0]["n"] if rows else 0
    all_findings: list[Finding] = []
    for sig, fn in _EXTRACTORS.items():
        try:
            all_findings.extend(fn(None, conn))
        except Exception:  # noqa: BLE001
            pass
    conn.close()
    return GapReport(
        run_id=None,
        scanned_runs=n_runs,
        findings=all_findings,
        generated_at=datetime.now().isoformat(),
    )
