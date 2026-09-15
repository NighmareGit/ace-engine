"""Engine context builder — thin bridge returning a compact context string."""
import os
_SKIP_DIRS = frozenset({"__pycache__", ".git", "node_modules", ".venv", "venv",
    ".tox", ".mypy_cache", ".pytest_cache", "dist", "build", ".eggs"})
_SKIP_EXTS = frozenset({".pyc", ".pyo", ".so", ".o", ".class", ".jar"})
_MAX_FILES, _MAX_BYTES, _MAX_INLINE, _MAX_RELEVANT = 50, 100_000, 2000, 8

def _build_file_tree(project_path):
    tree = {"root": project_path, "dirs": [], "files": []}
    if not os.path.isdir(project_path):
        return tree
    for entry in sorted(os.listdir(project_path)):
        if entry.startswith(".") or entry == "__pycache__":
            continue
        full = os.path.join(project_path, entry)
        (tree["dirs"] if os.path.isdir(full) else tree["files"]).append(entry)
    return tree

def _scan_project(project_path):
    """Single-pass: collect existing_code, imports map, and detect framework."""
    existing_code, modules = {}, {}
    framework = "unknown"
    count = 0
    blob_parts = []
    for root, dirs, files in os.walk(project_path):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for fname in sorted(files):
            if count >= _MAX_FILES:
                break
            if os.path.splitext(fname)[1] in _SKIP_EXTS:
                continue
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, project_path)
            try:
                with open(full, "r", errors="replace") as fh:
                    content = fh.read(_MAX_BYTES)
                existing_code[rel] = content
                blob_parts.append(content)
                count += 1
                if fname.endswith(".py"):
                    mod = rel.replace("/", ".").replace("\\", ".").removesuffix(".py")
                    if mod.endswith(".__init__"):
                        mod = mod[:-9]
                    modules[mod] = rel
            except OSError:
                pass
        if count >= _MAX_FILES:
            break
    blob = "\n".join(blob_parts)
    for name, marker in [("fastapi", "FastAPI"), ("flask", "Flask"),
                         ("django", "Django"), ("pytest", "pytest"),
                         ("uvicorn", "uvicorn")]:
        if marker in blob:
            framework = name
            break
    return existing_code, modules, framework

def _find_relevant(task, existing_code):
    """Return {rel_path: content} for files relevant to the task."""
    relevant = {}
    if task is None:
        return relevant
    mod = getattr(task, "module", "")
    for rel, content in existing_code.items():
        if mod and (rel == mod or rel.endswith(mod)):
            relevant[rel] = content
    for dep in getattr(task, "dependencies", []):
        for rel, content in existing_code.items():
            if dep.lower() in rel.lower() and rel not in relevant:
                relevant[rel] = content
    desc = getattr(task, "description", "").lower()
    for rel, content in existing_code.items():
        if rel in relevant:
            continue
        stem = os.path.splitext(os.path.basename(rel))[0].lower()
        if len(stem) >= 3 and stem in desc:
            relevant[rel] = content
        if len(relevant) >= _MAX_RELEVANT:
            break
    return relevant

class StringProxy(str):
    """str subclass carrying ProjectContext attributes for downstream compat."""
    file_tree: dict; relevant_files: dict; imports: dict
    framework: str; existing_code: dict

def build_context(project_dir, task, transport=None):
    """Build project context; returns compact string with attached attributes.
    Args: project_dir, task (or None), transport (ignored, API compat).
    Returns: StringProxy with .file_tree, .imports, .relevant_files, .framework, .existing_code.
    """
    file_tree = _build_file_tree(project_dir)
    existing_code, imports, framework = _scan_project(project_dir)
    relevant = _find_relevant(task, existing_code)
    parts = [f"Project root: {file_tree.get('root', '?')}",
             f"Framework: {framework}",
             f"Dirs: {', '.join(file_tree.get('dirs', [])) or '(none)'}",
             f"Files: {', '.join(file_tree.get('files', [])) or '(none)'}",
             f"Modules: {len(imports)}"]
    if relevant:
        parts.append(f"\nRelevant files ({len(relevant)}):")
        for rel, content in relevant.items():
            preview = content[:_MAX_INLINE]
            parts.append(f"\n--- {rel} ---\n{preview}")
            if len(content) > _MAX_INLINE:
                parts.append(f"... [+{len(content) - _MAX_INLINE} chars]")
    ctx = StringProxy("\n".join(parts))
    ctx.project_path = project_dir
    ctx.file_tree = file_tree
    ctx.relevant_files = relevant
    ctx.imports = imports
    ctx.framework = framework
    ctx.existing_code = existing_code
    return ctx
