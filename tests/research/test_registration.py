"""Registration hooks test (T10).

Verifies that the research task type is wired end-to-end:
- CATEGORY_TO_TYPE maps 'research' -> 'research'
- task_role() returns 'researcher' for research tasks
- max_tokens_by_role has 'researcher': 8192
- pipeline retry key 'evidence_check' exists
- build_prd_result() sets the research flag
- ace research CLI dispatches via handler
"""

import json
import os
import tempfile

import pytest

from engine import Task, task_role, EngineConfig


def _research_task():
    return Task(
        id="R01", title="research atom",
        description="investigate caching strategies",
        module="engine/research/output.md",
        task_type="research",
    )


class TestCategoryMapping:
    def test_research_category_maps_to_research_type(self):
        from engine.prd import CATEGORY_TO_TYPE
        assert CATEGORY_TO_TYPE["research"] == "research"


class TestTaskRole:
    def test_research_task_role_is_researcher(self):
        assert task_role(_research_task()) == "researcher"

    def test_coder_task_unchanged(self):
        t = Task(id="T1", title="code", description="x", module="a.py",
                 task_type="implementation")
        assert task_role(t) == "coder"

    def test_explicit_role_attribute_wins(self):
        t = _research_task()
        t.role = "custom-role"
        assert task_role(t) == "custom-role"


class TestMaxTokensByRole:
    def test_researcher_has_8192(self):
        cfg = EngineConfig()
        assert cfg.max_tokens_by_role["researcher"] == 8192

    def test_coder_unchanged(self):
        cfg = EngineConfig()
        assert cfg.max_tokens_by_role["coder"] == 4096


class TestPipelineRetryKey:
    def test_evidence_check_key_exists(self):
        from engine.pipeline import Pipeline

        class _MockEngine:
            on_event = None

        pipe = Pipeline(_MockEngine())
        # Accessing a retry count initializes the task's retry dict.
        assert pipe.get_retry_count("T1", "evidence_check") == 0
        assert "evidence_check" in pipe.task_retries["T1"]

    def test_increment_evidence_check(self):
        from engine.pipeline import Pipeline

        class _MockEngine:
            on_event = None

        pipe = Pipeline(_MockEngine())
        pipe.increment_retry("T1", "evidence_check")
        assert pipe.get_retry_count("T1", "evidence_check") == 1


class TestPromptsResearchBranch:
    def test_build_prd_result_sets_research_flag(self):
        from engine.prompts import build_prd_result
        task = _research_task()

        class _Ctx:
            file_tree = {"root": "/tmp"}
            framework = None

        result = build_prd_result(task, _Ctx())
        assert result["tasks"][0]["is_research"] is True
        assert result["tasks"][0]["role"] == "researcher"

    def test_build_prd_result_code_task_unchanged(self):
        from engine.prompts import build_prd_result
        task = Task(id="T1", title="code", description="x", module="a.py",
                    task_type="implementation")

        class _Ctx:
            file_tree = {"root": "/tmp"}
            framework = None

        result = build_prd_result(task, _Ctx())
        assert "is_research" not in result["tasks"][0]


class TestHandlerDispatch:
    def test_research_task_resolves_to_research_handler(self):
        from engine.task_handler import _resolve_handler, CodeTaskHandler
        task = _research_task()

        class _MockEngine:
            pass

        handler = _resolve_handler(task, _MockEngine())
        assert not isinstance(handler, CodeTaskHandler)

    def test_code_task_resolves_to_code_handler(self):
        from engine.task_handler import _resolve_handler, CodeTaskHandler
        task = Task(id="T1", title="code", description="x", module="a.py",
                    task_type="implementation")

        class _MockEngine:
            pass

        handler = _resolve_handler(task, _MockEngine())
        assert isinstance(handler, CodeTaskHandler)


class TestAceResearchCLI:
    def test_cli_dispatch_exists(self):
        from engine.cli import cmd_research
        assert callable(cmd_research)

    def test_cli_runs_research_atom_from_prd_json(self):
        """E2E: ace research with the PRD-R01 JSON dispatches the handler."""
        from engine.cli import cmd_research

        prd_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "..",
            ".scratch", "research-task-type", "PRD-A-set", "PRD-R01.json")
        prd_path = os.path.abspath(prd_path)
        if not os.path.exists(prd_path):
            pytest.skip(f"PRD JSON not found: {prd_path}")

        class _Args:
            prd = prd_path
            project = tempfile.mkdtemp()

        result = cmd_research(_Args())
        assert result["task_id"] == "T01"
        assert result["role"] == "researcher"
        # T01 is a schema task (category research) — the handler runs it;
        # the result state depends on whether the research pipeline can
        # process a schema-only atom. We only assert dispatch succeeded.
        assert "state" in result
