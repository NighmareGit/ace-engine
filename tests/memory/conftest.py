"""Shared fixtures for tests/memory/."""

from __future__ import annotations

import pytest

from engine.state import init_db


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Fresh temp-file DB for each test (not :memory: because the gate opens
    separate connections)."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("ENGINE_DB_PATH", db_path)
    init_db(db_path)
    return db_path


@pytest.fixture()
def gate(db):
    from engine.memory import gate
    return gate


@pytest.fixture()
def recall(db):
    from engine.memory.recall import recall_for_objective
    return recall_for_objective
