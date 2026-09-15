"""IIL skills-as-docs (S3, P1 minimal).

Skills are markdown docs the IIL loads from a directory and injects into the
intent/native prompts as context. A skill = a markdown file + a registry
entry the router can learn by embedding its description (future work). This
is the cheapest compounding growth tier (spec P1): house procedures
(variance runs, release gates, extraction playbooks) become consultable by
the re-plan brain without any security surface.

Skill-lookup MISS telemetry (a name that has no doc) is written to the
additive `skill_lookups` table — the future data source for TGD G9
(skill-lookup misses). Writing the events is in scope; implementing the G9
extractor is a bonus (done here since the schema is trivial).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("engine.intent.skills")

# Default skills dir (the IIL's own skills, distinct from the top-level
# skills/ that holds agent skills). Overridable for tests.
DEFAULT_SKILLS_DIR = Path(__file__).resolve().parent / "skills"

# TGD contract: skill_lookups table (additive). G9 extractor reads misses.
SKILL_LOOKUPS_TABLE = "skill_lookups"

# Front-matter pattern: lines like "Key: value" or "**Key:** value" at the
# top of the md file (markdown bold-wrapped keys are common in skill docs).
_FRONT_MATTER_RE = re.compile(
    r"^\*?\*?([A-Za-z0-9 _-]+)\*?\*?:\s*(.+)$"
)


@dataclass
class SkillDoc:
    """One loaded skill document."""
    skill_id: str
    title: str
    purpose: str
    body: str
    source_path: str


class SkillStore:
    """Loads skill markdown docs from a directory and injects them as context.

    Lookup is by skill_id (the filename without .md, or the Skill ID: field
    in front-matter). A miss is logged to skill_lookups (feeds G9).
    """

    def __init__(self, skills_dir: str | os.PathLike | None = None) -> None:
        self._dir = Path(skills_dir) if skills_dir else DEFAULT_SKILLS_DIR
        self._skills: dict[str, SkillDoc] = {}
        self._load_all()

    # -- loading ------------------------------------------------------------

    def _load_all(self) -> None:
        if not self._dir.is_dir():
            log.debug("skills dir %s absent — no skills loaded", self._dir)
            return
        for path in sorted(self._dir.glob("*.md")):
            try:
                doc = self._parse(path)
                self._skills[doc.skill_id] = doc
            except Exception as exc:  # noqa: BLE001 — one bad doc must not break the store
                log.warning("failed to parse skill %s: %s", path, exc)

    def _parse(self, path: Path) -> SkillDoc:
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        # The first H1 line is the title. After it, consecutive "Key: value"
        # lines (markdown bold-wrapped keys ok) are front-matter. The body
        # starts at the first H2 (##) or after the front-matter block.
        title = path.stem
        fm: dict[str, str] = {}
        body_lines: list[str] = []
        i = 0
        # 1. Consume the first H1 as the title.
        while i < len(lines):
            line = lines[i]
            if line.startswith("# ") and not line.startswith("## "):
                title = line.lstrip("# ").strip()
                # Strip a leading "Skill:" label (doc convention).
                if title.lower().startswith("skill:"):
                    title = title[len("skill:"):].strip()
                i += 1
                break
            i += 1
        # 2. Consume front-matter "Key: value" lines. Skip blank lines
        #    between fields; stop at the first heading (##) or after a
        #    blank line once the body (non-field text) has started.
        seen_field = False
        while i < len(lines):
            line = lines[i]
            if line.startswith("#"):
                break
            m = _FRONT_MATTER_RE.match(line)
            if m:
                key = m.group(1).strip().lower().replace("*", "")
                value = m.group(2).strip()
                # Strip markdown bold/backtick wrappers from values.
                value = value.strip("*").strip().strip("`").strip()
                fm[key] = value
                seen_field = True
                i += 1
                continue
            if not line.strip() and seen_field:
                i += 1  # skip blank lines, but keep scanning for more fields
                continue
            if not line.strip():
                i += 1  # blank line before any field (e.g. after title)
                continue
            # Non-field, non-blank text = start of body.
            break
        # 3. Everything else is the body.
        body_lines = lines[i:]
        skill_id = fm.get("skill id") or path.stem
        purpose = fm.get("purpose") or ""
        body = "\n".join(body_lines).strip()
        return SkillDoc(
            skill_id=skill_id, title=title, purpose=purpose,
            body=body, source_path=str(path),
        )

    # -- lookup --------------------------------------------------------------

    @property
    def skill_ids(self) -> list[str]:
        return sorted(self._skills.keys())

    def get(self, skill_id: str) -> SkillDoc | None:
        """Look up a skill by id. Returns None on miss (G9 telemetry)."""
        doc = self._skills.get(skill_id)
        if doc is None:
            _write_lookup(skill_id, hit=False)
        return doc

    def context_for_prompt(self, skill_id: str) -> str:
        """Return the skill text for injection into a prompt (or '')."""
        doc = self.get(skill_id)
        if doc is None:
            return ""
        return f"# Skill: {doc.title}\n\n{doc.purpose}\n\n{doc.body}"

    def all_context(self) -> str:
        """Return all skills as one context block (for small skill sets)."""
        parts = []
        for sid in self.skill_ids:
            doc = self._skills[sid]
            parts.append(f"## {doc.title}\n{doc.purpose}\n\n{doc.body}")
        return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Telemetry — additive skill_lookups table (TGD G9 contract).
# ---------------------------------------------------------------------------

def _ensure_lookups_table() -> None:
    from engine.state import _get_conn
    conn = _get_conn()
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {SKILL_LOOKUPS_TABLE} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT DEFAULT (datetime('now')),
        skill_id TEXT NOT NULL,
        hit INTEGER DEFAULT 0
    )""")
    conn.commit()
    conn.close()


def _write_lookup(skill_id: str, hit: bool) -> None:
    """Append a skill-lookup event. Best-effort (never crashes the caller)."""
    try:
        _ensure_lookups_table()
        from engine.state import _get_conn
        conn = _get_conn()
        conn.execute(
            f"INSERT INTO {SKILL_LOOKUPS_TABLE} (skill_id, hit) VALUES (?, ?)",
            (skill_id, 1 if hit else 0),
        )
        conn.commit()
        conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("skill_lookups write failed: %s", exc)


def get_skill_lookups(limit: int = 100) -> list[dict]:
    """Read skill-lookups rows (most recent first). Used by tests + tooling."""
    _ensure_lookups_table()
    from engine.state import _get_conn
    conn = _get_conn()
    rows = conn.execute(
        f"SELECT * FROM {SKILL_LOOKUPS_TABLE} ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
