"""Unit tests for engine/edit_ops/apply.py — the pure seam.

Deterministic: no I/O, no LLM, no network.  Pure (str, blocks) -> (str, errors).
Covers: exact apply, multi-block sequential, multi-match fail WITH count,
zero-match fail, near-miss whitespace fail, whole-file replace, empty blocks.
"""

import sys
import os

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine.edit_ops.apply import apply_blocks
from engine.edit_ops.extract import EditBlock


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestExactApply:
    def test_single_block_apply(self):
        content = "line one\nline two\nline three"
        blocks = [EditBlock(path="f.py", old_string="line two", new_string="LINE TWO")]
        new, errors = apply_blocks(content, blocks)
        assert errors == []
        assert new == "line one\nLINE TWO\nline three"

    def test_multi_block_sequential(self):
        content = "A B C"
        blocks = [
            EditBlock(path="f.py", old_string="A", new_string="X"),
            EditBlock(path="f.py", old_string="B", new_string="Y"),
        ]
        new, errors = apply_blocks(content, blocks)
        assert errors == []
        assert new == "X Y C"

    def test_second_block_sees_first_result(self):
        # Block 2 replaces text introduced by block 1.
        content = "start"
        blocks = [
            EditBlock(path="f.py", old_string="start", new_string="middle"),
            EditBlock(path="f.py", old_string="middle", new_string="end"),
        ]
        new, errors = apply_blocks(content, blocks)
        assert errors == []
        assert new == "end"


class TestMultiMatchFail:
    def test_multi_match_fails_with_count(self):
        content = "foo bar foo baz foo"  # "foo" appears 3 times
        blocks = [EditBlock(path="f.py", old_string="foo", new_string="FOO")]
        new, errors = apply_blocks(content, blocks)
        assert new is None
        assert len(errors) == 1
        assert "3" in errors[0], f"error should report match count 3: {errors[0]}"
        assert "block 0" in errors[0]

    def test_multi_match_error_mentions_narrow_anchor(self):
        content = "x x x"
        blocks = [EditBlock(path="f.py", old_string="x", new_string="y")]
        new, errors = apply_blocks(content, blocks)
        assert new is None
        # Design: error should guide the caller to narrow the anchor.
        assert "Narrow" in errors[0] or "exactly 1" in errors[0]


class TestZeroMatchFail:
    def test_zero_match_fails(self):
        content = "hello world"
        blocks = [EditBlock(path="f.py", old_string="notpresent", new_string="replacement")]
        new, errors = apply_blocks(content, blocks)
        assert new is None
        assert len(errors) == 1
        assert "not found" in errors[0] or "count=0" in errors[0]


class TestNearMissWhitespace:
    def test_trailing_space_difference_fails(self):
        # Exact match only: trailing space in old_string makes it not match.
        content = "line one\nline two\n"
        blocks = [EditBlock(path="f.py", old_string="line two ", new_string="replaced")]
        new, errors = apply_blocks(content, blocks)
        assert new is None, "near-miss whitespace should fail exact match"


class TestWholeFileReplace:
    def test_block_spans_whole_file(self):
        content = "the entire file content"
        blocks = [EditBlock(path="f.py", old_string=content, new_string="replaced entirely")]
        new, errors = apply_blocks(content, blocks)
        assert errors == []
        assert new == "replaced entirely"


class TestEmptyBlocks:
    def test_empty_block_list_returns_original(self):
        content = "unchanged"
        new, errors = apply_blocks(content, [])
        assert errors == []
        assert new == "unchanged"


class TestDictBlocks:
    def test_dict_style_blocks_accepted(self):
        # apply_blocks supports dict-style blocks (the validator/handler may pass either form).
        content = "foo bar"
        blocks = [{"path": "f.py", "old_string": "foo", "new_string": "FOO"}]
        new, errors = apply_blocks(content, blocks)
        assert errors == []
        assert new == "FOO bar"


class TestAtomicRollback:
    def test_first_failing_block_aborts_all(self):
        # Block 0 succeeds, block 1 fails -> entire apply returns None.
        content = "keep me\nreplace me\nambiguous ambiguous"
        blocks = [
            EditBlock(path="f.py", old_string="replace me", new_string="REPLACED"),
            EditBlock(path="f.py", old_string="ambiguous", new_string="X"),  # 2 matches
        ]
        new, errors = apply_blocks(content, blocks)
        assert new is None
        assert len(errors) == 1
        assert "block 1" in errors[0]
