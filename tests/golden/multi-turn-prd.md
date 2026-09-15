# Iterative API Design

## Summary

Design a REST API through iterative refinement across multiple turns. The initial draft is produced, then refined based on human feedback.

## Turn 1: Initial Draft

Generate an OpenAPI 3.0 spec for a simple "Books" API with:

- `GET /books` — list all books
- `POST /books` — create a book
- `GET /books/{id}` — get a book by ID

## Turn 2: Human Feedback

Add the following refinements:

- Each book must have fields: `id`, `title`, `author`, `year`.
- `POST /books` must validate that `title` and `author` are non-empty strings.
- `GET /books` should support a query param `?author=` to filter.

## Turn 3: Final Polish

Add error responses:

- `400` with `{"error": "validation failed"}` for bad POST bodies.
- `404` with `{"error": "not found"}` for missing book IDs.
- `GET /books/{id}` should return `404` if the book doesn't exist.

## Acceptance Criteria

- The final spec is valid OpenAPI 3.0 YAML.
- All three turns are represented in the transcript.
- The spec includes error responses for all endpoints.
