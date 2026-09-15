"""Tests for the judge hook inside engine.engine.Engine.run().

Covers:
  1. EngineConfig().judge defaults to False.
  2. judge=True: judge_run attaches result["judge"] and calls save_judge_verdict.
  3. judge raising an exception does not fail the run.
  4. judge=False: judge_run never called.
"""

import sys, os
from unittest.mock import MagicMock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine import EngineConfig
from engine.engine import Engine, RunResult, TaskResult
from engine.judge import JudgeVerdict, ScoreDimension
from engine.state import State


def _task():
    t = MagicMock()
    t.id = "T01"; t.title = "A"; t.module = "app.py"
    t.dependencies = []; t.task_type = "implementation"
    t.priority = 1; t.files = []; t.acceptance_criteria = []
    return t


def _verdict():
    return JudgeVerdict(
        overall_pass=True, score=1.0,
        dimensions=[ScoreDimension(n, w, True, "ok") for n, w in
            [("success", 0.30), ("tasks", 0.20), ("commit", 0.20),
             ("tokens", 0.15), ("error_free", 0.15)]])


def _fast_result():
    return TaskResult("T01", "A", "done", 1, "abc123", 0.1, 500, None)


def _make_engine(judge=False):
    transport = MagicMock()
    transport.check_health.return_value = True
    # P0/cycle-2: run() now bootstraps the project repo (git init etc.).
    # Fake the shell out: every run_command reports failure (no-ops on mock).
    transport.run_command.return_value = ("", "", 1)
    eng = Engine(transport=transport, config=EngineConfig(judge=judge))
    eng._run_task = MagicMock(return_value=_fast_result())
    return eng


def _patch_pipeline(mp):
    p = MagicMock(); p.state = State.DONE; p.states_visited = []
    mp.setattr("engine.engine.Pipeline", lambda _: p)
    mp.setattr("engine.engine.parse_prd", lambda _: [_task()])
    mp.setattr("engine.engine.emit_event", MagicMock())


# (1) EngineConfig().judge defaults to False
def test_config_judge_defaults_false():
    assert EngineConfig().judge is False

def test_config_judge_can_be_set_true():
    assert EngineConfig(judge=True).judge is True


# (2) judge=True: judge_run attaches verdict and calls save
def test_judge_true_attaches_verdict_and_saves(monkeypatch):
    verdict = _verdict()
    mock_judge_run = MagicMock(return_value=verdict)
    mock_save = MagicMock()
    _patch_pipeline(monkeypatch)
    monkeypatch.setattr("engine.engine.save_judge_verdict", mock_save)
    monkeypatch.setattr("engine.judge.judge_run", mock_judge_run)

    engine = _make_engine(judge=True)
    result = engine.run("/tmp/prd.md", "/tmp/proj")

    mock_judge_run.assert_called_once()
    assert isinstance(mock_judge_run.call_args[0][0], RunResult)
    assert result.judge is not None
    assert result.judge["overall_pass"] is True
    assert abs(result.judge["score"] - 1.0) < 1e-9
    mock_save.assert_called_once()
    assert mock_save.call_args[0][0] == result.run_id
    assert mock_save.call_args[0][1] == result.judge


# (3) judge raising does not fail the run
def test_judge_exception_does_not_fail_run(monkeypatch):
    def _boom(r): raise RuntimeError("judge exploded")
    _patch_pipeline(monkeypatch)
    monkeypatch.setattr("engine.engine.save_judge_verdict", MagicMock())
    monkeypatch.setattr("engine.judge.judge_run", _boom)

    engine = _make_engine(judge=True)
    result = engine.run("/tmp/prd.md", "/tmp/proj")

    assert result.success is True
    assert result.judge is None


# (4) judge=False: judge_run never called
def test_judge_false_skips_judge_hook(monkeypatch):
    mock_judge_run = MagicMock()
    _patch_pipeline(monkeypatch)
    monkeypatch.setattr("engine.judge.judge_run", mock_judge_run)

    engine = _make_engine(judge=False)
    result = engine.run("/tmp/prd.md", "/tmp/proj")

    assert result.success is True
    mock_judge_run.assert_not_called()
    assert result.judge is None
