"""Regression test: archive default path must not hardcode /home/<user>.

The archive default must resolve to the actual user's home directory
(via os.path.expanduser) so it works for any user (e.g. nightmare).
"""

import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine.archive import _default_archive_dir


def test_archive_default_uses_expanduser():
    """The default archive dir must be under the actual user's home."""
    default = _default_archive_dir()
    home = os.path.expanduser("~")
    assert default.startswith(home), (
        f"archive default {default!r} must be under home {home!r}")
    assert "ace-results" in default


def test_archive_default_not_hardcoded_hunter():
    """The default must NOT be the hardcoded /home/<user> path."""
    default = _default_archive_dir()
    assert default != "/home/<user>/ace-results", (
        "archive default must not hardcode /home/<user>")


def test_archive_default_respects_env_override():
    """ACE_ARCHIVE_DIR env var must override the default."""
    original = os.environ.get("ACE_ARCHIVE_DIR")
    try:
        os.environ["ACE_ARCHIVE_DIR"] = "/tmp/custom-archive-dir"
        default = _default_archive_dir()
        assert default == "/tmp/custom-archive-dir"
    finally:
        if original is None:
            os.environ.pop("ACE_ARCHIVE_DIR", None)
        else:
            os.environ["ACE_ARCHIVE_DIR"] = original


def test_archive_default_matches_current_user():
    """The default must resolve to the CURRENT user's home, regardless of
    who runs the test (nightmare, <user>, etc.)."""
    default = _default_archive_dir()
    expected = os.path.join(os.path.expanduser("~"), "ace-results")
    assert default == expected, (
        f"expected {expected!r}, got {default!r}")
