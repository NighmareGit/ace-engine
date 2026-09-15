"""Epic-5 tests: engine_scores persistence + engine.llm_judge scorer.

All offline — mock transports only, no BeeLlama calls, no benchmark DB.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from engine import state as engine_state  # noqa: E402
from engine import llm_judge  # noqa: E402


def _tmp_db():
    fd, path = tempfile.mkstemp(suffix=".engine.db")
    os.close(fd)
    os.unlink(path)
    return path


class _MockTransport:
    """BeeLlama mock: returns canned completions, counts calls."""

    def __init__(self, reply=None, fail_times=0):
        self.reply = reply or json.dumps({d: 8 for d in llm_judge.DIMENSIONS})
        self.fail_times = fail_times
        self.calls = 0
        self.last_port = None
        self.last_messages = None
        self.last_kwargs = None

    def curl_beellama(self, port, messages, **kwargs):
        self.calls += 1
        self.last_port = port
        self.last_messages = messages
        self.last_kwargs = kwargs
        if self.calls <= self.fail_times:
            raise TimeoutError("mock timeout")
        return {
            "model": "/data/Qwen3.6-35B-A3B-UD-IQ4_XS.gguf",
            "choices": [{"message": {"content": self.reply}}],
        }


class TestEngineScoresTable(unittest.TestCase):
    def setUp(self):
        self.old = engine_state.DB_PATH
        self.path = _tmp_db()
        engine_state.DB_PATH = self.path
        engine_state.init_db()

    def tearDown(self):
        engine_state.DB_PATH = self.old
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_table_created_lazily(self):
        conn = sqlite3.connect(self.path)
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        self.assertIn("engine_scores", names)

    def test_save_and_overall_generated(self):
        row = engine_state.save_engine_scores(
            "run-1", "T01", ":8080",
            {"completeness": 8, "correctness": 6, "quality": 7,
             "intelligence": 9, "role_fit": 10},
            reasoning="ok")
        conn = sqlite3.connect(self.path)
        r = conn.execute("SELECT overall, run_id, task_id FROM engine_scores "
                         "WHERE id=?", (row,)).fetchone()
        conn.close()
        self.assertEqual(r[0], 8.0)  # generated column: mean of 5 dims
        self.assertEqual((r[1], r[2]), ("run-1", "T01"))

    def test_check_constraint_rejects_out_of_range(self):
        with self.assertRaises(sqlite3.IntegrityError):
            engine_state.save_engine_scores(
                "run-1", "T01", ":8080",
                {"completeness": 11, "correctness": 6, "quality": 7,
                 "intelligence": 9, "role_fit": 10})


class TestLLMJudge(unittest.TestCase):
    def setUp(self):
        self.path = _tmp_db()
        self.old = engine_state.DB_PATH
        engine_state.DB_PATH = self.path
        engine_state.init_db()

    def tearDown(self):
        engine_state.DB_PATH = self.old
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_happy_path_scores_and_persists(self):
        t = _MockTransport()
        r = llm_judge.score_task("run-x", "T01", "Health endpoint",
                                 "def health(): ...", t, save=True)
        self.assertIsNone(r["error"])
        self.assertEqual(r["scores"], {d: 8 for d in llm_judge.DIMENSIONS})
        self.assertEqual(r["overall"], 8.0)
        self.assertIn("Qwen3.6-35B", r["judge_model"])
        self.assertEqual(t.last_port, 8080)  # judge pinned, never 8082
        self.assertEqual(t.calls, 1)
        conn = sqlite3.connect(self.path)
        n = conn.execute("SELECT COUNT(*) FROM engine_scores").fetchone()[0]
        conn.close()
        self.assertEqual(n, 1)

    def test_one_retry_cap_then_final_zeros(self):
        t = _MockTransport(fail_times=2)  # both attempts fail
        r = llm_judge.score_task("run-x", "T01", "t", "code", t, save=True)
        self.assertEqual(t.calls, 2)  # initial + ONE retry, no more
        self.assertEqual(r["scores"], {d: 0 for d in llm_judge.DIMENSIONS})
        self.assertIsNotNone(r["error"])

    def test_transient_then_success(self):
        t = _MockTransport(fail_times=1)  # first fails, retry succeeds
        r = llm_judge.score_task("run-x", "T01", "t", "code", t, save=True)
        self.assertEqual(t.calls, 2)
        self.assertIsNone(r["error"])
        self.assertEqual(r["overall"], 8.0)

    def test_malformed_json_is_final_zero(self):
        t = _MockTransport(reply="I cannot score this.")
        r = llm_judge.score_task("run-x", "T01", "t", "code", t, save=True)
        self.assertEqual(r["scores"], {d: 0 for d in llm_judge.DIMENSIONS})
        self.assertIn("no JSON", r["error"])

    def test_json_inside_prose_tolerated(self):
        t = _MockTransport(reply='Here is my assessment:\n```json\n'
                           '{"completeness": 5, "correctness": 5, "quality": 5,'
                           ' "intelligence": 5, "role_fit": 5}\n```')
        r = llm_judge.score_task("run-x", "T01", "t", "code", t, save=False)
        self.assertEqual(r["overall"], 5.0)

    def test_dims_clamped_to_0_10(self):
        t = _MockTransport(reply=json.dumps(
            {d: (99 if d == "quality" else -3 if d == "role_fit" else 7)
             for d in llm_judge.DIMENSIONS}))
        r = llm_judge.score_task("run-x", "T01", "t", "code", t, save=False)
        self.assertEqual(r["scores"]["quality"], 10)
        self.assertEqual(r["scores"]["role_fit"], 0)
        self.assertEqual(r["scores"]["completeness"], 7)

    def test_port_mismatch_refused(self):
        old_j, old_s = llm_judge.JUDGE_PORT, llm_judge.SUBJECT_PORT
        llm_judge.JUDGE_PORT = llm_judge.SUBJECT_PORT = 8080
        try:
            with self.assertRaises(ValueError):
                llm_judge.score_task("run-x", "T01", "t", "code",
                                     _MockTransport(), save=False)
        finally:
            llm_judge.JUDGE_PORT, llm_judge.SUBJECT_PORT = old_j, old_s

    def test_no_benchmark_db_writes(self):
        """Rule 6 guard: engine_score activity never touches benchmark-results.db."""
        t = _MockTransport()
        llm_judge.score_task("run-x", "T01", "t", "code", t, save=True)
        bench = os.path.join(os.path.dirname(engine_state.__file__), "..",
                             "benchmark-results.db")
        if not os.path.exists(bench):
            # File absent (untracked/never materialized) — nothing to pollute.
            self.assertNotEqual(os.path.abspath(engine_state.DB_PATH),
                                os.path.abspath(bench))
            return
        conn = sqlite3.connect(bench)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        self.assertNotIn("engine_scores", tables)


if __name__ == "__main__":
    unittest.main()
