# RECORD=1 Regeneration Convention

## Overview

Golden traces (transcripts and atom traces) are recorded by running the engine in `RECORD=1` mode. This captures the exact model interaction and state transitions for later replay and comparison.

## Recording a Golden Trace

Set the `RECORD=1` environment variable and run the engine against a golden PRD:

```bash
RECORD=1 python3 -m engine.run tests/golden/test-prd.md
```

This will:
1. Execute the engine pipeline against the specified PRD.
2. Write the transcript to `tests/golden/test-prd.transcript.jsonl`.
3. Write the atom trace to `tests/golden/test-prd.atoms.json`.
4. Capture the current `engine_commit` from `git rev-parse HEAD`.

## Engine Commit Tracking

The `engine_commit` field in each `.atoms.json` file records the git SHA of the engine at the time the trace was recorded:

```json
{
  "run_id": "golden-test-prd",
  "engine_commit": "a1b2c3d4e5f6...",
  ...
}
```

This is captured automatically via:

```bash
git rev-parse HEAD
```

at record time.

## Stale Trace Detection

A golden trace is considered **stale** when:

```
golden_trace.engine_commit != current HEAD
```

Detect stale traces with:

```bash
cd /home/<user>/projects/ace-engine
CURRENT=$(git rev-parse HEAD)
for f in tests/golden/*.atoms.json; do
  RECORDED=$(python3 -c "import json; print(json.load(open('$f'))['engine_commit'])")
  if [ "$RECORDED" != "$CURRENT" ] && [ -n "$RECORDED" ]; then
    echo "STALE: $f (recorded=$RECORDED, current=$CURRENT)"
  fi
done
```

## Re-Recording Policy

When engine code changes, **all golden traces must be re-recorded before merging**:

1. Make engine changes.
2. Run `RECORD=1` for each golden PRD.
3. Verify traces pass replay validation (T62/T66).
4. Commit updated `.atoms.json` and `.transcript.jsonl` files.

## Transport Mode

Golden traces always use `transport_mode: "mock"` to ensure reproducibility without network or model dependencies.
