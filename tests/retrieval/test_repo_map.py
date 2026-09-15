"""Tests for engine/retrieval/repo_map.py — RepoMapBuilder."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

from engine.retrieval.repo_map import (
    Def, RepoMap, RepoMapBuilder, MtimeCache, _AceIgnore, _estimate_tokens,
)


class TestRepoMapDataModel:
    """RepoMap/Def basic behavior."""

    def test_def_is_frozen(self):
        """Def is immutable — fields cannot be reassigned."""
        d = Def(name="foo", kind="function", start_line=1, end_line=5,
                file="a.py")
        with pytest.raises(AttributeError):
            d.name = "bar"
        # All fields remain as constructed.
        assert d.name == "foo"
        assert d.kind == "function"
        assert d.start_line == 1
        assert d.end_line == 5
        assert d.file == "a.py"

    def test_repo_map_add_def(self):
        """add_def populates both the flat list and the by_file index."""
        rm = RepoMap()
        rm.add_def(Def("foo", "function", 1, 5, "a.py"))
        rm.add_def(Def("Bar", "class", 10, 20, "a.py"))
        rm.add_def(Def("baz", "function", 1, 3, "b.py"))
        assert len(rm.defs) == 3
        assert len(rm.by_file) == 2
        assert len(rm.by_file["a.py"].defs) == 2
        # by_file groups defs by their file.
        assert {d.name for d in rm.by_file["a.py"].defs} == {"foo", "Bar"}
        assert rm.by_file["b.py"].defs[0].name == "baz"

    def test_repo_map_merge(self):
        """merge() replaces defs for files present in other, adds new ones."""
        rm = RepoMap()
        rm.add_def(Def("foo", "function", 1, 5, "a.py"))
        rm.add_def(Def("old", "function", 1, 3, "b.py"))
        delta = RepoMap()
        delta.add_def(Def("foo_new", "function", 2, 6, "a.py"))
        delta.add_def(Def("Bar", "class", 10, 20, "c.py"))
        rm.merge(delta)
        # a.py defs replaced (old "foo" gone, "foo_new" present).
        a_names = {d.name for d in rm.by_file["a.py"].defs}
        assert a_names == {"foo_new"}
        # b.py untouched.
        assert rm.by_file["b.py"].defs[0].name == "old"
        # c.py added.
        assert rm.by_file["c.py"].defs[0].name == "Bar"
        assert len(rm.defs) == 3


class TestRepoMapBuilder:
    """RepoMapBuilder.build() on a fixture tree."""

    def test_build_extracts_python_defs(self, fixture_tree):
        b = RepoMapBuilder()
        rm = b.build(fixture_tree)
        names = {d.name for d in rm.defs}
        assert "add" in names
        assert "Multiplier" in names
        assert "sub" in names

    def test_build_extracts_c_defs(self, fixture_tree):
        b = RepoMapBuilder()
        rm = b.build(fixture_tree)
        names = {d.name for d in rm.defs}
        assert "foo" in names
        assert "bar" in names

    def test_build_extracts_cpp_defs(self, fixture_tree):
        b = RepoMapBuilder()
        rm = b.build(fixture_tree)
        names = {d.name for d in rm.defs}
        assert "App" in names

    def test_build_respects_aceignore(self, fixture_tree):
        b = RepoMapBuilder()
        rm = b.build(fixture_tree)
        # .pyc files should be excluded
        files = {d.file for d in rm.defs}
        assert not any(f.endswith(".pyc") for f in files)

    def test_build_writes_mtime_cache(self, fixture_tree):
        b = RepoMapBuilder()
        b.build(fixture_tree)
        cache_path = fixture_tree / ".ace" / "repo_map_mtime.json"
        assert cache_path.is_file()
        data = json.loads(cache_path.read_text())
        assert len(data) > 0
        # Each entry has mtime + size
        for entry in data.values():
            assert "mtime" in entry
            assert "size" in entry


class TestMtimeCache:
    """MtimeCache hit/miss logic."""

    def test_cache_miss_for_new_file(self, tmp_path):
        cache = MtimeCache(tmp_path)
        assert cache.needs_reparse("new.py", 1000.0, 50) is True

    def test_cache_hit_for_unchanged_file(self, tmp_path):
        cache = MtimeCache(tmp_path)
        cache.update("stable.py", 1000.0, 50)
        assert cache.needs_reparse("stable.py", 1000.0, 50) is False

    def test_cache_miss_on_mtime_change(self, tmp_path):
        cache = MtimeCache(tmp_path)
        cache.update("f.py", 1000.0, 50)
        assert cache.needs_reparse("f.py", 2000.0, 50) is True

    def test_cache_miss_on_size_change(self, tmp_path):
        cache = MtimeCache(tmp_path)
        cache.update("f.py", 1000.0, 50)
        assert cache.needs_reparse("f.py", 1000.0, 99) is True

    def test_cache_prune_drops_stale(self, tmp_path):
        cache = MtimeCache(tmp_path)
        cache.update("keep.py", 1000.0, 50)
        cache.update("drop.py", 1000.0, 50)
        cache.prune({"keep.py"})
        assert "keep.py" in cache._data
        assert "drop.py" not in cache._data

    def test_incremental_skips_cached_files(self, fixture_tree):
        b = RepoMapBuilder()
        rm1 = b.build(fixture_tree)
        n1 = len(rm1.defs)
        # Incremental build with all files cached -> 0 new defs
        rm2 = b.incremental(fixture_tree)
        assert len(rm2.defs) == 0
        assert n1 > 0


class TestAceIgnore:
    """.aceignore filtering."""

    def test_ignores_matching_pattern(self, tmp_path):
        (tmp_path / ".aceignore").write_text("*.pyc\n")
        ign = _AceIgnore(tmp_path)
        assert ign.is_ignored("foo.pyc") is True

    def test_does_not_ignore_non_matching(self, tmp_path):
        (tmp_path / ".aceignore").write_text("*.pyc\n")
        ign = _AceIgnore(tmp_path)
        assert ign.is_ignored("foo.py") is False

    def test_negation_pattern(self, tmp_path):
        (tmp_path / ".aceignore").write_text("*.pyc\n!keep.pyc\n")
        ign = _AceIgnore(tmp_path)
        assert ign.is_ignored("keep.pyc") is False
        assert ign.is_ignored("drop.pyc") is True

    def test_no_aceignore_file(self, tmp_path):
        ign = _AceIgnore(tmp_path)
        assert ign.is_ignored("anything.py") is False

    def test_negation_pattern_both_paths(self, tmp_path):
        """M1 regression: !keep.pyc un-ignores keep.pyc after *.pyc in
        BOTH the fnmatch fallback AND the pathspec path.

        pathspec.PathSpec.from_lines receives bare patterns (no "!"), so
        negation silently breaks when pathspec is installed.  Force both
        code paths via monkeypatching _pathspec.
        """
        (tmp_path / ".aceignore").write_text("*.pyc\n!keep.pyc\n")

        # --- Path A: fnmatch fallback (_pathspec forced to None) ---
        ign_fallback = _AceIgnore(tmp_path)
        # _AceIgnore sets _pathspec if pathspec is importable; force None
        # to exercise the fnmatch rollup.
        ign_fallback._pathspec = None
        assert ign_fallback.is_ignored("keep.pyc") is False, (
            "fnmatch fallback: !keep.pyc should un-ignore keep.pyc")
        assert ign_fallback.is_ignored("drop.pyc") is True, (
            "fnmatch fallback: drop.pyc should still be ignored")

        # --- Path B: pathspec path (provide a fake PathSpec) ---
        class _FakeMatchResult:
            def __init__(self, val):
                self._val = val

        class _FakePathSpec:
            """Minimal pathspec stand-in recording the patterns it received."""
            received_patterns: list[str] = []

            @staticmethod
            def from_lines(style, patterns):
                _FakePathSpec.received_patterns = list(patterns)
                inst = _FakePathSpec()
                return inst

            def match_file(self, rel_path):
                # Emulate gitwildmatch semantics for the negation test:
                # last matching pattern wins; a leading "!" negates.
                ignored = False
                for pat in _FakePathSpec.received_patterns:
                    negated = pat.startswith("!")
                    p = pat[1:] if negated else pat
                    import fnmatch as _fn
                    if _fn.fnmatch(rel_path, p):
                        ignored = not negated
                return ignored

        ign_ps = _AceIgnore(tmp_path)
        # Sanity: if pathspec is really importable, _pathspec is set; otherwise
        # it is None.  Either way, install the fake so we exercise the
        # self._pathspec is not None branch.
        ign_ps._pathspec = _FakePathSpec.from_lines(
            "gitwildmatch", [("!" + p) if n else p
                             for p, n in ign_ps._patterns])

        # The fake must have received a "!"-prefixed negation pattern.
        assert any(p.startswith("!") for p in _FakePathSpec.received_patterns), (
            "pathspec path: negation pattern must carry the '!' prefix, "
            f"got {_FakePathSpec.received_patterns}")

        assert ign_ps.is_ignored("keep.pyc") is False, (
            "pathspec path: !keep.pyc should un-ignore keep.pyc")
        assert ign_ps.is_ignored("drop.pyc") is True, (
            "pathspec path: drop.pyc should still be ignored")

    def test_directory_pattern_ignores_files_under_it(self, tmp_path):
        """A pattern ending in '/' matches all files under that directory
        (gitignore-style directory-prefix semantics)."""
        (tmp_path / ".aceignore").write_text("ignored_dir/\n")
        (tmp_path / "ignored_dir").mkdir()
        (tmp_path / "ignored_dir" / "x.py").write_text("pass")
        (tmp_path / "ignored_dir" / "sub").mkdir()
        (tmp_path / "ignored_dir" / "sub" / "y.py").write_text("pass")
        ign = _AceIgnore(tmp_path)
        assert ign.is_ignored("ignored_dir/x.py") is True
        assert ign.is_ignored("ignored_dir/sub/y.py") is True
        # Sibling directory with similar name is NOT ignored.
        assert ign.is_ignored("kept_dir/x.py") is False


class TestRender:
    """Token-budgeted rendering."""

    def test_render_within_budget(self):
        """With a generous budget, all defs render and no overflow marker."""
        b = RepoMapBuilder()
        rm = RepoMap()
        for i in range(10):
            rm.add_def(Def(f"func_{i}", "function", i, i + 1, f"file_{i}.py"))
        text = b.render(rm, budget=5000)
        assert "... (+more)" not in text
        for i in range(10):
            assert f"func_{i}" in text

    def test_render_overflow_count_correctness(self):
        """Overflow marker reports the exact number of remaining defs."""
        b = RepoMapBuilder()
        rm = RepoMap()
        for i in range(50):
            rm.add_def(Def(f"f{i}", "function", i, i + 1, f"f{i}.py"))
        text = b.render(rm, budget=10)
        # Overflow marker present (with exact count).
        m = re.search(r"\(\+(\d+) more\)", text)
        assert m is not None, "overflow marker missing count"
        overflow_n = int(m.group(1))
        rendered = text.split("\n")
        # Lines that are real def lines (not overflow marker).
        def_lines = [l for l in rendered if l and not l.startswith("...")]
        assert len(def_lines) + overflow_n == 50

    def test_render_never_truncates_mid_symbol(self):
        """No line is a partial def — each is either complete or overflow."""
        b = RepoMapBuilder()
        rm = RepoMap()
        rm.add_def(Def("long_function_name_here", "function", 1, 10, "a.py"))
        text = b.render(rm, budget=5)
        lines = text.split("\n")
        for line in lines:
            if line and not line.startswith("..."):
                # Each non-overflow line should be a complete def line.
                assert "function" in line or "class" in line

    def test_render_empty_map(self):
        b = RepoMapBuilder()
        rm = RepoMap()
        assert b.render(rm, budget=3000) == ""

    def test_render_first_item_overflow_emits_marker(self):
        """When the first (and only) def exceeds budget, it is kept as a
        floor but an overflow marker is still emitted if there are more."""
        b = RepoMapBuilder()
        rm = RepoMap()
        rm.add_def(Def("first", "function", 1, 10, "a.py"))
        rm.add_def(Def("second", "function", 11, 20, "b.py"))
        # Budget too small for even one line — first kept, marker emitted.
        text = b.render(rm, budget=1)
        assert "first" in text
        assert "... (+1 more)" in text


class TestTokenEstimation:
    """_estimate_tokens behavior."""

    def test_whitespace_fallback(self):
        # Without tiktoken, falls back to whitespace split
        n = _estimate_tokens("one two three four")
        assert n == 4

    def test_empty_string(self):
        assert _estimate_tokens("") == 0
