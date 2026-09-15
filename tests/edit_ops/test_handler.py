"""Unit + integration tests for engine/edit_ops/handler.py — EditTaskHandler.

Mock transport only — no GPU, no inference, no network calls.
Mirrors mock patterns in tests/research/ (MagicMock transport with
curl_beellama returning a flat dict).
"""

import sys
import os
import tempfile
from unittest.mock import MagicMock

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine import Task
from engine.edit_ops.handler import EditTaskHandler, EditGenerateResult
from engine.edit_ops.apply import apply_blocks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_transport_with(content, project_dir=None):
    """Return a MagicMock transport that performs real local file I/O.

    curl_beellama returns a flat dict with the given content.
    run_command executes base64-decoded writes and cat reads against the
    local filesystem (so commit-gate read-back sees what was written).
    """
    import base64
    t = MagicMock()
    t.curl_beellama.return_value = {
        "content": content,
        "total_tokens": 100,
        "thinking_tokens": 0,
        "finish_reason": "stop",
        "reasoning_content": "",
    }

    def _run_command(cmd, timeout=30):
        # Handle: mkdir -p $(dirname <path>) && echo <b64> | base64 -d > <path>
        if "base64 -d >" in cmd:
            parts = cmd.split("base64 -d >")
            b64_part = parts[0].strip().rsplit("echo ", 1)[-1].strip()
            path = parts[1].strip()
            # Resolve relative paths against project_dir.
            if project_dir and not os.path.isabs(path):
                path = os.path.join(project_dir, path)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(base64.b64decode(b64_part))
            return ("", "", 0)
        if cmd.strip().startswith("cat "):
            path = cmd.strip()[4:].strip()
            if project_dir and not os.path.isabs(path):
                path = os.path.join(project_dir, path)
            try:
                with open(path) as f:
                    return (f.read(), "", 0)
            except FileNotFoundError:
                return ("", "No such file", 1)
        return ("", "", 0)

    t.run_command.side_effect = _run_command
    return t


class _MockEngine:
    def __init__(self, transport):
        self.transport = transport
        self.config = MagicMock()
        self.config.max_tokens_by_role = {"coder": 4096}
        self.config.model_config = "3090-test"


class _MockPipeline:
    def __init__(self, run_id="test-run"):
        self.run_id = run_id
        self.task_retries = {}


