"""PRD Parser — decomposes markdown PRDs into module-grouped coding tasks.

Groups related user stories into implementation modules when the PRD
contains an "Implementation Decisions" section with a module list.
Falls back to one-task-per-story when no module list is found.

Usage:
    python3 prd_parser.py <prd-file.md> [--output tasks.json] [--format json|markdown]
"""

import re
import json
import sys
import argparse
from pathlib import Path


class PRDParser:
    def __init__(self):
        self.sections = []
        self.user_stories = []
        self.requirements = []
        self.modules = []          # list of {name, description, keywords}
        self._raw_content = ""     # kept for module extraction
        self._available_libs = []  # libraries available in the runtime
        self._specs = {}           # module_name -> spec text
        self._dependency_order = []  # explicit task ordering

    def parse(self, prd_path: str) -> dict:
        """Parse a markdown PRD file into structured tasks."""
        self._raw_content = Path(prd_path).read_text()
        self._extract_sections(self._raw_content)
        self._extract_user_stories(self._raw_content)
        self._extract_requirements(self._raw_content)
        self._available_libs = self._extract_available_libs(self._raw_content)
        self._specs = self._extract_module_specs(self._raw_content)
        self._dependency_order = self._extract_dependency_order(self._raw_content)
        self.modules = self._extract_modules(self._raw_content)
        tasks = self._decompose_into_tasks()
        return {
            "prd_title": self._extract_title(self._raw_content),
            "tasks": tasks,
            "metadata": {
                "total_tasks": len(tasks),
                "total_estimated_tokens": sum(t["estimated_tokens"] for t in tasks),
                "estimated_time_seconds": sum(t["estimated_tokens"] for t in tasks) // 200,
                "grouping": "module" if self.modules else "per-story",
                "modules_found": len(self.modules),
                "available_libs": self._available_libs,
                "specs_found": len(self._specs),
            }
        }

    def _extract_sections(self, content):
        """Extract H2/H3 sections from markdown."""
        self.sections = re.findall(r'^#{2,3}\s+(.+)$', content, re.MULTILINE)

    def _extract_user_stories(self, content):
        """Extract 'As a... I want... so that...' patterns.

        Handles both ``so that`` and ``so`` (without ``that``) clauses,
        as well as numbered list items (``1. As a...``).

        Returns list of 3-tuples: (actor, want, benefit).  ``benefit`` may
        be an empty string when the ``so`` clause is missing.
        """
        # Pattern 1: full "As a... I want... so that..." or "so ..."
        pattern = (
            r'As\s+(?:a|an)\s+(.+?),\s+I\s+want\s+(.+?)'
            r'(?:,\s*so\s+(?:that\s+)?(.+?))?(?:\.|$)'
        )
        raw = re.findall(pattern, content, re.MULTILINE | re.IGNORECASE)
        # Normalize to 3-tuples (benefit may be empty)
        self.user_stories = [
            (actor.strip(), want.strip(), (benefit or "").strip())
            for actor, want, benefit in raw
        ]

    def _extract_requirements(self, content):
        """Extract bullet points under Technical Requirements."""
        # Find the Technical Requirements section and grab its bullet points
        match = re.search(
            r'#+\s+Technical Requirements\s*\n(.*?)(?=^#|\Z)',
            content,
            re.MULTILINE | re.DOTALL
        )
        if match:
            section_text = match.group(1)
            self.requirements = re.findall(r'^\s*[-*]\s+(.+)$', section_text, re.MULTILINE)
        else:
            self.requirements = []

    # ------------------------------------------------------------------
    # Available libraries extraction
    # ------------------------------------------------------------------

    def _extract_available_libs(self, content: str) -> list:
        """Extract available libraries from 'Runtime Environment' section.

        Looks for a bullet list under a heading containing 'Runtime Environment'
        or 'Available Libraries'.  Returns a list of library name strings.
        Filters out common non-library items (database filenames, paths, ports).
        """
        # Patterns that indicate this is NOT a library
        _non_lib = re.compile(
            r'(?:\.db$|\.sqlite$|\.json$|\.yaml$|\.yml$|/|port\s|database|stdlib)',
            re.IGNORECASE,
        )

        # Try "Available Libraries" first, then "Runtime Environment"
        for heading_pattern in [
            r'#+\s+Available\s+Libraries\s*\n',
            r'#+\s+Runtime\s+Environment\s*\n',
        ]:
            match = re.search(
                heading_pattern + r'(.*?)(?=^#{1,3}\s|\Z)',
                content, re.MULTILINE | re.DOTALL,
            )
            if match:
                section = match.group(1)
                # Extract backtick-quoted library names
                libs = re.findall(r'`([^`]+)`', section)
                if libs:
                    return [lib for lib in libs if not _non_lib.search(lib)]
                # Fallback: extract bullet items
                raw = re.findall(r'^\s*[-*]\s+`?(\w[\w.-]*)`?', section, re.MULTILINE)
                return [lib for lib in raw if not _non_lib.search(lib)]
        return []

    # ------------------------------------------------------------------
    # Module spec extraction (sections with spec text per module)
    # ------------------------------------------------------------------

    def _extract_module_specs(self, content: str) -> dict:
        """Extract per-module specification text from the PRD.

        Looks for H3 sections under 'Implementation Decisions' that contain
        detailed spec text.  Maps module filename -> spec text.

        Also extracts the 'Event Envelope Schema', 'SQLite Schema',
        'API Endpoints', and 'Event Types' tables as shared spec context
        that gets injected into every task.
        """
        specs = {}

        # Extract shared spec sections (schemas, tables, API contracts)
        shared_sections = []
        for section_name in [
            'Event Envelope Schema', 'SQLite Schema', 'API Endpoints',
            'Event Types', 'Ring Buffer', 'Dashboard Layout',
            'Testing',
        ]:
            pattern = rf'#+\s+{re.escape(section_name)}\s*\n(.*?)(?=^#{1,3}\s|\Z)'
            match = re.search(pattern, content, re.MULTILINE | re.DOTALL)
            if match:
                text = match.group(1).strip()
                if text:
                    shared_sections.append(f"### {section_name}\n{text}")

        shared_spec = "\n\n".join(shared_sections) if shared_sections else ""

        # Extract per-module specs from "Module Organization" subsection
        # Each bullet like "- streaming_server.py — FastAPI app: ..." defines a module
        # Look for detailed subsections that follow each module bullet
        mod_section = re.search(
            r'#+\s+Implementation\s+Decisions.*?\n'
            r'(?:.*?\n)*?'
            r'#+\s+Module\s+Organization\s*\n'
            r'(.*?)(?=^#{2,3}\s|\Z)',
            content,
            re.MULTILINE | re.DOTALL | re.IGNORECASE,
        )

        if mod_section:
            section_text = mod_section.group(1)
            # Parse module bullets — same pattern as _extract_modules
            bullet_re = re.compile(
                r'^\s*[-*]\s+`?([\w./-]+\.(?:py|js|ts|html|css|json|yaml|yml|toml|sh))`?\s*[-–—~:]+\s*(.+?)$',
                re.MULTILINE,
            )
            for m in bullet_re.finditer(section_text):
                filename = m.group(1)
                desc = m.group(2).strip()
                stem = re.sub(r'[^a-zA-Z0-9]', '_', filename).strip('_').replace('.py', '').replace('.js', '').replace('.html', '').replace('.css', '')
                # Build spec: description + shared context
                spec_parts = [f"Module: {filename}\nDescription: {desc}"]
                if shared_spec:
                    spec_parts.append(shared_spec)
                specs[stem] = "\n\n".join(spec_parts)

        # If no module section found, create specs from shared sections only
        if not specs and shared_spec:
            specs["_shared"] = shared_spec

        return specs

    # ------------------------------------------------------------------
    # Dependency order extraction
    # ------------------------------------------------------------------

    def _extract_dependency_order(self, content: str) -> list:
        """Extract explicit task ordering from 'Dependency Order' section.

        Looks for a numbered or bulleted list under 'Dependency Order':
          1. T01 first: streaming_client.py — ...
          2. T02 second: streaming_server.py — ...

        Returns a list of dicts: [{task_num, filename, description}].
        """
        match = re.search(
            r'#+\s+Dependency\s+Order\s*\n(.*?)(?=^#{1,3}\s|\Z)',
            content, re.MULTILINE | re.DOTALL,
        )
        if not match:
            return []

        section = match.group(1)
        order = []
        # Match patterns like "1. **T01** first: `file.py` — description"
        # or "1. T01 first: file.py — description"
        # Also handles: "1. **T01** first: `dashboard/index.html` — ..."
        item_re = re.compile(
            r'^\s*\d+\.\s*(?:\*\*)?T(\d+)(?:\*\*)?\s+\w+:\s*`?([\w./-]+)`?\s*[-–—~:]\s*(.+?)$',
            re.MULTILINE,
        )
        for m in item_re.finditer(section):
            order.append({
                "task_num": int(m.group(1)),
                "filename": m.group(2),
                "description": m.group(3).strip(),
            })

        return order

    # ------------------------------------------------------------------
    # Module extraction from "Implementation Decisions" section
    # ------------------------------------------------------------------

    def _extract_modules(self, content: str) -> list:
        """Extract module definitions from an Implementation Decisions section.

        Looks for a subsection like "Module Organization" that contains a
        bulleted list of ``- module_name.py — description`` entries.
        Returns a list of dicts: [{name, description, keywords}].
        Returns an empty list if no module list is found (triggers fallback).
        """
        # Find the "Module Organization" subsection (or any H3 under
        # Implementation Decisions that lists modules).
        mod_section = re.search(
            r'#+\s+Implementation\s+Decisions.*?\n'
            r'(?:.*?\n)*?'                         # skip header line
            r'#+\s+Module\s+Organization\s*\n'     # the H3 we want
            r'(.*?)(?=^#{2,3}\s|\Z)',              # grab until next H2/H3
            content,
            re.MULTILINE | re.DOTALL | re.IGNORECASE,
        )
        if not mod_section:
            return []

        section_text = mod_section.group(1)

        # Parse bullet lines:  "- module_name.py — description"
        # Also handles: "- dashboard/index.html — description"
        # Handles both em-dash (—) and regular dash (--) and single dash (-)
        bullet_re = re.compile(
            r'^\s*[-*]\s+'
            r'`?([\w./-]+\.(?:py|js|ts|html|css|json|yaml|yml|toml|sh))`?\s*'  # module filename
            r'[-–—~:]+\s*'                          # separator dash(es) or colon
            r'(.+?)$',                              # description
            re.MULTILINE,
        )

        modules = []
        for m in bullet_re.finditer(section_text):
            filename = m.group(1)
            desc = m.group(2).strip()
            # Derive a stem name: streaming_server.py → streaming_server
            #                      dashboard/index.html → dashboard_index
            stem = re.sub(r'[^a-zA-Z0-9]', '_', filename).strip('_').replace('.py', '').replace('.js', '').replace('.html', '').replace('.css', '')
            # Generate keywords from the stem and description
            keywords = self._module_keywords(stem, desc)
            modules.append({
                "name": stem,
                "filename": filename,
                "description": desc,
                "keywords": keywords,
            })

        return modules

    @staticmethod
    def _module_keywords(stem: str, description: str) -> list:
        """Derive a set of lowercase keywords for matching stories to modules.

        Uses only the module stem words and description words — no context
        enrichment — to keep classification precise.  Domain-specific
        keywords are added selectively for well-known patterns.
        """
        parts = stem.split("_")
        keywords = list(parts)
        stop = {"the", "for", "and", "with", "from", "that", "this", "into",
                "all", "new", "non", "via", "our", "are", "has", "not",
                "but", "can", "may", "its", "also", "will", "when", "each"}
        for w in re.findall(r'[a-z]{3,}', description.lower()):
            if w not in stop and w not in keywords:
                keywords.append(w)

        # Selective domain-specific keyword expansions.
        # Only triggered when the exact domain stem word is present.
        domain_hints = {
            "cache":       ["kv", "kvarn", "lcp", "defrag"],
            "gpu":         ["nvidia", "temperature", "power", "vram", "thermal"],
            "pipeline":    ["stall", "batch", "bubble"],
            "memory":      ["vram", "fragmentation", "oom", "headroom"],
            "slot":        ["queue"],
            "beellama":    ["draft", "acceptance"],
            "waterfall":   ["chart", "svg"],
            "dashboard":   ["html", "offline"],
            "alert":       ["threshold"],
            "bandwidth":   ["pcie"],
            "trace":       ["percentile", "p95"],
            "inference":   ["prompt", "generation"],
        }
        stem_words = set(stem.split("_"))
        for trigger, extra_kws in domain_hints.items():
            if trigger in stem_words:
                for ek in extra_kws:
                    if ek not in keywords:
                        keywords.append(ek)

        return keywords

    # ------------------------------------------------------------------
    # Classification of user stories into modules
    # ------------------------------------------------------------------

    def _classify_story_to_module(self, actor: str, want: str, benefit: str) -> str:
        """Return the best-matching module name for a user story, or 'general'."""
        text = f"{actor} {want} {benefit}".lower()

        best_module = None
        best_score = 0

        for mod in self.modules:
            score = sum(1 for kw in mod["keywords"] if kw in text)
            if score > best_score:
                best_score = score
                best_module = mod["name"]

        # Require at least 2 keyword matches to avoid false grouping.
        # Single-word matches (e.g. "api") are too ambiguous.
        return best_module if best_score >= 2 else "general"

    def _classify_req_to_module(self, requirement: str) -> str:
        """Return the best-matching module name for a requirement, or 'general'."""
        text = requirement.lower()
        best_module = None
        best_score = 0
        for mod in self.modules:
            score = sum(1 for kw in mod["keywords"] if kw in text)
            if score > best_score:
                best_score = score
                best_module = mod["name"]
        return best_module if best_score >= 2 else "general"

    # ------------------------------------------------------------------
    # Task decomposition
    # ------------------------------------------------------------------

    def _decompose_into_tasks(self) -> list:
        """Convert user stories + requirements into tasks.

        When modules are extracted from the PRD, groups stories into
        module-level tasks.  Otherwise falls back to one-task-per-story.
        """
        if self.modules:
            return self._decompose_grouped()
        return self._decompose_flat()

    # ---------- grouped (module-level) decomposition ----------

    def _decompose_grouped(self) -> list:
        """Group stories and requirements into module-level tasks.

        Enhanced to inject:
        - ``spec``: per-module technical specification text from the PRD.
        - ``available_libs``: runtime libraries the model may import.
        - ``imports``: import paths from other modules in this PRD.
        - ``depends_on``: task IDs that must complete before this task.
        """
        # Build a dict: module_name → list of (actor, want, benefit) tuples
        module_stories: dict[str, list] = {m["name"]: [] for m in self.modules}
        module_stories["general"] = []

        for story in self.user_stories:
            actor, want, benefit = story
            mod = self._classify_story_to_module(actor, want, benefit)
            module_stories.setdefault(mod, []).append(story)

        # Requirements
        module_reqs: dict[str, list] = {m["name"]: [] for m in self.modules}
        module_reqs["general"] = []
        for req in self.requirements:
            mod = self._classify_req_to_module(req)
            module_reqs.setdefault(mod, []).append(req)

        # Find the module object by name
        mod_map = {m["name"]: m for m in self.modules}

        # Build dependency order lookup: filename -> task_num
        dep_order = {item["filename"]: item for item in self._dependency_order}

        tasks = []
        task_id = 1
        order = [m["name"] for m in self.modules] + (
            ["general"] if module_stories.get("general") or module_reqs.get("general") else []
        )

        # Track task_id -> filename mapping for dependency resolution
        task_file_map = {}  # filename -> task_id string

        for mod_name in order:
            stories = module_stories.get(mod_name, [])
            reqs = module_reqs.get(mod_name, [])
            if not stories and not reqs:
                continue

            mod_obj = mod_map.get(mod_name)
            mod_title = mod_name.replace("_", " ").title()
            if mod_obj:
                mod_title = f"{mod_title} — {mod_obj['description'][:80]}"

            # Collect acceptance criteria from stories
            acceptance = []
            for actor, want, benefit in stories:
                acceptance.append(f"[{actor}] {want.strip()}")
            for req in reqs:
                acceptance.append(f"Requirement: {req[:100]}")

            # Estimate tokens: 3000 per story + 2000 per requirement, capped
            est_tokens = min(len(stories) * 3000 + len(reqs) * 2000, 30000)

            # Determine files_to_create from module name
            files = []
            if mod_obj:
                files = [mod_obj["filename"]]
                task_file_map[mod_obj["filename"]] = f"T{task_id:02d}"

            # Pick dominant category from the stories
            categories = [self._categorize_task(w) for _, w, _ in stories]
            categories += [self._categorize_task(r) for r in reqs]
            category = max(set(categories), key=categories.count) if categories else "logic"

            tasks.append({
                "id": f"T{task_id:02d}",
                "title": mod_title,
                "description": (
                    f"Implement the {mod_name} module. "
                    f"Includes {len(stories)} user stories and {len(reqs)} requirements."
                ),
                "files_to_create": files,
                "files_to_modify": [],
                "dependencies": [],
                "acceptance_criteria": acceptance,
                "complexity": "high" if len(stories) > 5 else "medium",
                "estimated_tokens": est_tokens,
                "category": category,
                "user_stories": [
                    {"actor": a, "want": w, "benefit": b}
                    for a, w, b in stories
                ],
                "requirements": reqs,
            })
            task_id += 1

        # ── Post-pass: inject spec, libs, imports, dependencies ──────

        # Build a reverse map: task_id -> module_name
        task_mod_map = {}
        for i, t in enumerate(tasks):
            # Find the module name for this task
            for mod_name in order:
                if module_stories.get(mod_name) or module_reqs.get(mod_name):
                    expected_id = f"T{i + 1:02d}"
                    if t["id"] == expected_id:
                        task_mod_map[t["id"]] = mod_name
                        break

        for t in tasks:
            mod_name = task_mod_map.get(t["id"], "")

            # Inject spec text
            spec = self._specs.get(mod_name, "")
            if spec:
                t["spec"] = spec
            elif "_shared" in self._specs:
                t["spec"] = self._specs["_shared"]

            # Inject available libraries
            if self._available_libs:
                t["available_libs"] = self._available_libs

            # Inject imports from other modules (what this module imports)
            files = t.get("files_to_create", [])
            imports = []
            for fname in files:
                # This module may import from other modules in the PRD
                for other_mod_name, other_mod in mod_map.items():
                    if other_mod_name != mod_name:
                        # Use the other module's Python import path
                        other_filename = other_mod["filename"]
                        if other_filename.endswith(".py"):
                            other_import_name = other_filename.replace(".py", "")
                            # Don't import from self
                            current_import_name = fname.replace(".py", "")
                            if other_import_name != current_import_name:
                                imports.append(f"from {other_import_name} import ...")
            if imports:
                t["imports"] = imports

            # Inject dependency ordering from PRD's Dependency Order section
            dep_task_ids = []
            for fname in files:
                if fname in dep_order:
                    # Find tasks that should complete before this one
                    my_num = dep_order[fname]["task_num"]
                    for other_fname, other_item in dep_order.items():
                        if other_item["task_num"] < my_num and other_fname != fname:
                            # Find the task that creates this file
                            other_task_id = task_file_map.get(other_fname)
                            if other_task_id and other_task_id not in dep_task_ids:
                                dep_task_ids.append(other_task_id)
            if dep_task_ids:
                t["dependencies"] = dep_task_ids
                t["depends_on"] = dep_task_ids

        return tasks

    # ---------- flat (per-story) decomposition — original behavior ----------

    def _decompose_flat(self) -> list:
        """Original behavior: one task per user story + one per requirement."""
        tasks = []
        task_id = 1

        for story in self.user_stories:
            actor, want, benefit = story
            tasks.append({
                "id": f"T{task_id:02d}",
                "title": want.strip(),
                "description": f"As {actor}, implement: {want}. {benefit}",
                "files_to_create": [],
                "files_to_modify": [],
                "dependencies": [],
                "acceptance_criteria": [f"Feature implements: {want}"],
                "complexity": "medium",
                "estimated_tokens": 3000,
                "category": self._categorize_task(want),
            })
            task_id += 1

        for req in self.requirements:
            tasks.append({
                "id": f"T{task_id:02d}",
                "title": req[:60],
                "description": req,
                "files_to_create": [],
                "files_to_modify": [],
                "dependencies": [],
                "acceptance_criteria": [f"Requirement met: {req[:80]}"],
                "complexity": "medium",
                "estimated_tokens": 2000,
                "category": self._categorize_task(req),
            })
            task_id += 1

        return tasks

    def _extract_title(self, content):
        match = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
        return match.group(1) if match else "Untitled PRD"

    def _categorize_task(self, description):
        desc = description.lower()
        if any(w in desc for w in ['model', 'database', 'schema', 'table']):
            return "data"
        elif any(w in desc for w in ['api', 'endpoint', 'route', 'request']):
            return "api"
        elif any(w in desc for w in ['ui', 'frontend', 'button', 'page', 'display']):
            return "ui"
        elif any(w in desc for w in ['test', 'verify', 'validate']):
            return "test"
        elif any(w in desc for w in ['config', 'setup', 'install', 'deploy']):
            return "infra"
        else:
            return "logic"


