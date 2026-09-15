"""Engine validation — 4-stage validation pipeline for generated code."""

import ast
import os
import sys
import threading
from dataclasses import dataclass


def validate(code, context, task=None):
    """
    Run validation stages + D3 targeting.

    Args:
        code: GeneratedCode dataclass
        context: Context dataclass with imports dict
        task: optional Task for D3 targeting stage

    Returns:
        ValidationResult with passed (bool) and stages (list of dicts)
    """
    all_stages = []
    all_passed = True

    for filename, code_str in code.files.items():
        # Stage 0: Empty code check — empty files are never valid output
        passed, error = check_empty(code_str)
        all_stages.append({"stage": "empty", "file": filename, "passed": passed, "error": error})
        if not passed:
            return ValidationResult(passed=False, stages=all_stages)

        # Stage 1: AST parse
        passed, error = check_ast(code_str)
        all_stages.append({"stage": "ast", "file": filename, "passed": passed, "error": error})
        if not passed:
            return ValidationResult(passed=False, stages=all_stages)

        # Stage 2: Syntax check
        passed, error = check_syntax(code_str)
        all_stages.append({"stage": "syntax", "file": filename, "passed": passed, "error": error})
        if not passed:
            return ValidationResult(passed=False, stages=all_stages)

        # Stage 3: Import resolution (D1: 3-tier classifier with dag_files).
        _dag_files = getattr(context, "dag_files", None)
        passed, error = check_imports(code_str, context.imports,
                                      dag_files=_dag_files)
        all_stages.append({"stage": "imports", "file": filename, "passed": passed, "error": error})
        if not passed:
            return ValidationResult(passed=False, stages=all_stages)

        # Stage 4: Execution test — restricted namespace, inherited __builtins__
        # Uses a fresh namespace dict (isolates variable scope) while inheriting
        # builtins from the parent so stdlib functions like print() work.
        namespace = {"__name__": "__test__", "__builtins__": __builtins__}
        passed, error = _with_timeout(
            lambda: check_execution(code_str, namespace,
                                    getattr(context, "project_path", None)),
            5, "exec validation")
        all_stages.append({"stage": "execution", "file": filename, "passed": passed, "error": error})
        if not passed:
            all_passed = False

    # D3: Targeting rail
    if task is not None:
        passed, error = check_targeting(task, context, code=code)
        all_stages.append({"stage": "targeting", "file": None,
                           "passed": passed, "error": error})
        if not passed:
            all_passed = False

    return ValidationResult(passed=all_passed, stages=all_stages)


def check_targeting(task, context, code=None):
    """D3 targeting rail: every file declared in task.files must be present in
    the generated code AND not be an untouched D2 stub."""
    from engine.orchestrator.stubs import is_stub

    declared = list(task.files) if getattr(task, "files", None) else []
    if not declared:
        declared = [task.module] if getattr(task, "module", None) else []
    if not declared:
        return (True, None)

    generated = code.files if code else {}
    missing = []
    stubs = []
    for fpath in declared:
        if fpath not in generated:
            missing.append(fpath)
        elif is_stub(generated[fpath]):
            stubs.append(fpath)

    if not missing and not stubs:
        return (True, None)

    parts = []
    if missing:
        parts.append(f"missing file(s): {', '.join(missing)}")
    if stubs:
        parts.append(f"untouched stub(s): {', '.join(stubs)}")
    return (False, f"Targeting rail: declared file(s) not implemented — {'; '.join(parts)}.")


def check_empty(code_str):
    """Reject empty or whitespace-only code. Returns (passed, error_msg)."""
    if not code_str or not code_str.strip():
        return (False, "Code is empty or whitespace-only")
    return (True, None)


def check_ast(code_str):
    """Parse code with ast.parse. Returns (passed, error_msg)."""
    try:
        ast.parse(code_str)
        return (True, None)
    except SyntaxError as e:
        return (False, str(e))


def check_syntax(code_str):
    """Compile code. Returns (passed, error_msg)."""
    try:
        compile(code_str, '<generated>', 'exec')
        return (True, None)
    except SyntaxError as e:
        return (False, str(e))


