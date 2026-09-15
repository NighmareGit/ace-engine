"""RepoMapBuilder — whole-workspace tree-sitter map with mtime cache.

Builds an immutable RepoMap snapshot of a workspace's top-level structure:
defs (name, kind, lines, file), refs (source, target, line), and per-file
summaries.  Results are cached via .ace/repo_map_mtime.json so unchanged
files are skipped on incremental rebuilds.

tree-sitter (>=0.23) with Python + C + C++ grammars is imported lazily.
If unavailable, a clear RuntimeError guides the user.  .aceignore filtering
mirrors .gitignore semantics (pathspec if available, else fnmatch).

Token-budgeted rendering uses tiktoken (cl100k_base) if available, else a
whitespace-proxy fallback.  Default budget is 3000 tokens (the ≤3k ceiling).
"""

from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ── Data model ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Def:
    """A top-level definition (function/class)."""
    name: str
    kind: str              # "function" | "class" | ...
    start_line: int
    end_line: int
    file: str


@dataclass(frozen=True)
class Ref:
    """A reference from one symbol to another."""
    source_file: str
    target_name: str
    line: int


@dataclass
class FileSummary:
    """Per-file summary of definitions."""
    file: str
    defs: list[Def] = field(default_factory=list)


@dataclass
class RepoMap:
    """Immutable snapshot of a workspace's top-level structure."""
    defs: list[Def] = field(default_factory=list)
    refs: list[Ref] = field(default_factory=list)
    by_file: dict[str, FileSummary] = field(default_factory=dict)

    def add_def(self, d: Def) -> None:
        self.defs.append(d)
        if d.file not in self.by_file:
            self.by_file[d.file] = FileSummary(file=d.file)
        self.by_file[d.file].defs.append(d)

    def merge(self, other: "RepoMap") -> None:
        """Merge *other* into this RepoMap in place.

        For files present in *other*, all existing defs for that file are
        replaced (not duplicated).  New files are added.  This supports
        incremental accumulation: call ``base.merge(builder.incremental(...))``
        to fold a delta into a full snapshot."""
        # Collect files that exist in other — their defs replace ours.
        other_files = {d.file for d in other.defs}
        # Drop existing defs for those files from both the flat list and
        # the by_file index.
        self.defs = [d for d in self.defs if d.file not in other_files]
        for f in other_files:
            self.by_file.pop(f, None)
        for d in other.defs:
            self.add_def(d)


# ── tree-sitter adapter (lazy import) ────────────────────────────────────────

# Extension -> language name, mapped to tree-sitter-language grammar packs.
_EXT_TO_LANG = {
    ".py": "python",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
}


class _TreeSitterAdapter:
    """Lazy-loading tree-sitter adapter.  Raises a clear error if the
    tree-sitter package or a required grammar is not installed."""

    def __init__(self) -> None:
        self._lang_cache: dict[str, object] = {}
        try:
            import tree_sitter  # noqa: F401
            self._ts = tree_sitter
        except ImportError as exc:
            raise RuntimeError(
                "tree-sitter>=0.23 is required for RepoMapBuilder. "
                "Install with: pip install tree-sitter>=0.23 "
                "tree-sitter-python tree-sitter-c tree-sitter-cpp"
            ) from exc

    def _get_language(self, lang_name: str) -> object:
        if lang_name in self._lang_cache:
            return self._lang_cache[lang_name]
        try:
            # tree-sitter >=0.23 exposes get_language / Language constructors
            # via the language pack's build_lib + tree_sitter.Language.
            import importlib
            mod = importlib.import_module(f"tree_sitter_{lang_name}")
            # Newer packs expose a `language` function returning a raw pointer.
            lang_ptr = mod.language()
            language = self._ts.Language(lang_ptr)
        except (ImportError, AttributeError, ModuleNotFoundError) as exc:
            raise RuntimeError(
                f"tree-sitter grammar for '{lang_name}' not installed. "
                f"Install with: pip install tree-sitter-{lang_name}"
            ) from exc
        self._lang_cache[lang_name] = language
        return language

    def parse(self, source: bytes, lang_name: str) -> object:
        """Parse *source* with the grammar for *lang_name*.  Returns a
        tree_sitter.Tree."""
        language = self._get_language(lang_name)
        parser = self._ts.Parser(language)
        return parser.parse(source)


# ── .aceignore filter ────────────────────────────────────────────────────────

