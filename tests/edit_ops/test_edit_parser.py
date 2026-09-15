"""Unit tests for engine/edit_ops/extract.py — Aider fence + XML fallback parsing.

Deterministic: no LLM, no network, no I/O.  Pure string-in -> EditOp-out.
"""

import sys
import os

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine.edit_ops.extract import EditOpExtractor, EditBlock, EditOp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

AIDER_RESPONSE = """\
Here is the fix:

```python
path/to/file.py
<<<<<<< SEARCH
old line one
old line two
=======
new line one
new line two
>>>>>>> REPLACE
```

Done.
"""

AIDER_MULTI_RESPONSE = """\
<<<<<<< SEARCH
first old
=======
first new
>>>>>>> REPLACE

<<<<<<< SEARCH
second old
=======
second new
>>>>>>> REPLACE
"""

XML_RESPONSE = """\
<edit>
<old>old text here</old>
<new>new text here</new>
</edit>
"""

MALFORMED_UNCLOSED = """\
<<<<<<< SEARCH
some old text
=======
some new text
"""

EMPTY_BLOCK_RESPONSE = """\
<<<<<<< SEARCH
=======
>>>>>>> REPLACE
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestAiderFenceParsing:
    def test_single_block_parsed(self):
        op = EditOpExtractor.extract(AIDER_RESPONSE, "path/to/file.py")
        assert isinstance(op, EditOp)
        assert len(op.blocks) == 1
        assert op.blocks[0].old_string == "old line one\nold line two"
        assert op.blocks[0].new_string == "new line one\nnew line two"
        assert op.blocks[0].path == "path/to/file.py"

    def test_multiple_blocks_parsed(self):
        op = EditOpExtractor.extract(AIDER_MULTI_RESPONSE, "f.py")
        assert len(op.blocks) == 2
        assert op.blocks[0].old_string == "first old"
        assert op.blocks[0].new_string == "first new"
        assert op.blocks[1].old_string == "second old"
        assert op.blocks[1].new_string == "second new"

    def test_raw_response_preserved(self):
        op = EditOpExtractor.extract(AIDER_RESPONSE, "f.py")
        assert op.raw_response == AIDER_RESPONSE
        assert op.target_file == "f.py"


class TestXmlFallback:
    def test_xml_fallback_parsed(self):
        op = EditOpExtractor.extract(XML_RESPONSE, "f.txt")
        assert len(op.blocks) == 1
        assert op.blocks[0].old_string == "old text here"
        assert op.blocks[0].new_string == "new text here"

    def test_aider_takes_priority_over_xml(self):
        # When both formats are present, Aider wins (parsed first).
        combined = AIDER_RESPONSE + "\n" + XML_RESPONSE
        op = EditOpExtractor.extract(combined, "f.py")
        # At least the Aider block is present.
        assert any(b.old_string == "old line one\nold line two" for b in op.blocks)


class TestDegenerateCases:
    def test_empty_response_yields_no_blocks(self):
        op = EditOpExtractor.extract("", "f.py")
        assert op.blocks == []

    def test_no_markers_yields_no_blocks(self):
        op = EditOpExtractor.extract("just some random text without fences", "f.py")
        assert op.blocks == []

    def test_empty_old_string_skipped(self):
        # A block with empty old_string is skipped (warning logged).
        op = EditOpExtractor.extract(EMPTY_BLOCK_RESPONSE, "f.py")
        assert op.blocks == []

    def test_unclosed_fence_does_not_crash(self):
        # Degenerate: unclosed fence must not raise.
        op = EditOpExtractor.extract(MALFORMED_UNCLOSED, "f.py")
        # The strict regex won't match an unclosed fence; result is empty, not a crash.
        assert isinstance(op, EditOp)
        assert op.blocks == []
