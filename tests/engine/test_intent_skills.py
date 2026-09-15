"""S3 tests: IIL skills-as-docs (skills.py).

Skills are markdown docs loaded from a directory and injected into prompts as
context. A lookup miss writes to the additive skill_lookups table (the future
data source for TGD G9). Tests assert loading, lookup, miss telemetry, and
prompt-context injection.

Target: ≥8 tests.
"""

import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from engine.intent.skills import (
    SkillStore, get_skill_lookups,
)
from engine.intent.types import IntentRequest


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "engine.db")
    monkeypatch.setenv("ENGINE_DB_PATH", db_path)
    from engine.state import init_db
    init_db()
    yield db_path


@pytest.fixture
def store(tmp_path):
    """A skill store pointed at a temp dir with two example skills."""
    d = tmp_path / "skills"
    d.mkdir()
    (d / "alpha.md").write_text(
        "# Skill: Alpha Procedure\n\n"
        "**Skill ID:** `alpha`\n"
        "**Purpose:** First example skill.\n\n"
        "## When to Use\n\nUse alpha when testing.\n\n"
        "## Procedure\n\nStep one. Step two.\n",
        encoding="utf-8",
    )
    (d / "beta.md").write_text(
        "# Skill: Beta Procedure\n\n"
        "**Skill ID:** `beta`\n"
        "**Purpose:** Second example skill.\n\n"
        "## When to Use\n\nUse beta for the other case.\n",
        encoding="utf-8",
    )
    return SkillStore(skills_dir=str(d))


class TestSkillLoading:
    """Skills load from markdown files."""

    def test_loads_all_skills(self, store):
        assert set(store.skill_ids) == {"alpha", "beta"}

    def test_parses_title(self, store):
        doc = store.get("alpha")
        assert doc is not None
        assert doc.title == "Alpha Procedure"

    def test_parses_purpose(self, store):
        doc = store.get("alpha")
        assert doc.purpose == "First example skill."

    def test_parses_body(self, store):
        doc = store.get("alpha")
        assert "Step one" in doc.body

    def test_missing_skill_returns_none(self, store):
        assert store.get("nonexistent") is None


class TestSkillContext:
    """Skills inject into prompts as context."""

    def test_context_for_prompt_returns_text(self, store):
        ctx = store.context_for_prompt("alpha")
        assert "Alpha Procedure" in ctx
        assert "First example skill" in ctx

    def test_context_for_missing_skill_returns_empty(self, store):
        ctx = store.context_for_prompt("nonexistent")
        assert ctx == ""

    def test_all_context_includes_all_skills(self, store):
        ctx = store.all_context()
        assert "Alpha Procedure" in ctx
        assert "Beta Procedure" in ctx


class TestSkillLookupTelemetry:
    """Lookup misses write to skill_lookups (TGD G9 contract)."""

    def test_miss_writes_lookup_row(self, store, tmp_db):
        store.get("nonexistent")
        lookups = get_skill_lookups()
        assert len(lookups) >= 1
        assert lookups[0]["skill_id"] == "nonexistent"
        assert lookups[0]["hit"] == 0

    def test_hit_does_not_write_lookup(self, store, tmp_db):
        store.get("alpha")  # hit — should NOT write a miss row
        lookups = get_skill_lookups()
        assert lookups == []

    def test_multiple_misses_all_recorded(self, store, tmp_db):
        store.get("foo")
        store.get("bar")
        lookups = get_skill_lookups()
        ids = {row["skill_id"] for row in lookups}
        assert "foo" in ids
        assert "bar" in ids
