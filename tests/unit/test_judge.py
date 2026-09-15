#!/usr/bin/env python3
"""Unit tests for CCBS-009: Judge Extension + Automated Checks.

Tests the following functions:
- build_judge_prompt()
- run_automated_check()
- compute_reliability()
- score_run()
- _parse_judge_response()
- save_scores()
"""

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Import ccbs-judge.py (hyphenated filename requires importlib)
# ---------------------------------------------------------------------------
_CCBS_JUDGE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "tickets", "ccbs", "ccbs-judge.py"
)
_CCBS_JUDGE_PATH = os.path.normpath(_CCBS_JUDGE_PATH)

_spec = importlib.util.spec_from_file_location("ccbs_judge", _CCBS_JUDGE_PATH)
_ccbs_judge = importlib.util.module_from_spec(_spec)
sys.modules["ccbs_judge"] = _ccbs_judge
_spec.loader.exec_module(_ccbs_judge)

# Now import the names we need
build_judge_prompt = _ccbs_judge.build_judge_prompt
run_automated_check = _ccbs_judge.run_automated_check
compute_reliability = _ccbs_judge.compute_reliability
score_run = _ccbs_judge.score_run
_parse_judge_response = _ccbs_judge._parse_judge_response
_call_judge_model = _ccbs_judge._call_judge_model
save_scores = _ccbs_judge.save_scores
CCBS_DIMENSIONS = _ccbs_judge.CCBS_DIMENSIONS
JUDGE_PROMPT_TEMPLATE = _ccbs_judge.JUDGE_PROMPT_TEMPLATE


class TestBuildJudgePrompt(unittest.TestCase):
    """Tests for build_judge_prompt()."""

    def test_basic_prompt_generation(self):
        """Prompt contains task and response content."""
        task = {
            "prompt": "Write a fibonacci function",
            "expected_behavior": "Returns first n Fibonacci numbers",
        }
        response = "def fibonacci(n): return [0, 1, 1, 2, 3]"

        prompt = build_judge_prompt(task, response)

        self.assertIn("Write a fibonacci function", prompt)
        self.assertIn("Returns first n Fibonacci numbers", prompt)
        self.assertIn("def fibonacci(n)", prompt)
        self.assertIn("hallucinations", prompt.lower())

    def test_truncation_long_task(self):
        """Long task prompt is truncated."""
        task = {
            "prompt": "A" * 2000,
            "expected_behavior": "B" * 1000,
        }
        response = "C" * 3000

        prompt = build_judge_prompt(task, response)

        self.assertIn("[truncated]", prompt)
        # Should not contain full 2000-char prompt
        self.assertNotIn("A" * 2000, prompt)

    def test_empty_task_fields(self):
        """Handles missing/empty task fields gracefully."""
        task = {}
        response = "Some response"

        prompt = build_judge_prompt(task, response)

        # Should use defaults
        self.assertIn("See task description", prompt)
        self.assertIn("Some response", prompt)

    def test_prompt_contains_all_dimensions(self):
        """Prompt mentions all scoring dimensions."""
        task = {"prompt": "test", "expected_behavior": "test"}
        response = "test"

        prompt = build_judge_prompt(task, response)

        for dim in CCBS_DIMENSIONS:
            self.assertIn(dim, prompt)

    def test_prompt_format_json_only(self):
        """Prompt instructs JSON-only output."""
        task = {"prompt": "test", "expected_behavior": "test"}
        response = "test"

        prompt = build_judge_prompt(task, response)

        self.assertIn("Output JSON only", prompt)


