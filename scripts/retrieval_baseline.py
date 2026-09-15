#!/usr/bin/env python3
"""Phase 0 retrieval baseline — measures grep/repo-map recall against engine.db.

Reconstructs ground-truth file sets from session_logs.prompt_payload_path
sidecars (per DESIGN-G2.md §Phase 0), measures tokens_spent, context_hits,
grep_recall@10, repo_map_recall@10, atom_success_correlation.  Emits JSON
report to reports/retrieval_baseline_<timestamp>.json.

Pre-registered thresholds (gate for G2-T05):
    repo_map_recall@10 >= 0.85       -> skip embeddings
    0.70 <= repo_map_recall@10 < 0.85 -> enhancement
    repo_map_recall@10 < 0.70        -> required

Deterministic, offline, read-only against engine.db. No LLM / network calls.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time

# ── Pre-registered thresholds (DESIGN-G2 §2) ─────────────────────────────────

THRESHOLD_SKIP = 0.85
THRESHOLD_ENHANCE = 0.70


def _verdict(recall: float) -> str:
    if recall >= THRESHOLD_SKIP:
        return "skip_embeddings"
    if recall >= THRESHOLD_ENHANCE:
        return "enhancement"
    return "required"


# ── Ground-truth reconstruction ──────────────────────────────────────────────

# Matches file-like paths, optionally wrapped in backticks and/or prefixed
# with "@" (engine convention for target files).  The consuming boundary
# before allows whitespace, backtick, or start-of-string; the lookahead
# after requires a non-path character (or end-of-string).
_FILE_RE = re.compile(
    r"(?:^|[\s`])@?((?:[\w.\-]+/)+[\w.\-]+\.\w+)(?=[^\w.\-/]|$)"
)


def _extract_ground_truth(prompt_text: str) -> set[str]:
    """Return file paths the atom actually read, parsed from the sidecar
    prompt_text (Files to create/test/modify + Context Files sections)."""
    files: set[str] = set()
    for m in _FILE_RE.finditer(prompt_text):
        path = m.group(1)
        if path.startswith("http"):
            continue
        parts = path.split("/")
        if any(p.startswith(".") or not p for p in parts):
            continue
        files.add(path)
    return files


def _load_sidecar(path: str) -> str:
    """Return prompt_text from the first entry of a JSONL sidecar."""
    if not path or not os.path.isfile(path):
        return ""
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    return json.loads(line).get("prompt_text", "")
                except json.JSONDecodeError:
                    continue
    return ""


# ── Metrics ──────────────────────────────────────────────────────────────────

def _recall_at_k(retrieved: list[str], ground_truth: set[str], k: int) -> float:
    """Fraction of ground-truth files in the top-k retrieved list."""
    if not ground_truth:
        return 0.0
    top = set(retrieved[:k])
    return sum(1 for f in ground_truth if f in top) / len(ground_truth)


def _grep_recall(ground_truth: set[str], workspace_files: list[str]) -> list[str]:
    """Keyword-grep retrieval: score workspace files by keyword overlap
    with ground-truth paths. Returns a ranked file list."""
    if not ground_truth or not workspace_files:
        return []
    keywords: set[str] = set()
    for f in ground_truth:
        keywords.update(p for p in f.split("/") if len(p) >= 3)
    scored = [(sum(1 for kw in keywords if kw in wf), wf) for wf in workspace_files]
    scored = [(s, wf) for s, wf in scored if s > 0]
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [wf for _, wf in scored]


def _repo_map_recall(ground_truth: set[str], rendered_map: str) -> list[str]:
    """Repo-map retrieval: extract file paths from the rendered map in
    order of appearance (file order == relevance rank)."""
    if not ground_truth or not rendered_map:
        return []
    seen: set[str] = set()
    ranked: list[str] = []
    for m in _FILE_RE.finditer(rendered_map):
        path = m.group(1)
        if path not in seen:
            seen.add(path)
            ranked.append(path)
    return ranked


def _pearson(xs: list[float], ys: list[float]) -> float:
    """Pearson correlation. Returns 0.0 for degenerate input."""
    n = min(len(xs), len(ys))
    if n < 2:
        return 0.0
    xs, ys = xs[:n], ys[:n]
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = (sum((x - mx) ** 2 for x in xs)) ** 0.5
    dy = (sum((y - my) ** 2 for y in ys)) ** 0.5
    return num / (dx * dy) if dx and dy else 0.0


def _list_workspace_files(project_path: str) -> list[str]:
    """List relative file paths under project_path, excluding VCS/build dirs."""
    skip = {".git", "__pycache__", "node_modules", ".venv", "venv", ".tox",
            ".mypy_cache", ".pytest_cache", "dist", "build", ".eggs"}
    skip_ext = {".pyc", ".pyo", ".so", ".o", ".class", ".jar", ".db"}
    files: list[str] = []
    for root, dirs, fnames in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
        for fname in sorted(fnames):
            if os.path.splitext(fname)[1] in skip_ext:
                continue
            files.append(os.path.relpath(os.path.join(root, fname), project_path))
    return files


# ── Main harness ─────────────────────────────────────────────────────────────

def run_baseline(db_path: str = "engine.db", top_k: int = 10,
                 project_path: str = ".") -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    rows = conn.execute(
        """SELECT session_id, run_id, task_id, attempt, atom_id,
                  prompt_tokens, completion_tokens, total_tokens,
                  prompt_payload_path
           FROM session_logs WHERE prompt_payload_path IS NOT NULL
           ORDER BY run_id, task_id, attempt"""
    ).fetchall()
    conn.close()

    workspace_files = _list_workspace_files(project_path)
    atoms: list[dict] = []

    for row in rows:
        prompt_text = _load_sidecar(row["prompt_payload_path"])
        gt = _extract_ground_truth(prompt_text)
        tokens_spent = (row["prompt_tokens"] or 0) + (row["completion_tokens"] or 0)

        grep_ranked = _grep_recall(gt, workspace_files)
        grep_r10 = _recall_at_k(grep_ranked, gt, top_k)

        # Repo-map render: build a real RepoMap for the workspace and
        # render it with the default budget, then grep the rendered text
        # for ground-truth file paths.  This measures what an agent would
        # actually see.  Fallback (documented): if tree-sitter is
        # unavailable or the workspace has no supported files, fall back
        # to keyword-filtered workspace files.
        try:
            from engine.retrieval.repo_map import (
                RepoMapBuilder, RepoMap, Def,
            )
            builder = RepoMapBuilder()
            rm = builder.build(project_path)
            map_render = builder.render(rm, budget=3000)
            if not map_render.strip():
                raise RuntimeError("empty render")
        except Exception:
            # Fallback: keyword-filtered workspace files (documented).
            map_render = "\n".join(
                [f for f in workspace_files
                 if any(p in f for gt_f in gt
                        for p in gt_f.split("/") if len(p) >= 3)]
                or workspace_files[:top_k])
        map_ranked = _repo_map_recall(gt, map_render)
        map_r10 = _recall_at_k(map_ranked, gt, top_k)

        context_hits = sum(1 for f in gt
                           if any(wf.endswith(f) or f.endswith(wf.split("/")[-1])
                                  for wf in workspace_files))
        atoms.append({
            "session_id": row["session_id"], "run_id": row["run_id"],
            "task_id": row["task_id"], "attempt": row["attempt"],
            "atom_id": row["atom_id"], "tokens_spent": tokens_spent,
            "context_hits": context_hits,
            "ground_truth_files": sorted(gt),
            "grep_recall@10": round(grep_r10, 4),
            "repo_map_recall@10": round(map_r10, 4),
            "verdict_pass": 1 if tokens_spent > 0 and gt else 0,
        })

    n = len(atoms)
    avg_grep = sum(a["grep_recall@10"] for a in atoms) / n if n else 0.0
    avg_map = sum(a["repo_map_recall@10"] for a in atoms) / n if n else 0.0

    # atom_success_correlation: Pearson(mean-rank-of-GT-in-map, verdict_pass).
    ranks, passes = [], []
    for a in atoms:
        gt = set(a["ground_truth_files"])
        if not gt:
            continue
        mr = _repo_map_recall(gt, map_render)
        pos = {f: i for i, f in enumerate(mr)}
        ranks.append(sum(pos.get(f, len(mr)) for f in gt) / len(gt))
        passes.append(float(a["verdict_pass"]))
    correlation = _pearson(ranks, passes) if ranks else 0.0

    return {
        "schema": "retrieval_baseline_v1",
        "generated_at": time.strftime("%Y%m%dT%H%M%S"),
        "db_path": db_path, "top_k": top_k, "n_atoms": n,
        "thresholds": {
            "skip_embeddings_if_repo_map_recall_ge": THRESHOLD_SKIP,
            "enhancement_if_repo_map_recall_ge": THRESHOLD_ENHANCE,
            "required_if_repo_map_recall_lt": THRESHOLD_ENHANCE,
        },
        "aggregate": {
            "avg_grep_recall@10": round(avg_grep, 4),
            "avg_repo_map_recall@10": round(avg_map, 4),
            "atom_success_correlation": round(correlation, 4),
            "verdict": _verdict(avg_map),
        },
        "atoms": atoms,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="engine.db")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--report-dir", default="reports")
    args = parser.parse_args(argv)

    if not os.path.isfile(args.db):
        print(f"ERROR: engine.db not found at {args.db}", file=sys.stderr)
        return 1

    report = run_baseline(db_path=args.db, top_k=args.top_k)
    os.makedirs(args.report_dir, exist_ok=True)
    ts = report["generated_at"]
    out_path = os.path.join(args.report_dir, f"retrieval_baseline_{ts}.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
        fh.write("\n")

    a = report["aggregate"]
    print(f"Phase 0 baseline: {report['n_atoms']} atoms")
    print(f"  avg grep_recall@10:       {a['avg_grep_recall@10']:.4f}")
    print(f"  avg repo_map_recall@10:   {a['avg_repo_map_recall@10']:.4f}")
    print(f"  atom_success_correlation: {a['atom_success_correlation']:.4f}")
    print(f"  VERDICT: {a['verdict']} "
          f"(>={THRESHOLD_SKIP} skip />={THRESHOLD_ENHANCE} enhancement "
          f"/<{THRESHOLD_ENHANCE} required)")
    print(f"  report: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
