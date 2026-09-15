# Transcript Fixture Format

## Overview

Golden transcripts are stored as JSONL (one JSON object per line). Each line represents a single message turn in the engine's conversation with the model.

## File Naming

Each golden PRD has a companion transcript file:

```
tests/golden/<prd-name>.transcript.jsonl
```

## Record Format

Every line is a JSON object with these fields:

### User Turn

```json
{"turn": 1, "role": "user", "content": "...", "atom_id": "a001", "timestamp": "2025-01-15T10:30:00Z"}
```

### Assistant Turn

```json
{"turn": 1, "role": "assistant", "content": "...", "model": "beellama", "tokens": 256, "atom_id": "a001", "timestamp": "2025-01-15T10:30:01Z"}
```

## Required Fields

| Field       | Type   | Roles        | Description                                      |
|-------------|--------|--------------|--------------------------------------------------|
| `turn`      | int    | both         | Turn number (1-indexed, increments per user msg) |
| `role`      | string | both         | `"user"` or `"assistant"`                        |
| `content`   | string | both         | The message text                                 |
| `atom_id`   | string | both         | Matches the atom trace entry for this exchange    |
| `timestamp` | string | both         | ISO 8601 timestamp                               |
| `model`     | string | assistant    | Model identifier used for generation             |
| `tokens`    | int    | assistant    | Token count for the response                     |

## Atom ID Convention

Atom IDs follow the pattern `a001`, `a002`, etc. Each user-assistant pair in a single task shares one `atom_id`. When multiple tasks are processed, atom IDs continue sequentially.

## Example

```jsonl
{"turn": 1, "role": "user", "content": "Create a health check endpoint at /health.", "atom_id": "a001", "timestamp": "2025-01-15T10:30:00Z"}
{"turn": 1, "role": "assistant", "content": "I'll create a health check endpoint...", "model": "beellama", "tokens": 128, "atom_id": "a001", "timestamp": "2025-01-15T10:30:01Z"}
```