class TestParseJudgeResponse(unittest.TestCase):
    """Tests for _parse_judge_response()."""

    def test_valid_json(self):
        """Parses valid JSON with all dimensions."""
        content = json.dumps({
            "correctness": 8,
            "completeness": 7,
            "quality": 9,
            "type_safety": 6,
            "documentation": 8,
            "edge_cases": 5,
            "hallucinations": ["fake function foo()"],
            "reasoning": "Good implementation but missing edge cases",
        })

        result = _parse_judge_response(content)

        self.assertEqual(result["correctness"], 8)
        self.assertEqual(result["completeness"], 7)
        self.assertEqual(result["quality"], 9)
        self.assertEqual(result["type_safety"], 6)
        self.assertEqual(result["documentation"], 8)
        self.assertEqual(result["edge_cases"], 5)
        self.assertEqual(len(result["hallucinations"]), 1)

    def test_json_in_code_block(self):
        """Extracts JSON from markdown code block."""
        content = """Here is my evaluation:

```json
{"correctness": 7, "completeness": 6, "quality": 8, "type_safety": 5,
 "documentation": 7, "edge_cases": 4, "hallucinations": [], "reasoning": "ok"}
```
"""
        result = _parse_judge_response(content)

        self.assertEqual(result["correctness"], 7)
        self.assertEqual(result["quality"], 8)

    def test_empty_content(self):
        """Returns empty dict for empty content."""
        result = _parse_judge_response("")
        self.assertEqual(result, {})

    def test_none_content(self):
        """Returns empty dict for None content."""
        result = _parse_judge_response(None)
        self.assertEqual(result, {})

    def test_malformed_json(self):
        """Falls back to regex extraction for malformed JSON."""
        content = 'correctness: 7, quality: 8, documentation: 6'
        result = _parse_judge_response(content)

        # Should extract via regex fallback
        self.assertEqual(result.get("correctness"), 7)
        self.assertEqual(result.get("quality"), 8)

    def test_hallucination_array_extraction(self):
        """Extracts hallucination list from JSON."""
        content = json.dumps({
            "correctness": 5,
            "hallucinations": ["nonexistent.lib.foo", "wrong_api()"],
            "reasoning": "Found 2 hallucinations",
        })
        result = _parse_judge_response(content)

        self.assertEqual(len(result["hallucinations"]), 2)
        self.assertIn("nonexistent.lib.foo", result["hallucinations"])

    def test_score_capping(self):
        """Scores are capped at 0-10."""
        content = json.dumps({
            "correctness": 15,
            "completeness": -3,
            "quality": 10,
            "type_safety": 7,
            "documentation": 8,
            "edge_cases": 9,
        })
        # Note: capping happens in score_run, not in parse
        result = _parse_judge_response(content)
        self.assertEqual(result["correctness"], 15)  # Parse preserves raw

    def test_nested_score_objects(self):
        """Handles nested score objects like {"score": N, "justification": "..."}."""
        content = json.dumps({
            "correctness": {"score": 8, "justification": "Works well"},
            "completeness": {"score": 7, "justification": "Mostly complete"},
        })
        result = _parse_judge_response(content)

        # Parse returns raw, score_run handles nested
        self.assertIsInstance(result["correctness"], dict)


class TestRunAutomatedCheck(unittest.TestCase):
    """Tests for run_automated_check()."""

    def test_no_automated_check(self):
        """Returns appropriate message when no check defined."""
        task = {"id": "TEST-01", "automated_check": None}
        response = "def foo(): pass"

        result = run_automated_check(task, response)

        self.assertFalse(result["passed"])
        self.assertIn("No automated check", result["output"])

    def test_no_code_blocks(self):
        """Returns error when response has no code blocks."""
        task = {
            "id": "TEST-02",
            "automated_check": "python3 -c 'print(1)'",
        }
        response = "Here is my answer without code blocks."

        result = run_automated_check(task, response)

        self.assertFalse(result["passed"])
        self.assertIn("No code blocks", result["output"])

    def test_successful_execution(self):
        """Passes when code runs successfully."""
        task = {
            "id": "TEST-03",
            "automated_check": "python3 -c 'assert 1+1 == 2; print(\"All tests passed\")'",
        }
        response = "```python\nresult = 1 + 1\nassert result == 2\n```"

        result = run_automated_check(task, response, workspace=tempfile.mkdtemp())

        self.assertTrue(result["passed"])
        self.assertEqual(result["pass_rate"], 1.0)

    def test_failed_execution(self):
        """Fails when code raises an error."""
        task = {
            "id": "TEST-04",
            "automated_check": "python3 -c 'assert False'",
        }
        response = "```python\nassert False\n```"

        result = run_automated_check(task, response, workspace=tempfile.mkdtemp())

        self.assertFalse(result["passed"])
        self.assertEqual(result["pass_rate"], 0.0)

    def test_creates_workspace_directory(self):
        """Creates task directory in workspace."""
        task = {
            "id": "TEST-05",
            "automated_check": "python3 -c 'print(1)'",
        }
        response = "```python\nprint(1)\n```"

        with tempfile.TemporaryDirectory() as tmpdir:
            run_automated_check(task, response, workspace=tmpdir)
            task_dir = os.path.join(tmpdir, "task_TEST-05")
            self.assertTrue(os.path.isdir(task_dir))
            self.assertTrue(os.path.isfile(os.path.join(task_dir, "solution.py")))


