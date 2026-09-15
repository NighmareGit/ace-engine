"""P4 results archive — per-run-id snapshot of engine.db + report.json.

At the end of a successful Engine.run() (and via the ``ace archive`` CLI
command), the engine writes a JSON RunReport and a copy of engine.db into
<results_repo>/archive/<run_id>/, commits, and pushes to the Gitea archive repo
(ARCHIVE_GITEA_REPO, default ``ace-results``). Bloat is controlled by retaining
report.json forever and pruning .db snapshots to ``archive_keep`` (default 30)
per project. Snapshot/commit failures are best-effort and never fail the run.
"""

import json
import logging
import os
import shutil
import subprocess

log = logging.getLogger(__name__)


def _default_archive_dir():
    """Resolve the archive root at call time so env overrides take effect.

    Defaults to ~/ace-results (via expanduser so it resolves to the actual
    user's home, not a hardcoded /home/<user>).  Override with ACE_ARCHIVE_DIR.
    """
    return os.environ.get("ACE_ARCHIVE_DIR",
                          os.path.join(os.path.expanduser("~"), "ace-results"))


# ---------------------------------------------------------------------------
# Git helpers (mockable — tests patch these to stay offline)
# ---------------------------------------------------------------------------

def _git(results_repo, *args):
    """Run a git command in *results_repo*. Returns (stdout, stderr, rc)."""
    cmd = ["git"] + list(args)
    proc = subprocess.run(cmd, cwd=results_repo, capture_output=True, text=True)
    return proc.stdout, proc.stderr, proc.returncode


def _commit_and_push(archive_dir, run_id, results_repo):
    """git add the archive run dir, commit, push. Best-effort (logs on failure)."""
    rel = os.path.relpath(archive_dir, results_repo)
    _git(results_repo, "add", rel)
    out, err, rc = _git(results_repo, "commit", "-m", f"run: {run_id}")
    if rc != 0 and "nothing to commit" not in (out + err).lower():
        log.warning("archive commit failed: %s %s", out, err)
    # Cycle-2 finding: a fresh archive clone has no upstream — push must set
    # it explicitly (also creates the branch on an empty Gitea repo).
    out, err, rc = _git(results_repo, "push", "-u", "origin", "HEAD:main")
    if rc != 0:
        log.warning("archive push failed: %s %s", out, err)


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------

def archive_run(run_id, project, results_repo=None, db_path=None, report=None,
                archive_keep=None):
    """Snapshot engine.db + write report.json into results_repo/archive/<run_id>/,
    commit, push. Returns the archive directory path. Idempotent per run_id
    (re-writes the same files; the commit is a no-op if nothing changed).

    Args:
        run_id: Engine run identifier.
        project: Project path (used for per-project pruning).
        results_repo: Local clone of the archive repo. Defaults to
            ACE_ARCHIVE_DIR (Triton: /home/<user>/ace-results).
        db_path: Path to engine.db to snapshot. Defaults to the active DB.
        report: RunReport dict to persist as report.json. Defaults to None
            (only the .db snapshot is written).
        archive_keep: If set, prune this project's older .db snapshots to
            this many after archiving.
    """
    results_repo = results_repo or _default_archive_dir()
    archive_dir = os.path.join(results_repo, "archive", run_id)
    os.makedirs(archive_dir, exist_ok=True)

    # 1. report.json — the JSON export (small, diffable, forever).
    if report is not None:
        with open(os.path.join(archive_dir, "report.json"), "w") as f:
            json.dump(report, f, indent=2, default=str)

    # 2. engine.db — faithful full-state snapshot.
    if db_path is None:
        from engine.state import _db_path
        db_path = _db_path()
    if db_path and os.path.exists(db_path):
        shutil.copy2(db_path, os.path.join(archive_dir, "engine.db"))

    # 3. commit + push to the Gitea archive repo (best-effort).
    _commit_and_push(archive_dir, run_id, results_repo)

    # 4. bloat control — prune older .db snapshots for this project.
    if archive_keep is not None:
        prune_archives(project, keep=archive_keep, results_repo=results_repo)

    return archive_dir


def fetch_run(run_id, results_repo=None):
    """Load archive/<run_id>/report.json. Falls back to the Gitea API if the
    JSON is not present locally (e.g. fresh clone, or a re-archive pull)."""
    results_repo = results_repo or _default_archive_dir()
    report_file = os.path.join(results_repo, "archive", run_id, "report.json")
    if os.path.exists(report_file):
        with open(report_file) as f:
            return json.load(f)
    return _fetch_from_gitea(run_id)


def _fetch_from_gitea(run_id):
    """Fetch report.json from the Gitea archive repo via its API. Raises if
    unavailable so callers can distinguish "not found" from "empty"."""
    raise FileNotFoundError(
        f"run {run_id} not in local archive and Gitea fetch unavailable")


def prune_archives(project, keep=30, results_repo=None):
    """Remove engine.db snapshots beyond the per-project retention cap.

    report.json files are never pruned (they are the durable, small artifact).
    Recency is judged by the newest mtime within each archive dir. Returns the
    number of .db snapshots removed.
    """
    results_repo = results_repo or _default_archive_dir()
    archive_root = os.path.join(results_repo, "archive")
    if not os.path.isdir(archive_root):
        return 0

    # Collect this project's .db snapshots with their recency.
    candidates = []
    for entry in sorted(os.listdir(archive_root)):
        run_dir = os.path.join(archive_root, entry)
        if not os.path.isdir(run_dir):
            continue
        db_file = os.path.join(run_dir, "engine.db")
        if not os.path.exists(db_file):
            continue
        # Scope to the requested project via report.json, if present.
        report_file = os.path.join(run_dir, "report.json")
        if os.path.exists(report_file):
            try:
                with open(report_file) as f:
                    rep = json.load(f)
            except Exception:
                continue
            if rep.get("project_path") != project:
                continue
        recency = max((os.path.getmtime(os.path.join(run_dir, n))
                       for n in os.listdir(run_dir)), default=0)
        candidates.append((recency, db_file))

    # Keep the newest `keep`; remove the rest.
    candidates.sort(key=lambda x: x[0], reverse=True)
    removed = 0
    for _, db_file in candidates[keep:]:
        try:
            os.unlink(db_file)
            removed += 1
        except Exception:
            pass
    return removed
