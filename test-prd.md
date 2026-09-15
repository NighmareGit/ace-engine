# Test Feature: Health Check API Endpoint

## Problem Statement
The project needs a lightweight health check endpoint so that monitoring
tools and load balancers can verify the service is alive and responsive.

## User Stories
1. As a DevOps engineer, I want a `/health` endpoint that returns HTTP 200, so that load balancers can confirm the service is up
2. As a developer, I want the `/health` endpoint to return a JSON payload with a `status` field set to `"ok"` and a `timestamp` field, so that I can verify the service is responding correctly
3. As a developer, I want a `/` root endpoint that returns a JSON greeting with the service name and version, so that I can quickly identify which service I'm talking to

## Technical Requirements
- Use FastAPI as the web framework
- Use uvicorn as the ASGI server
- The `/health` endpoint should respond in under 50ms
- The `/` endpoint should return `{"service": "ace-demo", "version": "0.1.0"}`
- Include a `requirements.txt` with `fastapi` and `uvicorn`
- Include a `test_app.py` with pytest tests for both endpoints
- Include a `Dockerfile` for containerized deployment

## Acceptance Criteria
- GET `/health` returns `{"status": "ok", "timestamp": "<iso8601>"}`
- GET `/` returns `{"service": "ace-demo", "version": "0.1.0"}`
- `pytest test_app.py` passes all tests
- `python -m uvicorn app:app` starts the server on port 8000
- Docker image builds and starts successfully