class _AceIgnore:
    """Filter paths against an .aceignore file.  Uses pathspec if available,
    else a minimal fnmatch rollup with gitignore-style directory semantics.

    Directory patterns (trailing ``/``) match every file under that
    directory, e.g. ``ignored_dir/`` ignores ``ignored_dir/x.py`` and
    ``ignored_dir/sub/y.py``.  This mirrors gitignore's "a trailing / means
    the pattern only matches a directory, and all files under it" rule.
    """

    def __init__(self, project_path: Path) -> None:
        self._patterns: list[tuple[str, bool]] = []  # (pattern, negated)
        self._pathspec = None
        ignore_file = project_path / ".aceignore"
        if ignore_file.is_file():
            raw = ignore_file.read_text(encoding="utf-8")
            for line in raw.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                negated = line.startswith("!")
                pat = line[1:] if negated else line
                self._patterns.append((pat, negated))
        # Try pathspec for .gitignore-style semantics.
        if self._patterns:
            try:
                import pathspec  # noqa: F401
                # pathspec's gitwildmatch needs the literal "!" prefix on
                # negated patterns — dropping it (as a bare pattern) makes
                # the negation silently vanish when pathspec is installed.
                # The fnmatch fallback below handles negation on its own.
                self._pathspec = pathspec.PathSpec.from_lines(
                    "gitwildmatch",
                    [("!" + p) if negated else p
                     for p, negated in self._patterns])
            except ImportError:
                self._pathspec = None

    def is_ignored(self, rel_path: str) -> bool:
        """Return True if *rel_path* (POSIX-style) matches .aceignore.

        When pathspec is available it handles directory patterns natively.
        The fnmatch fallback implements directory-prefix semantics: a
        pattern ending in ``/`` matches *rel_path* if it starts with that
        prefix (e.g. pattern ``ignored_dir/`` matches ``ignored_dir/x.py``).
        """
        if not self._patterns:
            return False
        if self._pathspec is not None:
            return self._pathspec.match_file(rel_path)
        # fnmatch rollup: last matching pattern wins (gitignore semantics).
        # Directory patterns (trailing '/') use prefix matching.
        ignored = False
        for pat, negated in self._patterns:
            if pat.endswith("/"):
                # Directory pattern: match any file under that directory.
                prefix = pat  # keep trailing '/' — rel_path starts with it
                if rel_path.startswith(prefix) or rel_path == pat.rstrip("/"):
                    ignored = not negated
            elif fnmatch.fnmatch(rel_path, pat) or fnmatch.fnmatch(
                    os.path.basename(rel_path), pat):
                ignored = not negated
        return ignored


# ── Mtime cache ──────────────────────────────────────────────────────────────

class MtimeCache:
    """Read/write .ace/repo_map_mtime.json.  Format:
    {rel_path: {mtime: float, size: int}}."""

    def __init__(self, project_path: Path) -> None:
        self._path = project_path / ".ace" / "repo_map_mtime.json"
        self._data: dict[str, dict] = {}
        if self._path.is_file():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def needs_reparse(self, rel_path: str, mtime: float, size: int) -> bool:
        """Return True if *rel_path* has changed since last parse."""
        entry = self._data.get(rel_path)
        if entry is None:
            return True
        return entry.get("mtime") != mtime or entry.get("size") != size

    def update(self, rel_path: str, mtime: float, size: int) -> None:
        self._data[rel_path] = {"mtime": mtime, "size": size}

    def prune(self, valid_paths: set[str]) -> None:
        """Drop entries for files that no longer exist."""
        for key in list(self._data.keys()):
            if key not in valid_paths:
                del self._data[key]

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data, indent=2),
                              encoding="utf-8")


# ── Token counting ───────────────────────────────────────────────────────────

def _estimate_tokens(text: str) -> int:
    """Estimate token count.  Uses tiktoken (cl100k_base) if available,
    else a whitespace-based fallback (~4 chars/token heuristic)."""
    try:
        import tiktoken  # noqa: F401
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except ImportError:
        # Whitespace proxy: split on whitespace, count non-empty tokens.
        # This underestimates slightly but is deterministic and dependency-free.
        return len(text.split())


# ── RepoMapBuilder ───────────────────────────────────────────────────────────

