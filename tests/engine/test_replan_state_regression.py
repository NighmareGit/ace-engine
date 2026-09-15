"""Regression: re-plan retry state transitions (run-1788913851).

Live failure: with TWO re-plan cycles, the second cycle called
pipeline.transition(State.VALIDATE) while state was already VALIDATE
(set by the first re-plan retry) -> InvalidTransition VALIDATE->VALIDATE.

Fix: re-entering generation from the re-plan loop transitions to
GENERATE first (legal from VALIDATE). Pre-fix this test reproduces the
crash; post-fix the task fails cleanly through normal exhaustion.
"""

import pytest

import engine.committer as cm
import engine.engine as eng_mod
from engine import Task
from engine.engine import Engine, EngineConfig

from tests.engine.test_t4_replan_brain import _FailingGenReplanTransport, tmp_db


def test_two_replan_cycles_no_invalid_transition(tmp_db, monkeypatch, tmp_path):
    transport = _FailingGenReplanTransport()

    cycles = {"n": 0}

    def fake_replan(self, task, pipeline, result, last_error, _stopped):
        cycles["n"] += 1
        if cycles["n"] > 2:
            return None  # ladder exhausted
        return {"applied": True, "cycle": cycles["n"]}

    monkeypatch.setattr(Engine, "_maybe_replan", fake_replan)
    monkeypatch.setattr(eng_mod, "parse_prd", lambda _: [
        Task(id="T01", title="T", description="d", module="app.py")])
    monkeypatch.setattr(cm, "_ensure_repo_exists", lambda repo, autocreate: None)

    cfg = EngineConfig(max_retries_generate=1, max_replans_per_task=3,
                       model_config="3070-qwen35-9b", archive_results=False)
    eng = Engine(transport=transport, config=cfg, on_event=lambda *a: None)
    result = eng.run("/tmp/prd.md", str(tmp_path / "proj"))

    # Pre-fix: cycle 2 crashed (InvalidTransition swallowed upstream), so
    # only 2 cycles ran. Post-fix the ladder runs to the cap (3).
    assert cycles["n"] == 3
    assert transport.generate_calls == 3  # initial + 2 re-plan retries (cap-1)
    assert not result.success
    assert [t.state for t in result.tasks] == ["FAILED"]
    # Pre-fix, the crash was swallowed and the task failed with the WRONG
    # error ("Invalid transition: VALIDATE → VALIDATE") instead of the
    # honest validation-exhaustion reason. Post-fix the error is honest.
    for t in result.tasks:
        assert "Invalid transition" not in (t.error_message or ""), \
            f"state-machine crash leaked into task error: {t.error_message}"
