"""Epic-5 tests: run_matrix API, RunReport schema, role-aware max_tokens.

All offline — mocked Engine.run, no transport, no BeeLlama.
"""

import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from engine import EngineConfig, task_role, Task  # noqa: E402
from engine.engine import Engine, RunResult, TaskResult, build_run_report  # noqa: E402


class TestTaskRole(unittest.TestCase):
    def _t(self, task_type):
        return Task(id="T1", title="t", description="d", module="m.py",
                    task_type=task_type)

    def test_coder_default(self):
        self.assertEqual(task_role(self._t("implementation")), "coder")

    def test_multi_turn(self):
        self.assertEqual(task_role(self._t("multi-turn")), "multi-turn")

    def test_orchestrator(self):
        self.assertEqual(task_role(self._t("orchestrator-planning")),
                         "orchestrator")

    def test_explicit_role_wins(self):
        t = self._t("implementation")
        t.role = "orchestrator"
        self.assertEqual(task_role(t), "orchestrator")


class TestRoleAwareMaxTokens(unittest.TestCase):
    def test_orchestrator_gets_4096(self):
        """T08 lesson: 2048 truncated orchestrator answers."""
        cfg = EngineConfig()
        self.assertEqual(cfg.max_tokens_by_role["orchestrator"], 4096)
        self.assertEqual(cfg.max_tokens_by_role["multi-turn"], 4096)
        # Live evidence 2026-09-09: 9B-MTP reasoning exhausts 2048
        self.assertEqual(cfg.max_tokens_by_role["coder"], 4096)


class _FakeTransport:
    def check_health(self):
        return True


class TestRunReportSchema(unittest.TestCase):
    """AC5.3: schema fields present and numeric."""

    def _report(self):
        cfg = EngineConfig(model_config="3090-qwen36-35b")
        result = RunResult(
            run_id="run-1", success=True, prd_path="p.md", project_path="/p",
            total_time_s=12.5, total_tokens=100,
            tasks=[TaskResult(
                task_id="T01", title="t", state="COMMIT", attempts=1,
                commit_sha="abc", time_s=10.0, tokens=100,
                error_message=None, prompt_tokens=40, completion_tokens=60,
                thinking_tokens=5, tokens_per_sec=42.5, role="coder",
                scores={"overall": 7.5, "scores": {}, "judge_model": ":8080",
                        "error": None},
            )],
            states_visited=[], error_message=None, judge={"overall_pass": True},
        )
        return build_run_report(result, cfg).to_dict()

    def test_contract_fields(self):
        r = self._report()
        for field in ("run_id", "config", "prd_path", "success",
                      "wall_clock_s", "total_tokens", "prompt_tokens",
                      "completion_tokens", "thinking_tokens",
                      "tokens_per_sec", "per_task", "judge_verdict",
                      "error_message"):
            self.assertIn(field, r)

    def test_fields_numeric(self):
        r = self._report()
        for field in ("wall_clock_s", "total_tokens", "prompt_tokens",
                      "completion_tokens", "thinking_tokens", "tokens_per_sec"):
            self.assertIsInstance(r[field], (int, float))

    def test_json_serializable(self):
        json.dumps(self._report())  # must not raise


class TestRunMatrix(unittest.TestCase):
    """AC5.2: run_matrix shape + variance summary (mocked runs)."""

    def _fake_run(self, prd_path, project_path, config=None, dry_run=False):
        cfg = config or EngineConfig()
        return RunResult(
            run_id=f"run-{abs(hash((prd_path, cfg.model_config))) % 99999}",
            success=True, prd_path=prd_path, project_path=project_path or "/p",
            total_time_s=1.0, total_tokens=50,
            tasks=[TaskResult(
                task_id="T01", title="t", state="COMMIT", attempts=1,
                commit_sha="sha", time_s=1.0, tokens=50, error_message=None,
                role="coder",
                scores={"overall": 6.0 + (hash(prd_path) % 3) * 0.5,
                        "scores": {}, "judge_model": ":8080", "error": None},
            )],
            states_visited=[], error_message=None,
        )

    def _patched_run(self, mock_run):
        def _side_effect(self_engine, prd, project_path=None, config=None,
                         dry_run=False):
            return self._fake_run(prd, project_path, config, dry_run)
        mock_run.side_effect = _side_effect

    def test_matrix_shape_and_summary(self):
        eng = Engine.__new__(Engine)  # skip transport probe
        eng.transport = _FakeTransport()
        eng.on_event = None
        eng.pipeline = None
        eng.config = EngineConfig()
        with patch.object(Engine, "run", autospec=True) as mock_run:
            self._patched_run(mock_run)
            out = eng.run_matrix(
                configs=["3090-qwen36-35b", "3070-qwen35-9b"],
                prds=["a.md", "b.md"], project_path="/p", repetitions=2,
                dry_run=True)
        self.assertEqual(set(out.keys()), {"matrix", "summary"})
        self.assertEqual(out["summary"]["total_runs"], 8)  # 2×2×2
        self.assertEqual(out["summary"]["variance"], "insufficient")
        cell = out["summary"]["cells"]["3090-qwen36-35b|a.md"]
        for field in ("runs", "pass_rate", "scores_mean", "scores_std",
                      "tokens_mean", "wall_clock_mean_s"):
            self.assertIn(field, cell)
        json.dumps(out)  # AC5.2: JSON-serializable

    def test_variance_reported_at_3_reps(self):
        eng = Engine.__new__(Engine)
        eng.transport = _FakeTransport()
        eng.on_event = None
        eng.pipeline = None
        eng.config = EngineConfig()
        with patch.object(Engine, "run", autospec=True) as mock_run:
            self._patched_run(mock_run)
            out = eng.run_matrix(configs=["c1"], prds=["a.md"],
                                 project_path="/p", repetitions=3,
                                 dry_run=True)
        self.assertEqual(out["summary"]["variance"], "reported")
        self.assertIsNotNone(
            out["summary"]["cells"]["c1|a.md"]["scores_std"])


