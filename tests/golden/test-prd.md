# Health Check Endpoint

## Summary

Create a health check endpoint at `/health` that returns `{"status": "ok"}`.

## Requirements

1. Add a `GET /health` route to the application.
2. The response body must be exactly `{"status": "ok"}` with content type `application/json`.
3. The endpoint must return HTTP 200.

## Acceptance Criteria

- `curl http://localhost:PORT/health` returns `{"status": "ok"}` with status 200.
- No external dependencies are required for this endpoint.