def check_imports(code_str, known_imports, dag_files: set[str] | None = None):
    """
    Parse import statements via ast, verify each top-level module is:
    1. A stdlib module (sys.stdlib_module_names, Python 3.10+), OR
    2. A file/directory in the project (known_imports keys), OR
    3. A dag-sibling — module maps to a file declared in any task's files[]
       of the current DAG (dag_files holds paths like 'engine/ralph/config.py';
       match by converting path -> dotted module), OR
    4. An explicitly allowlisted third-party package.

    When dag_files is None, behavior is identical to the pre-D1 classifier.

    Returns (passed, error_msg). A dag-sibling passes WITH a directive message
    (the message is the point — it kills the harmful stdlib misdirective).
    """
    try:
        tree = ast.parse(code_str)
    except SyntaxError:
        return (True, None)  # syntax errors caught by earlier stages

    stdlib = getattr(sys, "stdlib_module_names", set())
    project_modules = set(known_imports.keys()) if known_imports else set()

    # Pre-compute the set of dotted module names declared in the DAG so the
    # per-import lookup is O(1). Path 'engine/ralph/config.py' -> module
    # 'engine.ralph.config'; 'api/health.py' -> 'api.health'.
    dag_modules: set[str] = set()
    dag_paths_by_module: dict[str, str] = {}
    if dag_files:
        for path in dag_files:
            mod = path.replace("/", ".").replace("\\", ".").removesuffix(".py")
            if mod.endswith(".__init__"):
                mod = mod[:-9]
            dag_modules.add(mod)
            dag_paths_by_module[mod] = path

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module] if node.module else []
        else:
            continue
        for name in names:
            top = name.split(".")[0]
            if top in stdlib or top in project_modules or top in ("fastapi", "uvicorn", "pytest"):
                continue
            # importlib_metadata is the PyPI backport of stdlib
            # importlib.metadata — treat as stdlib (cycle-3d).
            if name == "importlib_metadata" or top == "importlib_metadata":
                continue
            # Cycle-2 finding: `import api.health` must resolve when the
            # project contains api/health.py — i.e. the top-level package of
            # any known project module is importable.
            if any(m == top or m.startswith(top + ".") for m in project_modules):
                continue
            # D1: dag-sibling tier — module maps to a file declared in the
            # current DAG. Pass WITH a directive so the model codes against
            # the sibling's contract instead of reimplementing it.
            if dag_modules and (name in dag_modules or top in dag_modules):
                matched_path = dag_paths_by_module.get(name) or dag_paths_by_module.get(top, name)
                return (True, f"Import '{name}' is a sibling module of this "
                        f"project's task DAG (declared: {matched_path}). "
                        f"Do NOT reimplement it. Code against its contract; "
                        f"it exists at test time.")
            # Cycle-2 finding: bare "not found" gave the model no corrective
            # direction — it re-picked third-party packages on retry.
            return (False, f"Import '{name}' is not available. "
                    "Do NOT use third-party packages — reimplement the needed "
                    "functionality with the Python standard library only "
                    "(e.g. http.server instead of flask).")
    return (True, None)


def check_execution(code_str, namespace, project_path=None):
    """Execute code in isolated namespace. Returns (passed, error_msg).

    Cycle-3 finding (run-1788861553): executing a test module that imports a
    project module (test_kvstore.py → import kvstore) failed with
    ModuleNotFoundError — the project root was not on sys.path.
    """
    added = []
    if project_path:
        for p in (project_path, os.path.dirname(project_path) or project_path):
            p = os.path.abspath(p)
            if p not in sys.path:
                sys.path.insert(0, p)
                added.append(p)
    try:
        exec(code_str, namespace)
        return (True, None)
    except Exception as e:
        return (False, str(e))
    finally:
        for p in added:
            if p in sys.path:
                sys.path.remove(p)


def _with_timeout(func, timeout_s, label="operation"):
    """Run func with timeout. Raises TimeoutError if exceeded."""
    result = [None]
    error = [None]
    def target():
        try:
            result[0] = func()
        except Exception as e:
            error[0] = e
    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout=timeout_s)
    if t.is_alive():
        return (False, f"{label} timed out after {timeout_s}s")
    if error[0]:
        return (False, str(error[0]))
    return result[0]


@dataclass
class ValidationResult:
    """Result of 4-stage validation."""
    passed: bool
    stages: list[dict]  # [{stage: "ast", file: "x.py", passed: True, error: None}, ...]