def main():
    parser = argparse.ArgumentParser(description="Parse PRD into coding tasks")
    parser.add_argument("prd_file", help="Path to markdown PRD file")
    parser.add_argument("--output", "-o", help="Output file path")
    parser.add_argument("--format", "-f", choices=["json", "markdown"], default="json")
    args = parser.parse_args()

    p = PRDParser()
    result = p.parse(args.prd_file)

    if args.format == "json":
        output = json.dumps(result, indent=2)
    else:
        output = _format_markdown(result)

    if args.output:
        Path(args.output).write_text(output)
        print(f"Written to {args.output}")
    else:
        print(output)


def _format_markdown(result):
    lines = [f"# {result['prd_title']}\n"]
    lines.append(f"**Total tasks:** {result['metadata']['total_tasks']}")
    lines.append(f"**Estimated tokens:** {result['metadata']['total_estimated_tokens']}")
    lines.append(f"**Estimated time:** {result['metadata']['estimated_time_seconds']}s\n")
    for task in result["tasks"]:
        lines.append(f"## {task['id']}: {task['title']}")
        lines.append(f"**Complexity:** {task['complexity']} | **Tokens:** {task['estimated_tokens']}")
        lines.append(f"**Category:** {task['category']}")
        lines.append(f"\n{task['description']}\n")
        lines.append("**Acceptance criteria:**")
        for ac in task["acceptance_criteria"]:
            lines.append(f"- [ ] {ac}")
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    main()