class RepoMapBuilder:
    """Build a RepoMap for a workspace.

    Usage::

        builder = RepoMapBuilder()
        repo_map = builder.build(Path("."))
        text = builder.render(repo_map, budget=3000)
    """

    def __init__(self) -> None:
        self._ts_adapter: Optional[_TreeSitterAdapter] = None
        self._ts_available: Optional[bool] = None

    def _get_adapter(self) -> Optional[_TreeSitterAdapter]:
        """Lazily initialise the tree-sitter adapter.  Returns None if
        tree-sitter is not installed (graceful degradation)."""
        if self._ts_available is not None:
            return self._ts_adapter
        try:
            self._ts_adapter = _TreeSitterAdapter()
            self._ts_available = True
        except RuntimeError:
            self._ts_adapter = None
            self._ts_available = False
        return self._ts_adapter

    def build(self, project_path: Path) -> RepoMap:
        """Walk *project_path*, parse all supported files, return a RepoMap.

        The mtime cache is updated during the build (for use by future
        incremental() calls) but does not skip parsing — build() always
        produces a complete snapshot."""
        project_path = Path(project_path)
        aceignore = _AceIgnore(project_path)
        cache = MtimeCache(project_path)
        repo_map = RepoMap()
        parsed_paths: set[str] = set()

        for root, dirs, files in os.walk(project_path):
            # Skip hidden/VCS dirs in-place.
            dirs[:] = [d for d in dirs
                       if not d.startswith(".") and d != "__pycache__"
                       and d != "node_modules"]
            for fname in sorted(files):
                full = Path(root) / fname
                rel = full.relative_to(project_path).as_posix()
                if aceignore.is_ignored(rel):
                    continue
                ext = fname[fname.rfind("."):]
                if ext not in _EXT_TO_LANG:
                    continue
                stat = full.stat()
                parsed_paths.add(rel)
                self._parse_file(full, rel, ext, repo_map)
                cache.update(rel, stat.st_mtime, stat.st_size)

        cache.prune(parsed_paths)
        cache.save()
        return repo_map

    def incremental(self, project_path: Path,
                    cache: Optional[MtimeCache] = None) -> RepoMap:
        """Build incrementally using the mtime cache (delta-only).

        Only files whose mtime or size changed since the last build are
        re-parsed; unchanged files are skipped entirely.  The returned
        RepoMap therefore contains **only the changed defs** (a delta),
        not a full snapshot.  To accumulate a complete map across calls,
        merge each delta into a base RepoMap via ``base.merge(delta)``
        (which replaces defs for re-parsed files and adds new ones).

        Missing entries are parsed; stale entries are dropped from the
        cache (but their defs are NOT removed from any existing RepoMap
        the caller holds — that is the caller's responsibility via
        merge()).
        """
        project_path = Path(project_path)
        aceignore = _AceIgnore(project_path)
        if cache is None:
            cache = MtimeCache(project_path)
        repo_map = RepoMap()
        parsed_paths: set[str] = set()

        for root, dirs, files in os.walk(project_path):
            dirs[:] = [d for d in dirs
                       if not d.startswith(".") and d != "__pycache__"
                       and d != "node_modules"]
            for fname in sorted(files):
                full = Path(root) / fname
                rel = full.relative_to(project_path).as_posix()
                if aceignore.is_ignored(rel):
                    continue
                ext = fname[fname.rfind("."):]
                if ext not in _EXT_TO_LANG:
                    continue
                stat = full.stat()
                parsed_paths.add(rel)
                if not cache.needs_reparse(rel, stat.st_mtime, stat.st_size):
                    continue
                self._parse_file(full, rel, ext, repo_map)
                cache.update(rel, stat.st_mtime, stat.st_size)

        cache.prune(parsed_paths)
        cache.save()
        return repo_map

    def _parse_file(self, full: Path, rel: str, ext: str,
                    repo_map: RepoMap) -> None:
        """Parse a single file and populate *repo_map*."""
        adapter = self._get_adapter()
        try:
            source = full.read_bytes()
        except OSError:
            return
        if adapter is None:
            # Graceful degradation: extract function/class defs via regex
            # when tree-sitter is unavailable.
            self._parse_fallback(source.decode("utf-8", errors="replace"),
                                 rel, repo_map)
            return
        lang_name = _EXT_TO_LANG[ext]
        try:
            tree = adapter.parse(source, lang_name)
        except RuntimeError:
            self._parse_fallback(source.decode("utf-8", errors="replace"),
                                 rel, repo_map)
            return
        self._walk_tree(tree.root_node, rel, source, repo_map)

    def _walk_tree(self, node, rel: str, source: bytes,
                   repo_map: RepoMap) -> None:
        """Recursively walk the AST and extract top-level defs."""
        target_kinds = {"function_definition", "function_def",
                        "method_definition", "class_definition", "class_def"}
        kind_map = {
            "function_definition": "function",
            "function_def": "function",
            "method_definition": "function",
            "class_definition": "class",
            "class_def": "class",
        }
        if node.type in target_kinds:
            name = self._extract_name(node, source)
            if name:
                repo_map.add_def(Def(
                    name=name,
                    kind=kind_map.get(node.type, node.type),
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    file=rel,
                ))
        for child in node.children:
            self._walk_tree(child, rel, source, repo_map)

    def _extract_name(self, node, source: bytes) -> Optional[str]:
        """Extract the identifier name from a def node."""
        for child in node.children:
            if child.type == "identifier":
                return source[child.start_byte:child.end_byte].decode(
                    "utf-8", errors="replace")
        return None

    def _parse_fallback(self, text: str, rel: str, repo_map: RepoMap) -> None:
        """Regex-based fallback when tree-sitter is unavailable.
        Extracts top-level function/class definitions for Python,
        C, and C++."""
        import re
        # Python-style: "def foo(" or "class Foo"
        for m in re.finditer(
                r"^(def|class)\s+([A-Za-z_]\w*)\s*[\(:]", text, re.MULTILINE):
            kind = "function" if m.group(1) == "def" else "class"
            line_no = text[:m.start()].count("\n") + 1
            repo_map.add_def(Def(
                name=m.group(2), kind=kind,
                start_line=line_no, end_line=line_no, file=rel,
            ))
        # C/C++ functions: "int foo(" or "void bar(" etc.
        for m in re.finditer(
                r"^(?:int|void|char|float|double|long|short|unsigned|bool|"
                r"static|inline|virtual)\s+"
                r"([A-Za-z_]\w*)\s*\([^)]*\)\s*\{",
                text, re.MULTILINE):
            name = m.group(1)
            if name in ("if", "while", "for", "switch", "return"):
                continue
            line_no = text[:m.start()].count("\n") + 1
            repo_map.add_def(Def(
                name=name, kind="function",
                start_line=line_no, end_line=line_no, file=rel,
            ))
        # C++ class: "class Foo {" (avoid double-counting Python classes)
        if rel.endswith((".cpp", ".cc", ".cxx", ".hpp")):
            for m in re.finditer(
                    r"^class\s+([A-Za-z_]\w*)\s*[\{:]",
                    text, re.MULTILINE):
                line_no = text[:m.start()].count("\n") + 1
                repo_map.add_def(Def(
                    name=m.group(1), kind="class",
                    start_line=line_no, end_line=line_no, file=rel,
                ))

    def render(self, repo_map: RepoMap, budget: int = 3000) -> str:
        """Render *repo_map* as a compact text map within *budget* tokens.

        Iterates symbols in file order, appending lines until the token
        estimate reaches *budget*.  Overflow symbols are replaced with
        '... (+N more)'.  Never truncates mid-symbol.

        Budget asymmetry: the first line is always emitted even when it
        alone exceeds *budget* — an empty map is never useful, so the
        highest-priority symbol is kept as a floor.  When this happens an
        overflow marker is still appended so callers can detect the
        truncation (consistent with assemble_retrieved()).
        """
        parts: list[str] = []
        current_tokens = 0
        rendered_defs = 0
        total_defs = len(repo_map.defs)
        budget_exceeded = False

        for d in repo_map.defs:
            line = f"{d.file}:{d.start_line}  {d.kind} {d.name}"
            line_tokens = _estimate_tokens(line)
            if current_tokens + line_tokens > budget and rendered_defs > 0:
                remaining = total_defs - rendered_defs
                parts.append(f"... (+{remaining} more)")
                budget_exceeded = True
                break
            if current_tokens + line_tokens > budget and rendered_defs == 0:
                # First item exceeds budget — keep it (floor), but record
                # that the budget was exceeded so we emit the marker.
                budget_exceeded = True
            parts.append(line)
            current_tokens += line_tokens
            rendered_defs += 1
            if budget_exceeded:
                # First item already over budget — emit overflow marker
                # after it and stop.
                remaining = total_defs - rendered_defs
                if remaining > 0:
                    parts.append(f"... (+{remaining} more)")
                break

        return "\n".join(parts)
