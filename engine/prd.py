"""Engine PRD parsing — bridge to PRDParser with regex fallback strategies."""

import re

from engine import Task


BOILERPLATE_SECTIONS = {"user stories", "problem statement", "acceptance criteria",
                        "further notes", "out of scope", "glossary", "references"}


def _filter_boilerplate(text):
    """Strip boilerplate sections from PRD content.

    Splits markdown on ``\\n## `` headers, drops sections whose heading
    matches any entry in BOILERPLATE_SECTIONS (case-insensitive).
    Returns the original text unchanged when filtering would remove everything.
    """
    sections = re.split(r'\n##\s+', text)
    kept = [s for s in sections
            if not any(s.lower().startswith(b) for b in BOILERPLATE_SECTIONS)]
    # If filtering removed EVERYTHING (PRD is all boilerplate), keep original
    return "\n".join(kept) if any(s.strip() for s in kept) else text


CATEGORY_TO_TYPE = {
    "test": "test",
    "edit": "edit",
    "api": "implementation",
    "data": "implementation",
    "ui": "implementation",
    "logic": "implementation",
    "infra": "refactoring",
    "research": "research",
}


def _prdparser_task_to_engine_task(raw):
    """Map a PRDParser task dict to an engine Task dataclass.

    Args:
        raw: dict with keys id, title, description, files_to_create (list),
             category (str), dependencies (list), spec (str).

    Returns:
        Task dataclass instance.
    """
    files = raw.get("files_to_create", []) or []
    primary = files[0] if files else f"task_{raw.get('id', 'T00')}.py"
    return Task(
        id=raw["id"],
        title=raw.get("title", raw["id"]),
        description=raw.get("description", ""),
        module=primary.replace("..", "_").lstrip("/"),
        dependencies=raw.get("dependencies", []) or raw.get("depends_on", []),
        task_type=CATEGORY_TO_TYPE.get(raw.get("category", ""), "implementation"),
        priority=1,
        prd_section=raw.get("spec", ""),
        files=files or [primary],
    )


def _sanitize_module(module, task_id):
    """Block path traversal; fall back to task_{id}.py when needed."""
    # Reject absolute paths and home-dir paths immediately
    if module.startswith(("/", "~")):
        return f"task_{task_id}.py"
    # Strip all .. path components (path traversal)
    parts = module.split("/")
    clean_parts = [p for p in parts if p != ".."]
    module = "/".join(clean_parts)
    module = module.lstrip("/")
    if not module:
        module = f"task_{task_id}.py"
    return module


