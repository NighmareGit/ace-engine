# String Utility Library

## Summary

Create a two-module string utility library. Task A provides helper functions; Task B builds a higher-level module that imports and uses Task A.

## Tasks

### Task A: String Helpers (`string_helpers.py`)

Create a utility module with the following functions:

1. `capitalize_words(s: str) -> str` — Capitalize the first letter of each word in `s`.
2. `slugify(s: str) -> str` — Convert `s` to a URL-friendly slug (lowercase, hyphens, no special chars).

### Task B: Text Processor (`text_processor.py`)

Create a module that imports from `string_helpers` and provides:

1. `process_title(title: str) -> dict` — Returns `{"title": capitalize_words(title), "slug": slugify(title)}`.

## Expected Failure & Retry Scenario

Task B depends on Task A. The expected failure scenario during engine execution:

- **First attempt**: Task B fails because `string_helpers` does not yet exist (import error). The engine marks Task B as `FAILED`.
- **Retry**: After Task A completes successfully, Task B is retried and succeeds because the dependency is now available.

This exercises the engine's failure-retry path and state transitions: `GENERATE → VALIDATE → FAILED → QUEUED → GENERATE → ... → DONE`.

## Acceptance Criteria

- `string_helpers.py` exists with both functions working correctly.
- `text_processor.py` imports from `string_helpers` and `process_title("hello world")` returns `{"title": "Hello World", "slug": "hello-world"}`.