if __name__ == "__main__":
    unittest.main()


class TestXmlFileTagExtraction(unittest.TestCase):
    """Regression (run-1788853635): 9B-MTP wraps code in <file path=...> tags;
    the extractor treated the tag line as code line 1 → guaranteed AST fail."""

    def _gen(self, raw, module="task_T01.py"):
        from engine.generator import generate_code
        from engine import Task

        class T:
            def curl_beellama(self, port, messages, **kw):
                return {"content": raw, "total_tokens": 10,
                        "thinking_tokens": 0, "prompt_tokens": 1,
                        "completion_tokens": 9, "predicted_per_second": 1.0,
                        "finish_reason": "stop", "reasoning_content": ""}
        task = Task(id="T01", title="t", description="d", module=module)
        cfg = EngineConfig()
        ctx = type("C", (), {"project_tree": "", "file_tree": {"root": ""}, "framework": None, "imports": {}, "relevant_files": [], "get": lambda k, d=None: d})()
        return generate_code(ctx, task, cfg, T())

    def test_xml_tag_block_extracted_clean(self):
        raw = ('<file path="task_T02.py">\n'
               '"""\nTask docstring\n"""\n\n'
               'def get_version() -> str:\n    return "1.0"\n'
               '</file>\n')
        code = self._gen(raw, module="task_T02.py")
        self.assertIn("task_T02.py", code.files)
        content = code.files["task_T02.py"]
        self.assertFalse(content.startswith("<"))
        self.assertIn("def get_version", content)

    def test_markdown_still_works(self):
        raw = "```python\n# File: api/health.py\ndef health():\n    return 1\n```"
        code = self._gen(raw, module="api/health.py")
        self.assertIn("def health", code.files["api/health.py"])


class TestProjectBootstrap(unittest.TestCase):
    """Cycle-2 regression: run() never initialized the project repo, so every
    commit failed with 'not a git repository' on a fresh project dir."""

    def test_bootstrap_called_on_real_run_path(self):
        import inspect
        from engine.engine import Engine
        src = inspect.getsource(Engine.run)
        self.assertIn("bootstrap_project", src)
        self.assertNotIn("if not dry_run and project_path:\n            pass",
                         src)

    def test_validator_import_error_is_directive(self):
        from engine.validator import check_imports
        ok, msg = check_imports("import flask\nx = 1", {})
        self.assertFalse(ok)
        self.assertIn("standard library only", msg)
        self.assertIn("Do NOT use third-party", msg)


class TestCellIsolation(unittest.TestCase):
    """Cycle-2 regression: run_matrix cells shared one project dir, bleeding
    context across PRDs (kvstore task regenerated sim-PRD's api/health.py)."""

    def test_cells_get_isolated_dirs(self):
        import tempfile
        from pathlib import Path
        base = tempfile.mkdtemp()
        eng = Engine.__new__(Engine)
        eng.transport = _FakeTransport()
        eng.on_event = None
        eng.pipeline = None
        eng.config = EngineConfig()
        seen = {}
        def _capture_run(self_engine, prd, project_path=None, config=None,
                         dry_run=False):
            seen[prd] = project_path
            return RunResult(
                run_id=f"run-{abs(hash(prd)) % 99999}", success=True,
                prd_path=prd, project_path=project_path or "/x",
                total_time_s=1.0, total_tokens=50,
                tasks=[TaskResult(
                    task_id="T01", title="t", state="COMMIT", attempts=1,
                    commit_sha="s", time_s=1.0, tokens=50, error_message=None,
                    role="coder",
                    scores={"overall": 7.0, "scores": {}, "judge_model": ":8080",
                            "error": None})],
                states_visited=[], error_message=None)
        with patch.object(Engine, "run", autospec=True) as mock_run:
            mock_run.side_effect = _capture_run
            eng.run_matrix(configs=["c1"], prds=["/tmp/alpha.md", "/tmp/beta.md"],
                           project_path=base, repetitions=1,
                           dry_run=False)
        self.assertEqual(seen["/tmp/alpha.md"], str(Path(base) / "alpha__c1"))
        self.assertEqual(seen["/tmp/beta.md"], str(Path(base) / "beta__c1"))
        self.assertNotEqual(seen["/tmp/alpha.md"], seen["/tmp/beta.md"])


