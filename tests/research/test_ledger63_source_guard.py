"""LEDGER-63 regression tests: research task source-existence guard.

The phantom T02 task from the beellama dogfood driver had a description
that was pure output-contract boilerplate — no source file references.
The model fabricated claims ("142 new commits", "rebase resolves all
conflicts") that passed EvidenceValidator and reached submit_claims.

The guard (ResearchTaskHandler._guard_sources_declared) fails fast when:
  - the task description declares NO source paths at all, OR
  - none of the declared source paths exist in the project.

These tests verify the guard + the end-to-end handler behavior.
"""

import os
import sys
import tempfile
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(__file__, "..", "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine import Task
from engine.engine import State
from engine.research import ResearchTaskHandler, ResearchSourceError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_task(task_id="T02", title="Output contract (MANDATORY)",
               description="This is a RESEARCH task. Write to verdicts/v.md",
               files=None):
    return Task(
        id=task_id,
        title=title,
        description=description,
        module="verdicts/v.md",
        dependencies=[],
        task_type="research",
        priority=1,
        prd_section="",
        files=files or ["verdicts/v.md"],
    )


def _make_project_with_sources(tmpdir):
    """Create a minimal project with a dogfood-sources/ file."""
    src_dir = os.path.join(tmpdir, "dogfood-sources")
    os.makedirs(src_dir, exist_ok=True)
    with open(os.path.join(src_dir, "test.diff"), "w") as f:
        f.write("fake diff content for testing\n")
    os.makedirs(os.path.join(tmpdir, "verdicts"), exist_ok=True)


# ---------------------------------------------------------------------------
# Guard unit tests
# ---------------------------------------------------------------------------

class TestSourceExistenceGuard:
    """Unit tests for ResearchTaskHandler._guard_sources_declared."""

    def test_no_source_paths_raises(self):
        """A task with NO source references raises ResearchSourceError."""
        handler = ResearchTaskHandler(engine=None)
        task = _make_task(description="This is a RESEARCH task. Write to verdicts/v.md")
        with pytest.raises(ResearchSourceError, match="NO source files"):
            handler._guard_sources_declared(task, project_path="")

    def test_no_source_paths_with_project_raises(self):
        """Same, even when project_path is set (no candidates to check)."""
        handler = ResearchTaskHandler(engine=None)
        task = _make_task(description="Pure output-contract boilerplate, no paths.")
        with pytest.raises(ResearchSourceError, match="NO source files"):
            handler._guard_sources_declared(task, project_path="/tmp")

    def test_file_uri_source_exists_passes(self):
        """A task with a file:// source that exists passes the guard."""
        handler = ResearchTaskHandler(engine=None)
        tmpdir = tempfile.mkdtemp()
        try:
            _make_project_with_sources(tmpdir)
            task = _make_task(
                task_id="R06",
                title="Compatibility research",
                description="Sources: file://dogfood-sources/test.diff",
            )
            # Must NOT raise.
            handler._guard_sources_declared(task, project_path=tmpdir)
        finally:
            import shutil
            shutil.rmtree(tmpdir)

    def test_bare_relative_path_source_exists_passes(self):
        """A task with a bare relative path source that exists passes."""
        handler = ResearchTaskHandler(engine=None)
        tmpdir = tempfile.mkdtemp()
        try:
            _make_project_with_sources(tmpdir)
            task = _make_task(
                task_id="R06",
                title="Compatibility research",
                description="Sources: dogfood-sources/test.diff",
            )
            handler._guard_sources_declared(task, project_path=tmpdir)
        finally:
            import shutil
            shutil.rmtree(tmpdir)

    def test_source_not_exists_raises(self):
        """A task whose declared source doesn't exist raises ResearchSourceError."""
        handler = ResearchTaskHandler(engine=None)
        tmpdir = tempfile.mkdtemp()
        try:
            _make_project_with_sources(tmpdir)
            task = _make_task(
                task_id="R06",
                title="Compatibility research",
                description="Sources: file://dogfood-sources/nonexistent.diff",
            )
            with pytest.raises(ResearchSourceError, match="do not exist"):
                handler._guard_sources_declared(task, project_path=tmpdir)
        finally:
            import shutil
            shutil.rmtree(tmpdir)

    def test_empty_project_path_skips_existence_check_but_candidates_pass(self):
        """When project_path is empty/falsy, the guard only checks that
        candidates are declared (can't verify existence).  A task with a
        file:// URI passes the declaration check."""
        handler = ResearchTaskHandler(engine=None)
        task = _make_task(
            task_id="R06",
            title="Compatibility research",
            description="Sources: file://dogfood-sources/test.diff",
        )
        # Empty project_path -> can't check existence, but candidates exist.
        handler._guard_sources_declared(task, project_path="")

    def test_phantom_t02_description_fails(self):
        """The EXACT description from the T02 phantom task fails the guard."""
        handler = ResearchTaskHandler(engine=None)
        # This is the real T02 description from run-1789051779.
        task = _make_task(
            task_id="T02",
            title="Output contract (MANDATORY)",
            description=(
                "Output contract (MANDATORY)\n"
                "This is a RESEARCH task. The engine writes the verdict to: "
                "verdicts/R06-verdict.md\n"
                "Cite sources as [src-N] with file:// spans inside the "
                "dogfood-sources/ files ONLY (they exist in the workspace). "
                "NEVER write triple-backtick sequences anywhere."),
        )
        with pytest.raises(ResearchSourceError, match="NO source files"):
            handler._guard_sources_declared(task, project_path="/tmp/fake")


# ---------------------------------------------------------------------------
# Handler integration: guard converts to FAILED TaskResult
# ---------------------------------------------------------------------------

class TestGuardIntegrationWithHandler:
    """The guard must produce a clean FAILED TaskResult, not an uncaught
    exception, when run through ResearchTaskHandler.run()."""

    def test_handler_returns_failed_for_phantom_task(self):
        """A research task with no source paths fails at the guard with
        a FAILED TaskResult (not an exception)."""
        from engine import EngineConfig
        tmpdir = tempfile.mkdtemp()
        try:
            _make_project_with_sources(tmpdir)
            task = _make_task(
                task_id="T02",
                title="Output contract (MANDATORY)",
                description="This is a RESEARCH task. Write to verdicts/v.md",
            )
            # Engine with a REAL config (enforce_research_sources defaults True).
            engine_mock = MagicMock()
            engine_mock.config = EngineConfig(enforce_research_sources=True)
            handler = ResearchTaskHandler(engine=engine_mock)
            # Minimal pipeline stub.
            pipeline = type("P", (), {"run_id": "test-run"})()
            result = handler.run(task, pipeline, project_path=tmpdir)
            assert result.state == State.FAILED.value
            assert "NO source files" in (result.error_message or "")
        finally:
            import shutil
            shutil.rmtree(tmpdir)