class TestComputeReliability(unittest.TestCase):
    """Tests for compute_reliability()."""

    def _create_test_db(self, runs_data):
        """Create a temporary database with test data."""
        tmpdir = tempfile.mkdtemp()
        db_path = os.path.join(tmpdir, "test.db")

        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS model_configs (
                id TEXT PRIMARY KEY, gpu TEXT, model_name TEXT, port INTEGER, model_path TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, role TEXT, category TEXT, prompt TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS benchmark_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model_config_id TEXT, task_id TEXT, status TEXT,
                response_text TEXT, completed_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS judge_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER, judge_model TEXT,
                completeness INTEGER, correctness INTEGER, quality INTEGER,
                intelligence INTEGER, role_fit INTEGER,
                judge_reasoning TEXT, scored_at TEXT
            )
        """)

        # Insert test data
        conn.execute("INSERT INTO model_configs VALUES ('cfg1', '3090', 'test', 8080, '/m')")
        conn.execute("INSERT INTO tasks VALUES ('task1', 'coder', 'func', 'test prompt')")

        for i, (quality, reasoning) in enumerate(runs_data):
            conn.execute(
                "INSERT INTO benchmark_runs (model_config_id, task_id, status) VALUES (?, ?, 'complete')",
                ("cfg1", "task1"),
            )
            run_id = i + 1
            conn.execute(
                "INSERT INTO judge_scores (run_id, judge_model, quality, judge_reasoning, scored_at) "
                "VALUES (?, 'test', ?, ?, datetime('now', ?))",
                (run_id, quality, reasoning, f"-{i} hours"),
            )

        conn.commit()
        conn.close()
        return db_path, tmpdir

    def test_empty_database(self):
        """Returns zeros when no runs exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "empty.db")
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE benchmark_runs (id INTEGER, model_config_id TEXT, task_id TEXT, status TEXT)")
            conn.execute("CREATE TABLE judge_scores (id INTEGER, run_id INTEGER, quality INTEGER, judge_reasoning TEXT, scored_at TEXT)")
            conn.close()

            result = compute_reliability("cfg1", "task1", db_path)

            self.assertEqual(result["run_count"], 0)

    def test_nonexistent_database(self):
        """Returns zeros for missing database."""
        result = compute_reliability("cfg1", "task1", "/nonexistent/db.sqlite")
        self.assertEqual(result["run_count"], 0)

    def test_single_run(self):
        """Handles single run correctly."""
        db_path, tmpdir = self._create_test_db([
            (8, "Good work"),
        ])
        try:
            result = compute_reliability("cfg1", "task1", db_path)
            self.assertEqual(result["run_count"], 1)
            self.assertEqual(result["quality_mean"], 8.0)
            self.assertEqual(result["quality_stddev"], 0.0)
        finally:
            import shutil
            shutil.rmtree(tmpdir)

    def test_multiple_runs_mean(self):
        """Computes correct mean for multiple runs."""
        db_path, tmpdir = self._create_test_db([
            (6, "Ok"),
            (8, "Good"),
            (10, "Excellent"),
        ])
        try:
            result = compute_reliability("cfg1", "task1", db_path)
            self.assertEqual(result["run_count"], 3)
            self.assertAlmostEqual(result["quality_mean"], 8.0, places=1)
        finally:
            import shutil
            shutil.rmtree(tmpdir)

    def test_multiple_runs_stddev(self):
        """Computes correct standard deviation."""
        db_path, tmpdir = self._create_test_db([
            (5, "Average"),
            (5, "Average"),
            (5, "Average"),
        ])
        try:
            result = compute_reliability("cfg1", "task1", db_path)
            self.assertEqual(result["quality_stddev"], 0.0)
        finally:
            import shutil
            shutil.rmtree(tmpdir)

    def test_drift_calculation(self):
        """Computes drift percentage between first and last run."""
        db_path, tmpdir = self._create_db_with_ordered_runs([
            (5, "First run"),
            (10, "Last run"),
        ])
        try:
            result = compute_reliability("cfg1", "task1", db_path)
            # Oldest=5, newest=10 → drift = (10-5)/5 * 100 = 100%
            self.assertAlmostEqual(result["drift_pct"], 100.0, places=1)
        finally:
            import shutil
            shutil.rmtree(tmpdir)

    def test_hallucination_rate(self):
        """Computes hallucination rate from reasoning text."""
        db_path, tmpdir = self._create_test_db([
            (8, "Good code, clean implementation"),
            (6, "Found a hallucination: fake lib"),
            (7, "Another hallucination detected"),
            (9, "Clean response, no issues"),
        ])
        try:
            result = compute_reliability("cfg1", "task1", db_path)
            # 2 out of 4 have "hallucination" in reasoning
            self.assertAlmostEqual(result["hallucination_rate"], 0.5, places=1)
        finally:
            import shutil
            shutil.rmtree(tmpdir)

    def _create_db_with_ordered_runs(self, runs_data):
        """Create DB with explicit ordering for drift test."""
        tmpdir = tempfile.mkdtemp()
        db_path = os.path.join(tmpdir, "test.db")

        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE model_configs (id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE benchmark_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, model_config_id TEXT, task_id TEXT, status TEXT)")
        conn.execute("CREATE TABLE judge_scores (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, quality INTEGER, judge_reasoning TEXT, scored_at TEXT)")

        conn.execute("INSERT INTO model_configs VALUES ('cfg1')")
        conn.execute("INSERT INTO tasks VALUES ('task1')")

        for i, (quality, reasoning) in enumerate(runs_data):
            conn.execute("INSERT INTO benchmark_runs (model_config_id, task_id, status) VALUES ('cfg1', 'task1', 'complete')")
            run_id = i + 1
            # Use explicit timestamps: older first
            conn.execute(
                "INSERT INTO judge_scores (run_id, quality, judge_reasoning, scored_at) VALUES (?, ?, ?, ?)",
                (run_id, quality, reasoning, f"2024-01-0{i+1}T00:00:00"),
            )

        conn.commit()
        conn.close()
        return db_path, tmpdir