class TestPrdFileLineParsing(unittest.TestCase):
    """Cycle-2 regression: 'File: x.py' lines and explicit task ids in
    headers were ignored — every task fell back to task_TXX.py, breaking
    cross-task imports in fresh projects."""

    def test_file_line_and_explicit_ids(self):
        from engine.prd import parse_prd
        import tempfile, os
        prd = (
            "# Stress PRD\n\n"
            "## Task S01: KV store\n"
            "Implement a KV store.\n"
            "File: kvstore.py\n\n"
            "## Task S03: Store tests\n"
            "Test the KV store, import KVStore from kvstore.\n"
            "File: test_kvstore.py\n"
        )
        path = os.path.join(tempfile.mkdtemp(), "p.md")
        with open(path, "w") as f:
            f.write(prd)
        tasks = parse_prd(path)
        self.assertEqual([t.id for t in tasks], ["S01", "S03"])
        self.assertEqual([t.module for t in tasks],
                         ["kvstore.py", "test_kvstore.py"])


class TestDottedProjectImport(unittest.TestCase):
    """Cycle-2 regression: import api.health was rejected although the
    project contained api/health.py (only exact module names were known)."""

    def test_top_level_package_of_known_module_resolves(self):
        from engine.validator import check_imports
        ok, msg = check_imports("from api.health import bp", {})
        self.assertFalse(ok)  # no known modules yet
        ok, msg = check_imports("from api.health import bp",
                                {"api.health": "api/health.py"})
        self.assertTrue(ok, msg)
        ok, msg = check_imports("import kvstore",
                                {"kvstore": "kvstore.py"})
        self.assertTrue(ok, msg)


class TestUnclosedFenceSalvage(unittest.TestCase):
    def _gen(self, raw, module):
        return TestXmlFileTagExtraction()._gen(raw, module)

    """Cycle-3 regression (run-1788861146): unclosed ```python fence → regex
    matched nothing → raw response incl. fence line became the file →
    'invalid syntax line 1' on EVERY attempt."""

    def test_unclosed_fence_salvaged_without_fence_lines(self):
        raw = ("Here is the code:\n```python\n"
               "from functools import wraps\n\ndef retry():\n    return 1\n")
        t = self._gen(raw, "retry.py")
        content = t.files["retry.py"]
        self.assertNotIn("```", content)
        self.assertIn("def retry", content)

    def test_closed_fence_still_extracted(self):
        raw = "```python\ndef a():\n    return 1\n```\nextra text"
        t = self._gen(raw, "a.py")
        self.assertEqual(t.files["a.py"], "def a():\n    return 1")


class TestExecProjectPath(unittest.TestCase):
    """Cycle-3 regression (run-1788861553): executing a test module that
    imports a project module failed — project root not on sys.path."""

    def test_project_module_importable_during_exec(self):
        import tempfile, os
        from engine.validator import check_execution
        root = tempfile.mkdtemp()
        with open(os.path.join(root, "kvstore.py"), "w") as f:
            f.write("class KVStore:\n    def get(self, k):\n        return 1\n")
        code = ("from kvstore import KVStore\n"
                "s = KVStore()\nassert s.get('x') == 1\n")
        ok, err = check_execution(code, {"__name__": "__test__"},
                                  project_path=root)
        self.assertTrue(ok, err)


class TestStrayFenceStrip(unittest.TestCase):
    """Defense-in-depth: extracted file content must never begin with a
    markdown fence line, whatever the model emitted."""

    def test_leading_and_trailing_fences_stripped(self):
        from engine.generator import _strip_stray_fences
        code = '```python\n```python\ndef f():\n    return 1\n```'
        out = _strip_stray_fences(code)
        self.assertTrue(out.startswith("def f"))
        self.assertNotIn("```", out)


class TestImportAlias(unittest.TestCase):
    """Cycle-3d: importlib_metadata is the PyPI backport of stdlib
    importlib.metadata — must not be rejected as third-party."""

    def test_importlib_metadata_accepted(self):
        from engine.validator import check_imports
        ok, msg = check_imports("import importlib_metadata", {})
        self.assertTrue(ok, msg)