def _edit_task(task_id="E1", module="src/target.py"):
    return Task(
        id=task_id, title="fix the bug",
        description="replace old_func with new_func in target",
        module=module, task_type="edit",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestHandlerDispatch:
    def test_edit_task_resolves_to_edit_handler(self):
        from engine.task_handler import _resolve_handler, CodeTaskHandler
        task = _edit_task()
        engine = _MockEngine(MagicMock())
        handler = _resolve_handler(task, engine)
        assert isinstance(handler, EditTaskHandler)
        assert not isinstance(handler, CodeTaskHandler)

    def test_code_task_unchanged(self):
        from engine.task_handler import _resolve_handler, CodeTaskHandler
        task = Task(id="C1", title="x", description="y", module="a.py",
                    task_type="implementation")
        handler = _resolve_handler(task, _MockEngine(MagicMock()))
        assert isinstance(handler, CodeTaskHandler)


class TestHandlerHappyPath:
    def test_full_pipeline_commits(self):
        """Happy path: generate -> extract -> validate -> apply -> write -> commit -> COMMIT."""
        project = tempfile.mkdtemp()
        target = os.path.join(project, "src", "target.py")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        original = "def old_func():\n    return 1\n"
        with open(target, "w") as f:
            f.write(original)

        # LLM response with a valid SEARCH/REPLACE block.
        llm_response = f"""\
<<<<<<< SEARCH
def old_func():
    return 1
=======
def new_func():
    return 2
>>>>>>> REPLACE
"""
        transport = _mock_transport_with(llm_response, project_dir=project)
        engine = _MockEngine(transport)
        handler = EditTaskHandler(engine)
        pipeline = _MockPipeline()
        task = _edit_task()

        result = handler.run(task, pipeline, project)

        assert result.state == "COMMIT", f"expected COMMIT, got {result.state}: {result.error_message}"
        assert result.error_message is None
        assert result.attempts == 1

        # Verify the file was written with the new content (via transport I/O).
        with open(target) as f:
            disk_content = f.read()
        assert "def new_func():" in disk_content
        assert "return 2" in disk_content

    def test_validation_failure_retries(self):
        """If the first response fails validation, the handler retries."""
        project = tempfile.mkdtemp()
        target = os.path.join(project, "src", "target.py")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as f:
            f.write("def old_func():\n    return 1\n")

        # Response whose old_string does NOT match the file (zero-match).
        llm_response = """\
<<<<<<< SEARCH
this text is not in the file
=======
replacement
>>>>>>> REPLACE
"""
        transport = _mock_transport_with(llm_response)
        engine = _MockEngine(transport)
        handler = EditTaskHandler(engine)
        pipeline = _MockPipeline()
        task = _edit_task()

        result = handler.run(task, pipeline, project)

        # Should exhaust retries and fail.
        assert result.state == "FAILED"
        assert "retries exhausted" in (result.error_message or "")

    def test_multi_match_fails_loud(self):
        """Multi-match (old_string appears >1x) fails with match count in error."""
        project = tempfile.mkdtemp()
        target = os.path.join(project, "src", "target.py")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as f:
            f.write("dup\ndup\ndup\n")

        llm_response = """\
<<<<<<< SEARCH
dup
=======
unique
>>>>>>> REPLACE
"""
        transport = _mock_transport_with(llm_response)
        engine = _MockEngine(transport)
        handler = EditTaskHandler(engine)
        pipeline = _MockPipeline()
        task = _edit_task()

        result = handler.run(task, pipeline, project)
        assert result.state == "FAILED"


class TestApplyBlocksSeam:
    """Direct tests of the pure seam (mirrors test_apply_blocks.py but via handler context)."""

    def test_apply_blocks_exact(self):
        content = "a = 1\nb = 2\n"
        from engine.edit_ops.extract import EditBlock
        blocks = [EditBlock(path="f.py", old_string="a = 1", new_string="a = 10")]
        new, errors = apply_blocks(content, blocks)
        assert new == "a = 10\nb = 2\n"
        assert errors == []

    def test_apply_blocks_multi_match_error_has_count(self):
        content = "x\nx\nx\n"
        from engine.edit_ops.extract import EditBlock
        blocks = [EditBlock(path="f.py", old_string="x", new_string="y")]
        new, errors = apply_blocks(content, blocks)
        assert new is None
        assert "3" in errors[0]


class TestPathTraversal:
    """Path traversal is rejected BEFORE any filesystem touch (stage 5 < stage 2)."""

    def _run_with_target(self, module_path):
        project = tempfile.mkdtemp()
        transport = _mock_transport_with("", project_dir=project)
        engine = _MockEngine(transport)
        handler = EditTaskHandler(engine)
        pipeline = _MockPipeline()
        task = Task(id="E1", title="x", description="y",
                    module=module_path, task_type="edit")
        return handler.run(task, pipeline, project)

    def test_dotdot_rejected(self):
        result = self._run_with_target("../../etc/passwd")
        assert result.state == "FAILED"
        assert "path traversal" in (result.error_message or "")

    def test_absolute_rejected(self):
        result = self._run_with_target("/etc/passwd")
        assert result.state == "FAILED"
        assert "path traversal" in (result.error_message or "")

    def test_tilde_rejected(self):
        result = self._run_with_target("~/.ssh/id_rsa")
        assert result.state == "FAILED"
        assert "path traversal" in (result.error_message or "")


class TestMetricsRateMath:
    """Test first_apply_rate math directly (no DB needed for logic)."""

    def test_rate_calculation(self):
        import tempfile
        db = tempfile.mktemp(suffix=".db")
        from engine.state import init_db
        init_db(db_path=db)
        from engine.edit_ops.metrics import first_apply_rate, record_edit_op_result
        # 3 first-apply successes + 1 failure = 0.75
        record_edit_op_result("r1", "T1", 1, True, None, db)
        record_edit_op_result("r1", "T2", 1, True, None, db)
        record_edit_op_result("r1", "T3", 1, True, None, db)
        record_edit_op_result("r1", "T4", 1, False, 3, db)
        assert first_apply_rate("r1", db_path=db) == 0.75
        os.unlink(db)

    def test_rate_empty_run_is_zero(self):
        import tempfile
        db = tempfile.mktemp(suffix=".db")
        from engine.state import init_db
        init_db(db_path=db)
        from engine.edit_ops.metrics import first_apply_rate
        assert first_apply_rate("no-such-run", db_path=db) == 0.0
        os.unlink(db)

    def test_rate_all_success(self):
        import tempfile
        db = tempfile.mktemp(suffix=".db")
        from engine.state import init_db
        init_db(db_path=db)
        from engine.edit_ops.metrics import first_apply_rate, record_edit_op_result
        record_edit_op_result("r1", "T1", 1, True, None, db)
        record_edit_op_result("r1", "T2", 1, True, None, db)
        assert first_apply_rate("r1", db_path=db) == 1.0
        os.unlink(db)