class TestScoreRun(unittest.TestCase):
    """Tests for score_run()."""

    def test_score_run_with_automated_check_pass(self):
        """Scoring with passing automated check."""
        task = {
            "id": "CCBS-L1-02",
            "prompt": "Write fibonacci",
            "expected_behavior": "Returns n Fibonacci numbers",
            "automated_check": "python3 -c 'assert 1+1==2; print(\"All tests passed\")'",
        }
        response = "```python\ndef fibonacci(n): pass\n```"

        mock_auto_result = {
            "passed": True,
            "pass_rate": 1.0,
            "output": "All tests passed",
            "test_count": 0,
            "error_count": 0,
        }

        with patch("ccbs_judge._call_judge_model") as mock_judge, \
             patch("ccbs_judge.run_automated_check", return_value=mock_auto_result):
            mock_judge.return_value = {
                "correctness": 9,
                "completeness": 8,
                "quality": 7,
                "type_safety": 6,
                "documentation": 8,
                "edge_cases": 5,
                "hallucinations": [],
                "reasoning": "Good code",
            }

            result = score_run(1, task, response)

        self.assertEqual(result["run_id"], 1)
        self.assertIsNotNone(result["judge_scores"])
        # Correctness should be overridden by automated check (10 for pass)
        self.assertEqual(result["judge_scores"]["correctness"], 10)
        self.assertIsNotNone(result["overall"])

    def test_score_run_with_automated_check_fail(self):
        """Scoring with failing automated check."""
        task = {
            "id": "CCBS-L1-02",
            "prompt": "Write fibonacci",
            "expected_behavior": "Returns n Fibonacci numbers",
            "automated_check": "python3 -c 'assert False'",
        }
        response = "```python\ndef fibonacci(n): pass\n```"

        with patch("ccbs_judge._call_judge_model") as mock_judge:
            mock_judge.return_value = {
                "correctness": 5,
                "completeness": 8,
                "quality": 7,
                "type_safety": 6,
                "documentation": 8,
                "edge_cases": 5,
                "hallucinations": [],
                "reasoning": "Code doesn't pass tests",
            }

            result = score_run(1, task, response)

        # Correctness should be 0 from failed check
        self.assertEqual(result["judge_scores"]["correctness"], 0)

    def test_score_run_judge_failure(self):
        """Handles judge model failure gracefully."""
        task = {
            "id": "TEST-01",
            "prompt": "test",
            "expected_behavior": "test",
        }
        response = "test response"

        with patch("ccbs_judge._call_judge_model") as mock_judge:
            mock_judge.return_value = None

            result = score_run(1, task, response)

        self.assertIsNotNone(result["error"])
        self.assertIn("unavailable", result["error"])

    def test_score_run_no_automated_check(self):
        """Scoring without automated check only uses judge."""
        task = {
            "id": "TEST-01",
            "prompt": "Write something",
            "expected_behavior": "A response",
            "automated_check": None,
        }
        response = "Here is my response"

        with patch("ccbs_judge._call_judge_model") as mock_judge:
            mock_judge.return_value = {
                "correctness": 7,
                "completeness": 8,
                "quality": 9,
                "type_safety": 6,
                "documentation": 7,
                "edge_cases": 5,
                "hallucinations": ["fake library"],
                "reasoning": "Decent work",
            }

            result = score_run(1, task, response)

        self.assertEqual(result["hallucinations"], ["fake library"])
        self.assertEqual(result["judge_scores"]["correctness"], 7)

    def test_score_run_capping(self):
        """Scores are capped at 0-10 range."""
        task = {"id": "T", "prompt": "t", "expected_behavior": "t"}
        response = "r"

        with patch("ccbs_judge._call_judge_model") as mock_judge:
            mock_judge.return_value = {
                "correctness": 15,
                "completeness": -3,
                "quality": 10,
                "type_safety": 7,
                "documentation": 8,
                "edge_cases": 9,
            }

            result = score_run(1, task, response)

        self.assertEqual(result["judge_scores"]["correctness"], 10)  # Capped
        self.assertEqual(result["judge_scores"]["completeness"], 0)  # Capped


