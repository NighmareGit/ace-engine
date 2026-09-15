# Expected Atom Trace Schema

## Overview

Each golden PRD has a companion `.atoms.json` file that records the expected state transitions (atom trace) for that run. Atom traces are populated by running the engine in `RECORD=1` mode.

## File Location

```
tests/golden/<prd-name>.atoms.json
```

## Top-Level Schema

```json
{
  "run_id": "golden-<prd-name>",
  "engine_commit": "<git-sha-at-record-time>",
  "prd_file": "<name>.md",
  "transport_mode": "mock",
  "atoms": [...]
}
```

| Field            | Type     | Description                                              |
|------------------|----------|----------------------------------------------------------|
| `run_id`         | string   | Format: `golden-<prd-name>` (e.g., `golden-test-prd`)   |
| `engine_commit`  | string   | Git SHA of engine at record time; empty string if unrecorded |
| `prd_file`       | string   | Relative path to the PRD file within `tests/golden/`     |
| `transport_mode` | string   | Always `"mock"` for golden corpus                        |
| `atoms`          | array    | Ordered list of atom trace entries                       |

## Atom Entry Schema

Each element in `atoms`:

```json
{
  "atom_id": "a001",
  "task_id": "T01",
  "seq": 1,
  "from_state": "IDLE",
  "to_state": "PARSING",
  "input_hash": "sha256-hex",
  "output_hash": "sha256-hex",
  "gate_hash": "sha256-hex-or-null",
  "duration_ms": 123,
  "meta_json": "{}"
}
```

| Field          | Type   | Description                                               |
|----------------|--------|-----------------------------------------------------------|
| `atom_id`      | string | Unique atom identifier, matches transcript `atom_id`      |
| `task_id`      | string | Engine task identifier (e.g., `T01`)                      |
| `seq`          | int    | Sequence number within the run (1-indexed)                |
| `from_state`   | string | State enum value before transition                        |
| `to_state`     | string | State enum value after transition                         |
| `input_hash`   | string | SHA-256 hex of the input data                             |
| `output_hash`  | string | SHA-256 hex of the output data                            |
| `gate_hash`    | string | SHA-256 hex of gate result, or `null` if no gate          |
| `duration_ms`  | int    | Wall-clock duration of the transition in milliseconds     |
| `meta_json`    | string | JSON-encoded metadata string (default `"{}"`)             |

## State Enum Values

Valid values for `from_state` and `to_state` (from `engine/pipeline.py`):

```
IDLE, PARSING, QUEUED, CONTEXT, GENERATE, VALIDATE, TEST, COMMIT, NEXT, DONE, FAILED, CANCELLED
```

## Hash Convention

All hashes are lowercase hex-encoded SHA-256 digests (64 characters). Use `"null"` (JSON null) for `gate_hash` when no gate check was performed.
