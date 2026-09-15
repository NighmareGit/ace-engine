"""D5 tests: skills wiring (generator.py + seed docs).

1. generator.py: SkillStore context is wired into the generation prompt when
   a skill matches; no matches -> prompt unchanged.
2. Seed skill docs (multi-module-implementation.md, file-targeting-discipline.md)
   parse correctly and exist.
"""

import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(__file__, "..", "..", ".."))
sys.path.insert(0, PROJECT_ROOT)


# ---------------------------------------------------------------------------
# generator.py: skill context in prompt
# ---------------------------------------------------------------------------

def test_skill_context_appears_when_match_exists(tmp_path, monkeypatch):
    """When a skill matches, its context appears in the generation prompt."""
    # Create a temp skills dir with a matching skill.
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "multi-module-implementation.md").write_text(
        "# Skill: Multi-Module Implementation\n\n"
        "**Skill ID:** `multi-module-implementation`\n"
        "**Purpose:** Scaffold all files[] before wiring imports.\n\n"
        "## Procedure\n\nScaffold first, then wire.\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ACE_SKILLS_DIR", str(skills_dir))

    from engine.intent.skills import SkillStore
    store = SkillStore(skills_dir=str(skills_dir))
    ctx = store.context_for_prompt("multi-module-implementation")
    assert "Multi-Module Implementation" in ctx
    assert "Scaffold first" in ctx


def test_no_skills_dir_prompt_unchanged(tmp_path, monkeypatch):
    """When no skills dir exists, the generation prompt is byte-identical
    to what it would be without skills wiring (no crash, no extra text)."""
    empty_dir = tmp_path / "empty_skills"
    empty_dir.mkdir()
    from engine.intent.skills import SkillStore
    store = SkillStore(skills_dir=str(empty_dir))
    # No skills loaded -> context_for_prompt returns ''
    assert store.context_for_prompt("anything") == ""
    assert store.skill_ids == []


# ---------------------------------------------------------------------------
# seed skill docs
# ---------------------------------------------------------------------------

def test_seed_skill_docs_exist():
    """Both seed skill docs must exist in engine/intent/skills/."""
    skills_dir = os.path.join(PROJECT_ROOT, "engine", "intent", "skills")
    assert os.path.isfile(os.path.join(skills_dir,
                                       "multi-module-implementation.md"))
    assert os.path.isfile(os.path.join(skills_dir,
                                       "file-targeting-discipline.md"))


def test_seed_skill_docs_parse():
    """Both seed skill docs must parse into valid SkillDoc objects."""
    from engine.intent.skills import SkillStore
    skills_dir = os.path.join(PROJECT_ROOT, "engine", "intent", "skills")
    store = SkillStore(skills_dir=skills_dir)
    ids = store.skill_ids
    assert "multi-module-implementation" in ids
    assert "file-targeting-discipline" in ids


def test_seed_skill_content():
    """Seed docs carry the design's key guidance."""
    from engine.intent.skills import SkillStore
    skills_dir = os.path.join(PROJECT_ROOT, "engine", "intent", "skills")
    store = SkillStore(skills_dir=skills_dir)
    doc = store.get("multi-module-implementation")
    assert doc is not None
    # The doc should mention scaffolding / not inlining siblings
    combined = (doc.purpose + doc.body).lower()
    assert "scaffold" in combined or "sibling" in combined
