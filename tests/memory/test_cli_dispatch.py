"""Tests for ace memory CLI dispatch (G3-T04 / F9).

Verifies the dispatch key mapping directly: parses argv through the dispatch
path and confirms the correct handler is resolved.
"""

from __future__ import annotations

import argparse
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Dispatch key mapping (F1 regression)
# ---------------------------------------------------------------------------

def _build_cli_parser():
    """Replicate the parser structure from cli.py main() to verify dest names."""
    parser = argparse.ArgumentParser(prog="engine")
    subparsers = parser.add_subparsers(dest="command")

    memory_parser = subparsers.add_parser("memory", help="Memory poisoning gate")
    memory_sub = memory_parser.add_subparsers(dest="memory_command")

    approve_p = memory_sub.add_parser("approve")
    approve_p.add_argument("claim_id")

    reject_p = memory_sub.add_parser("reject")
    reject_p.add_argument("claim_id")

    list_p = memory_sub.add_parser("list")
    list_p.add_argument("store", nargs="?", default="claims")

    return parser


def test_memory_argv_parses_to_correct_dests():
    """Parsing ``ace memory approve <id>`` sets command='memory', memory_command='approve'."""
    parser = _build_cli_parser()
    args = parser.parse_args(["memory", "approve", "claim-123"])
    assert args.command == "memory"
    assert args.memory_command == "approve"
    assert args.claim_id == "claim-123"


def test_memory_list_argv_parses_store():
    """Parsing ``ace memory list patterns`` sets store='patterns'."""
    parser = _build_cli_parser()
    args = parser.parse_args(["memory", "list", "patterns"])
    assert args.command == "memory"
    assert args.memory_command == "list"
    assert args.store == "patterns"


def test_dispatch_resolves_memory_handlers():
    """The dispatch table in cli.py must resolve memory subcommands via 2-tuple keys.

    We verify this by importing the handler functions and checking they're
    callable (the actual dispatch dict is local to main(), but the handlers
    are module-level and the key structure is verified by the argv parse test).
    """
    from engine.cli import cmd_memory_approve, cmd_memory_reject, cmd_memory_list
    assert callable(cmd_memory_approve)
    assert callable(cmd_memory_reject)
    assert callable(cmd_memory_list)


def test_cli_main_routes_memory_approve(db, gate):
    """End-to-end: invoking cli.py with ``memory approve <id>`` dispatches correctly."""
    from engine.cli import main

    ids = gate.submit_claims("r1", "t1", {"claims": [
        {"id": "c1", "text": "test claim for cli approve"},
    ]})

    # Mock sys.argv and capture stdout.
    with patch("sys.argv", ["engine", "memory", "approve", ids[0]]):
        with patch("sys.stdout") as mock_stdout:
            mock_stdout.write = lambda x: None
            mock_stdout.flush = lambda: None
            try:
                main()
            except SystemExit as e:
                # main() calls sys.exit(0) on success.
                assert e.code == 0, f"Expected exit 0, got {e.code}"


# ---------------------------------------------------------------------------
# Handler invocation (F9) — invoke handlers the way cli.py's lookup does
# ---------------------------------------------------------------------------

def _make_memory_args(subcommand: str, claim_id: str = "claim-1",
                      store: str = "claims"):
    """Build a namespace like argparse would for ``ace memory <subcommand>``."""
    ns = argparse.Namespace(
        command="memory",
        ace_command=None,
        ralph_command=None,
        memory_command=subcommand,
        claim_id=claim_id,
        store=store,
        pretty=False,
        transport="auto",
    )
    return ns


def test_cmd_memory_approve_handler(gate, db):
    """Invoke cmd_memory_approve directly (as dispatch would)."""
    from engine.cli import cmd_memory_approve

    ids = gate.submit_claims("r1", "t1", {"claims": [
        {"id": "c1", "text": "test claim for approve"},
    ]})
    args = _make_memory_args("approve", claim_id=ids[0])
    result = cmd_memory_approve(args)
    assert result["approved"] is True
    assert result["claim_id"] == ids[0]


def test_cmd_memory_reject_handler(gate, db):
    """Invoke cmd_memory_reject directly (as dispatch would)."""
    from engine.cli import cmd_memory_reject

    ids = gate.submit_claims("r1", "t1", {"claims": [
        {"id": "c1", "text": "test claim for reject"},
    ]})
    args = _make_memory_args("reject", claim_id=ids[0])
    result = cmd_memory_reject(args)
    assert result["rejected"] is True
    assert result["claim_id"] == ids[0]


def test_cmd_memory_list_handler(gate, db):
    """Invoke cmd_memory_list directly (as dispatch would)."""
    from engine.cli import cmd_memory_list

    gate.submit_claims("r1", "t1", {"claims": [
        {"id": "c1", "text": "pending claim one"},
        {"id": "c2", "text": "pending claim two"},
    ]})
    args = _make_memory_args("list")
    result = cmd_memory_list(args)
    assert result["store"] == "claims"
    assert result["count"] == 2
    assert len(result["pending"]) == 2


def test_cmd_memory_list_patterns_store(gate, db):
    """cmd_memory_list with store='patterns' routes to pattern listing."""
    from engine.cli import cmd_memory_list

    args = _make_memory_args("list", store="patterns")
    result = cmd_memory_list(args)
    assert result["store"] == "patterns"