class TestCallJudgeModel(unittest.TestCase):
    """Tests for _call_judge_model()."""

    @patch("urllib.request.urlopen")
    def test_successful_call(self, mock_urlopen):
        """Returns parsed response on success."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "choices": [{"message": {"content": json.dumps({"correctness": 8})}}]
        }).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = _call_judge_model("test prompt", judge_port=8082, max_retries=0)

        self.assertIsNotNone(result)
        self.assertEqual(result["correctness"], 8)

    @patch("urllib.request.urlopen")
    def test_retry_on_failure(self, mock_urlopen):
        """Retries on connection error."""
        import urllib.error

        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] < 3:
                raise urllib.error.URLError("Connection refused")
            # On success, return a context manager that yields a response
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({
                "choices": [{"message": {"content": json.dumps({"correctness": 7})}}]
            }).encode()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            return mock_resp

        mock_urlopen.side_effect = side_effect

        result = _call_judge_model("test", judge_port=8082, max_retries=2)

        self.assertIsNotNone(result)
        self.assertEqual(result["correctness"], 7)

    @patch("urllib.request.urlopen")
    def test_all_retries_exhausted(self, mock_urlopen):
        """Returns None when all retries fail."""
        import urllib.error
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        result = _call_judge_model("test", judge_port=8082, max_retries=2)

        self.assertIsNone(result)


class TestSaveScores(unittest.TestCase):
    """Tests for save_scores()."""

    def test_save_scores_success(self):
        """Saves scores to database."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE judge_scores ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "run_id INTEGER, judge_model TEXT, "
                "completeness INTEGER, correctness INTEGER, quality INTEGER, "
                "intelligence INTEGER, role_fit INTEGER, "
                "judge_reasoning TEXT, scored_at TEXT)")
            conn.commit()
            conn.close()

            scores = {
                "correctness": 8,
                "completeness": 7,
                "quality": 9,
                "type_safety": 6,
                "documentation": 8,
                "edge_cases": 5,
            }
            hallucinations = ["fake function"]

            result = save_scores(1, scores, hallucinations, db_path)

            self.assertTrue(result)

            # Verify saved data
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM judge_scores WHERE run_id=1").fetchone()
            conn.close()

            self.assertIsNotNone(row)
            self.assertEqual(row["correctness"], 8)
            self.assertEqual(row["completeness"], 7)
            self.assertEqual(row["quality"], 9)

    def test_save_scores_nonexistent_db(self):
        """Returns False for missing database."""
        result = save_scores(1, {"correctness": 8}, [], "/nonexistent/db.sqlite")
        self.assertFalse(result)


class TestModuleImports(unittest.TestCase):
    """Tests for module-level constants and imports."""

    def test_ccbs_dimensions_list(self):
        """CCBS dimensions are defined."""
        self.assertEqual(len(CCBS_DIMENSIONS), 6)
        self.assertIn("correctness", CCBS_DIMENSIONS)
        self.assertIn("completeness", CCBS_DIMENSIONS)
        self.assertIn("quality", CCBS_DIMENSIONS)
        self.assertIn("type_safety", CCBS_DIMENSIONS)
        self.assertIn("documentation", CCBS_DIMENSIONS)
        self.assertIn("edge_cases", CCBS_DIMENSIONS)

    def test_judge_prompt_template(self):
        """Template has expected placeholders."""
        self.assertIn("{task_prompt}", JUDGE_PROMPT_TEMPLATE)
        self.assertIn("{expected_behavior}", JUDGE_PROMPT_TEMPLATE)
        self.assertIn("{response_text}", JUDGE_PROMPT_TEMPLATE)


if __name__ == "__main__":
    unittest.main()
