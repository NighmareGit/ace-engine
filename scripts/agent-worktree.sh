#!/usr/bin/env bash
# agent-worktree.sh — Create an isolated worktree + branch for agent work.
#
# Usage:
#   agent-worktree.sh <branch-name>
#
# Creates ../ace-engine-<branch-suffix> as a git worktree branched from
# current HEAD. Prints the worktree path and the 3-line rules.
#
# Handles:
#   - Branch already exists -> reuse it (don't fail)
#   - Dirty main checkout -> warn (worktree is from current HEAD regardless)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_NAME="$(basename "$REPO_ROOT")"

if [[ $# -lt 1 ]]; then
    echo "Usage: agent-worktree.sh <branch-name>" >&2
    echo "Creates ../<repo>-<branch-suffix> worktree + branch from current HEAD." >&2
    exit 2
fi

BRANCH="$1"
# Derive a filesystem-friendly suffix from the branch name
# e.g. "feature/host-rails" -> "host-rails"
SUFFIX="${BRANCH##*/}"
WORKTREE_PATH="$(cd "$REPO_ROOT/.." && pwd)/${REPO_NAME}-${SUFFIX}"

echo "[agent-worktree] Branch: $BRANCH"
echo "[agent-worktree] Worktree: $WORKTREE_PATH"

# Warn if main checkout is dirty
if ! git -C "$REPO_ROOT" diff --quiet 2>/dev/null || \
   ! git -C "$REPO_ROOT" diff --cached --quiet 2>/dev/null; then
    echo "[agent-worktree] WARNING: Main checkout has uncommitted changes." >&2
    echo "  Worktree is created from current HEAD (excluding uncommitted work)." >&2
fi

# Check if worktree already exists
if [[ -d "$WORKTREE_PATH" ]]; then
    echo "[agent-worktree] Worktree already exists at $WORKTREE_PATH — reusing."
    EXISTING_BRANCH="$(git -C "$WORKTREE_PATH" branch --show-current)"
    if [[ "$EXISTING_BRANCH" != "$BRANCH" ]]; then
        echo "[agent-worktree] WARNING: Existing worktree is on branch '$EXISTING_BRANCH', not '$BRANCH'." >&2
        echo "  Run: git -C $WORKTREE_PATH checkout $BRANCH" >&2
    fi
    echo ""
    echo "$WORKTREE_PATH"
    echo ""
    cat <<'RULES'
=== 3-LINE RULES ===
1. Work ONLY in the worktree path above. Never touch the main checkout.
2. The main checkout is bookkeeping — commits there will be lost or cause conflicts.
3. Push your branch when done: git push -u origin <branch-name>
RULES
    exit 0
fi

# Create worktree + branch
if git -C "$REPO_ROOT" rev-parse --verify "$BRANCH" >/dev/null 2>&1; then
    # Branch exists — create worktree from existing branch
    echo "[agent-worktree] Branch '$BRANCH' already exists — creating worktree from it."
    git -C "$REPO_ROOT" worktree add "$WORKTREE_PATH" "$BRANCH"
else
    # Create new branch from current HEAD
    git -C "$REPO_ROOT" worktree add -b "$BRANCH" "$WORKTREE_PATH"
fi

echo ""
echo "$WORKTREE_PATH"
echo ""
cat <<'RULES'
=== 3-LINE RULES ===
1. Work ONLY in the worktree path above. Never touch the main checkout.
2. The main checkout is bookkeeping — commits there will be lost or cause conflicts.
3. Push your branch when done: git push -u origin <branch-name>
RULES