def parse_prd(prd_path):
    """
    Parse a PRD markdown file into a list of Task objects.

    Supports multiple PRD formats:
    - Table: "| T01 | Health Check | api/health.py |"
    - Numbered lists: "1. As a user, I want..."
    - Bullet lists: "- Create health check endpoint"
    - Headers: "## Task 1: Health Check" followed by description

    Tries the existing PRDParser first (richer parsing: module grouping,
    classification, spec extraction, dependency ordering, library detection).
    Falls back to four regex strategies when PRDParser is absent or returns
    zero tasks.

    Returns list[Task] in dependency order (topological sort).
    """
    with open(prd_path, "r") as f:
        content = f.read()

    tasks = []

    # C5 FIX: TRY PRDParser FIRST — it groups by module (~50% task reduction),
    # classifies user stories, extracts specs, orders by dependencies, and
    # detects libraries. Regex below is only a fallback.
    try:
        from prd_parser import PRDParser
        parsed = PRDParser().parse(prd_path)
        raw_tasks = parsed.get("tasks", []) if isinstance(parsed, dict) else list(parsed)
        if raw_tasks:
            return _topological_sort([_prdparser_task_to_engine_task(t) for t in raw_tasks])
    except Exception:
        pass  # fall through to regex strategies

    # Strip boilerplate sections (User Stories, Problem Statement, Acceptance Criteria,
    # Further Notes, Out of Scope, Glossary, References) before any extraction
    # BEFORE any regex extraction, so items come from deliverable sections only.
    task_content = _filter_boilerplate(content)

    # Strategy 1: Table format (| ID | Title | Module | ...)
    # Table strategy uses raw content (boplate never uses T-ids).
    table_pattern = r'\|\s*(T\d+)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|'
    table_matches = re.findall(table_pattern, content)
    if table_matches:
        for match in table_matches:
            task_id, title, module = match
            tasks.append(Task(
                id=task_id.strip(),
                title=title.strip(),
                description=title.strip(),
                module=module.strip(),
            ))
        return _topological_sort(tasks)

    # Strategy 2: Numbered list ("1. ..." or "1) ...") — on FILTERED content
    numbered_pattern = r'(?:^|\n)\s*(\d+)[.)]\s+(.*?)(?=\n\s*\d+[.)]|\n##|\Z)'
    numbered_matches = re.findall(numbered_pattern, task_content, re.DOTALL)
    if numbered_matches:
        # Re-filter from raw content to get clean section boundaries
        sections = re.split(r'\n##\s+', content)
        task_content_filtered = "\n".join(
            s for s in sections
            if not any(s.lower().startswith(b) for b in BOILERPLATE_SECTIONS)
        )
        numbered_matches = re.findall(numbered_pattern, task_content_filtered, re.DOTALL)
    if numbered_matches:
        for num, text in numbered_matches:
            task_id = f"T{int(num):02d}"
            # Detect filenames with OR without backticks
            module_match = (re.search(r'`([^`]+\.(?:py|js|ts|yaml|txt))`', text)
                            or re.search(r'\b([\w/\-]+\.(?:py|js|ts|yaml|txt))\b', text))
            module = module_match.group(1) if module_match else f"task_{task_id}.py"
            module = _sanitize_module(module, task_id)
            tasks.append(Task(
                id=task_id,
                title=text.strip()[:80],
                description=text.strip(),
                module=module,
            ))
        return _topological_sort(tasks)

    # Strategy 3: Bullet lists ("- ..." or "* ...")
    bullet_pattern = r'(?:^|\n)\s*[-*]\s+(.*?)(?=\n\s*[-*]|\n##|\Z)'
    bullet_matches = re.findall(bullet_pattern, content, re.DOTALL)
    if bullet_matches:
        for i, text in enumerate(bullet_matches, 1):
            task_id = f"T{i:02d}"
            module_match = re.search(r'`([^`]+\.(?:py|js|ts|yaml))`', text)
            module = module_match.group(1) if module_match else f"task_{task_id}.py"
            module = _sanitize_module(module, task_id)
            tasks.append(Task(
                id=task_id,
                title=text.strip()[:80],
                description=text.strip(),
                module=module,
            ))
        return _topological_sort(tasks)

    # Strategy 4: Headers ("## Task: ..." or "## ...")
    # LEDGER-63 FIX: only headings matching the task pattern
    #   "## Task <ID>: ..."  (e.g. "## Task R06:", "## Task T05:")
    # start a new task.  Any OTHER ## heading (e.g. "## Output contract",
    # "## Notes", "## Sources") is part of the current task's description
    # (or a top-level PRD section), NEVER a new task.  Before this fix the
    # beellama dogfood driver's per-atom PRD — which has a real task heading
    # followed by an "## Output contract (MANDATORY)" heading — produced a
    # phantom T02 task whose "question" was the output-contract text, leading
    # the model to fabricate facts.
    header_pattern = r'(?:^|\n)##\s+(.*?)(?=\n##|\Z)'
    header_matches = re.findall(header_pattern, content, re.DOTALL)
    # Cycle-2 finding: our own PRD convention ("File: api/health.py" on a
    # plain line, ids like "## Task T05:"/"## Task S01:") fell through to
    # task_TXX.py fallbacks — which then broke cross-task imports.
    file_line_pattern = r'^\s*File:\s*([\w./\-]+\.(?:py|js|ts|yaml))\s*$'
    task_counter = 0
    for text in header_matches:
        stripped = text.strip()
        id_match = re.match(r'Task\s+([A-Za-z]\d+)\b', stripped)
        if id_match:
            # Genuine task heading — create a new task.
            task_counter += 1
            task_id = id_match.group(1)
            module_match = (re.search(r'`([^`]+\.(?:py|js|ts|yaml))`', text)
                            or re.search(file_line_pattern, text, re.MULTILINE))
            module = module_match.group(1) if module_match else f"task_{task_id}.py"
            tasks.append(Task(
                id=task_id,
                title=stripped.split("\n")[0][:80],
                description=stripped,
                module=module,
            ))
        else:
            # Non-task heading (e.g. "## Output contract", "## Notes").
            # Append to the current task's description if one exists;
            # otherwise it is a top-level PRD section — ignore it.
            if tasks:
                tasks[-1].description = (tasks[-1].description or "") + "\n\n## " + stripped
            # If no task exists yet, the heading is a top-level PRD section
            # (e.g. "## Problem Statement") — silently skip it.

    return _topological_sort(tasks)


def _topological_sort(tasks):
    """Sort tasks by dependency order (topological sort).

    Tasks with no dependencies come first. Each task appears after all
    of its dependencies. Missing dependency IDs are silently skipped
    (no error raised).
    """
    task_map = {t.id: t for t in tasks}
    visited = set()
    result = []

    def visit(task_id):
        if task_id in visited:
            return
        visited.add(task_id)
        task = task_map.get(task_id)
        if task:
            for dep in task.dependencies:
                visit(dep)
            result.append(task)

    for task in tasks:
        visit(task.id)

    return result
