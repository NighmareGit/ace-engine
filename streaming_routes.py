"""
streaming_routes.py — Route handlers and dashboard config for the streaming server.

Extracted from ``streaming_server.py`` (S.2) for modularity.  Contains the
FastAPI application factory (``create_app``) with all HTTP route handlers,
dashboard configuration helpers, and route-related constants.

This module depends on ``streaming_core`` for server state, SSE formatting,
and background tasks.

Usage::

    from streaming_routes import create_app
    app = create_app(db_path="streaming-events.db")
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

# ---------------------------------------------------------------------------
# Local imports
# ---------------------------------------------------------------------------

from streaming_core import (
    StreamingServer,
    _format_sse,
    _periodic_cleanup,
    generate_ulid,
    HEARTBEAT_INTERVAL,
    RING_BUFFER_MAXLEN,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logger = logging.getLogger("streaming_server")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DB_DEFAULT_PATH: str = "streaming-events.db"
MAX_EVENT_PAYLOAD_BYTES: int = 64 * 1024  # 64 KB max per event
MAX_SSE_CLIENTS: int = 50  # Max concurrent SSE connections
DASHBOARD_DIR: str = "dashboard"
DASHBOARD_FILE: str = "index.html"
SERVER_START_TIME: float = time.time()  # module-level for /health uptime
ENGINE_SERVICE_URL: str = os.environ.get("ENGINE_SERVICE_URL", "http://127.0.0.1:3082")

# ---------------------------------------------------------------------------
# Dashboard config helpers
# ---------------------------------------------------------------------------

_DASHBOARD_CONFIG_PATH = None  # Set in create_app


def _load_dashboard_config() -> dict:
    """Load dashboard config from JSON file."""
    global _DASHBOARD_CONFIG_PATH
    path = _DASHBOARD_CONFIG_PATH
    if path is None:
        return {"highlight_tasks": [], "focus_source": "both",
                "auto_scroll": True, "custom_panels": [], "annotations": []}
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"highlight_tasks": [], "focus_source": "both",
                "auto_scroll": True, "custom_panels": [], "annotations": []}


def _save_dashboard_config(config: dict) -> None:
    """Save dashboard config to JSON file."""
    global _DASHBOARD_CONFIG_PATH
    path = _DASHBOARD_CONFIG_PATH
    if path is None:
        return
    try:
        with open(path, "w") as f:
            json.dump(config, f, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------


def create_app(db_path: str = DB_DEFAULT_PATH, ring_buffer_size: int = RING_BUFFER_MAXLEN,
               auth_token: str | None = None) -> FastAPI:
    """Create and configure the FastAPI application.

    Parameters
    ----------
    db_path:
        Filesystem path to the SQLite database.
    ring_buffer_size:
        Maximum events to retain in the ring buffer.

    Returns
    -------
    FastAPI
        A configured ASGI application ready to be served by Uvicorn.
    """
    # Attach server state to app (created before lifespan so the closure captures it)
    server = StreamingServer(db_path=db_path, ring_buffer_size=ring_buffer_size,
                             auth_token=auth_token)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Startup: init DB, start cleanup task. Shutdown: cancel task, close SSE clients."""
        await server.init_db()
        app.state._cleanup_task = asyncio.create_task(_periodic_cleanup(server))
        logger.info(
            "Streaming server started: db=%s ring_buffer=%d",
            db_path,
            ring_buffer_size,
        )
        yield
        # Shutdown
        task = getattr(app.state, "_cleanup_task", None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        server.close_db()
        for cid in list(server.sse_manager._clients.keys()):
            server.sse_manager.disconnect(cid)
        logger.info("Streaming server shut down.")

    app = FastAPI(
        title="ACE Live Streaming Server",
        description=(
            "Server-Sent Events streaming server for the Autonomous Coding "
            "Engine.  Accepts events from engine, DSH, and system producers, "
            "persists to SQLite WAL, and broadcasts via SSE."
        ),
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    app.state.server = server

    # Dashboard config file lives alongside the DB
    global _DASHBOARD_CONFIG_PATH
    _DASHBOARD_CONFIG_PATH = os.path.join(os.path.dirname(db_path), "dashboard-config.json")

    # ------------------------------------------------------------------
    # POST /events — single event ingestion
    # ------------------------------------------------------------------

    @app.post("/events", status_code=201)
    async def post_event(request: Request) -> JSONResponse:
        """Ingest a single event.

        The request body must be a JSON object with at least ``source``
        and ``type`` fields.  The server auto-assigns ``id``, ``seq``,
        and ``time`` if missing.

        Maximum event payload size is 64 KB. Larger payloads are rejected
        with 413.

        Returns
        -------
        JSONResponse
            201 with the completed event envelope (including server-assigned
            ``id``, ``seq``, ``time``).
        """
        if not server.check_auth(request):
            return JSONResponse(status_code=401, content={"error": "Unauthorized"})
        # Enforce max body size (64 KB per event)
        content_length = request.headers.get("content-length", "0")
        try:
            if int(content_length) > MAX_EVENT_PAYLOAD_BYTES:
                return JSONResponse(status_code=413, content={"error": "Event payload too large"})
        except ValueError:
            pass
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return JSONResponse(
                status_code=400,
                content={"error": "Invalid JSON"},
            )

        try:
            result = await server.ingest_event(payload)
        except ValueError as exc:
            return JSONResponse(
                status_code=422,
                content={"error": str(exc)},
            )
        except Exception as exc:
            logger.exception("Error ingesting event")
            return JSONResponse(
                status_code=500,
                content={"error": f"Internal error: {exc}"},
            )

        return JSONResponse(status_code=201, content=result)

    # ------------------------------------------------------------------
    # POST /events/batch — batch event ingestion
    # ------------------------------------------------------------------

    @app.post("/events/batch", status_code=201)
    async def post_event_batch(request: Request) -> JSONResponse:
        """Ingest a batch of events.

        The request body must be a JSON object with an ``events`` key
        containing an array of event envelopes.  Each envelope must have
        at least ``source`` and ``type``.

        Returns
        -------
        JSONResponse
            201 with ``{"count": N, "events": [...]}``.
        """
        if not server.check_auth(request):
            return JSONResponse(status_code=401, content={"error": "Unauthorized"})
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse(
                status_code=400,
                content={"error": "Invalid JSON"},
            )

        events = body.get("events") if isinstance(body, dict) else body
        if not isinstance(events, list):
            return JSONResponse(
                status_code=422,
                content={"error": "Request body must contain an 'events' array"},
            )

        try:
            results = await server.ingest_batch(events)
        except ValueError as exc:
            return JSONResponse(
                status_code=422,
                content={"error": str(exc)},
            )
        except Exception as exc:
            logger.exception("Error ingesting event batch")
            return JSONResponse(
                status_code=500,
                content={"error": f"Internal error: {exc}"},
            )

        return JSONResponse(
            status_code=201,
            content={"count": len(results), "events": results},
        )

    # ------------------------------------------------------------------
    # GET /stream — SSE stream
    # ------------------------------------------------------------------

    @app.get("/stream")
    async def get_stream(
        request: Request,
        since: Optional[str] = Query(None, description="Replay events after this event ID"),
        source: Optional[str] = Query(None, description="Filter by source: engine|dsh|system"),
        event_type: Optional[str] = Query(None, alias="type", description="Filter by event type"),
    ) -> StreamingResponse:
        """Server-Sent Events stream.

        Connects the client to the live event stream.  Supports
        ``Last-Event-ID`` header (SSE standard) and ``?since=<id>``
        query parameter for reconnection replay.

        If the ring buffer doesn't go back far enough, falls back to
        a SQLite query.

        Parameters
        ----------
        request:
            The incoming HTTP request (used for ``Last-Event-ID`` header).
        since:
            Event ID to replay from.  Events with ``seq > last_seq``
            of this ID are replayed before the live stream begins.
        source:
            Optional source filter.
        event_type:
            Optional event type filter.

        Returns
        -------
        StreamingResponse
            An SSE stream with ``text/event-stream`` content type.
        """
        if not server.check_auth(request):
            return JSONResponse(status_code=401, content={"error": "Unauthorized"})
        # Determine replay start point
        last_seq = 0
        replay_id = request.headers.get("Last-Event-ID") or since
        if replay_id:
            # The SSE id field is the seq number (set in _format_sse).
            # Parse it directly instead of querying the ULID column.
            try:
                last_seq = int(replay_id)
            except (ValueError, TypeError):
                # Fallback: look up by ULID if a non-numeric id was sent
                def _lookup_seq() -> Optional[int]:
                    row = server._db_conn.execute(
                        "SELECT seq FROM live_events WHERE id = ?;",
                        (replay_id,),
                    ).fetchone()
                    return row[0] if row else None

                loop = asyncio.get_event_loop()
                seq = await loop.run_in_executor(None, _lookup_seq)
                if seq is not None:
                    last_seq = seq

        async def event_generator() -> AsyncGenerator[str, None]:
            """Generate SSE events for this client."""
            # Enforce max concurrent SSE connections
            if server.sse_manager.client_count >= MAX_SSE_CLIENTS:
                return
            client_id, queue = server.sse_manager.connect()
            try:
                # Phase 1: Replay from ring buffer
                replay_events = await server.ring_buffer.replay_since(last_seq)

                # Apply source/type filters to replay
                for event in replay_events:
                    if source and event.get("source") != source:
                        continue
                    if event_type:
                        evt_type = event.get("type", "")
                        if "*" in event_type:
                            if not fnmatch.fnmatch(evt_type, event_type):
                                continue
                        elif evt_type != event_type:
                            continue
                    yield _format_sse(event)

                # If ring buffer replayed fewer events than expected (gap),
                # fall back to SQLite
                if last_seq > 0 and not replay_events:
                    db_events = await server.query_events(
                        since_seq=last_seq,
                        source=source,
                        event_type=event_type,
                        limit=RING_BUFFER_MAXLEN,
                    )
                    for event in db_events:
                        yield _format_sse(event)

                # Phase 2: Live stream
                while True:
                    try:
                        # Wait for next event with heartbeat timeout
                        event = await asyncio.wait_for(
                            queue.get(),
                            timeout=HEARTBEAT_INTERVAL,
                        )
                    except asyncio.TimeoutError:
                        # Send heartbeat
                        yield ": ping\n\n"
                        continue

                    # Sentinel: client disconnected
                    if event is None:
                        break

                    # Apply filters
                    if source and event.get("source") != source:
                        continue
                    if event_type:
                        evt_type = event.get("type", "")
                        if "*" in event_type:
                            if not fnmatch.fnmatch(evt_type, event_type):
                                continue
                        elif evt_type != event_type:
                            continue

                    yield _format_sse(event)

            except asyncio.CancelledError:
                pass
            finally:
                server.sse_manager.disconnect(client_id)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                "X-SSE-Client-ID": "",  # placeholder, set per-response if needed
            },
        )

    # ------------------------------------------------------------------
    # GET / — serve dashboard
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def get_dashboard() -> Response:
        """Serve the dashboard HTML page.

        Reads ``dashboard/index.html`` relative to the script's directory.
        Returns a 404 if the file does not exist.
        """
        dashboard_path = (
            Path(__file__).parent / DASHBOARD_DIR / DASHBOARD_FILE
        )
        if not dashboard_path.exists():
            # Fallback: try relative to cwd
            dashboard_path = Path.cwd() / DASHBOARD_DIR / DASHBOARD_FILE

        if not dashboard_path.exists():
            return HTMLResponse(
                status_code=404,
                content=(
                    "<!DOCTYPE html><html><head><title>Dashboard Not Found</title></head>"
                    "<body><h1>Dashboard Not Found</h1>"
                    "<p>Create <code>dashboard/index.html</code> in the coder-harness directory.</p>"
                    "</body></html>"
                ),
            )

        html = dashboard_path.read_text(encoding="utf-8")
        return HTMLResponse(content=html)

    # ------------------------------------------------------------------
    # GET /health — health check
    # ------------------------------------------------------------------

    @app.get("/health")
    async def get_health() -> JSONResponse:
        """Server health check.

        Returns
        -------
        JSONResponse
            ``{"status": "ok", "events": <count>, "uptime": <seconds>}``
        """
        event_count = await server.get_event_count()
        uptime = round(time.time() - SERVER_START_TIME, 1)
        return JSONResponse(content={
            "status": "ok",
            "events": event_count,
            "uptime": uptime,
        })

    # ------------------------------------------------------------------
    # GET /api/snapshot — dashboard state
    # ------------------------------------------------------------------

    @app.get("/api/snapshot")
    async def get_snapshot(request: Request) -> JSONResponse:
        """Return the current dashboard state.

        Includes recent events from the ring buffer, total event count,
        uptime, SSE client count, and ring buffer stats.

        Returns
        -------
        JSONResponse
            Full snapshot object.
        """
        if not server.check_auth(request):
            return JSONResponse(status_code=401, content={"error": "Unauthorized"})
        snapshot = await server.get_snapshot()
        return JSONResponse(content=snapshot)

    # ------------------------------------------------------------------
    # GET /events/recent — recent events (convenience)
    # ------------------------------------------------------------------

    @app.get("/events/recent")
    async def get_recent_events(
        limit: int = Query(100, ge=1, le=1000, description="Max events to return"),
    ) -> JSONResponse:
        """Return the most recent events from the ring buffer.

        Parameters
        ----------
        limit:
            Maximum number of events to return (1-1000).

        Returns
        -------
        JSONResponse
            ``{"events": [...]}``
        """
        all_events = await server.ring_buffer.get_all()
        # Return the last N events (most recent)
        recent = all_events[-limit:]
        return JSONResponse(content={"events": recent})

    # ------------------------------------------------------------------
    # POST /exec — proxy to engine service /exec endpoint
    # ------------------------------------------------------------------

    @app.post("/exec")
    async def post_exec_proxy(request: Request) -> JSONResponse:
        """Proxy command execution to the engine service.

        Forwards the request body to ENGINE_SERVICE_URL/exec and returns
        the response. Used by the dashboard command bar.
        """
        if not server.check_auth(request):
            return JSONResponse(status_code=401, content={"error": "Unauthorized"})
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

        command = body.get("command", "")
        if not command:
            return JSONResponse(status_code=422, content={"error": "Missing 'command' field"})

        # Forward to engine service
        import urllib.request as _urlreq
        try:
            req = _urlreq.Request(
                f"{ENGINE_SERVICE_URL}/exec",
                data=json.dumps({"command": command}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with _urlreq.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            return JSONResponse(content=result)
        except Exception as exc:
            return JSONResponse(
                status_code=502,
                content={"error": f"Engine service unreachable: {exc}"},
            )

    # ------------------------------------------------------------------
    # POST /api/inject — inject custom events into the SSE stream
    # ------------------------------------------------------------------

    @app.post("/api/inject")
    async def post_inject_event(request: Request) -> JSONResponse:
        """Inject a custom event into the SSE stream.

        Body: {"type": "system.annotation", "data": {"text": "...", "style": "info"}}

        Valid types: system.annotation, system.panel, system.highlight,
                     system.focus, system.command
        """
        if not server.check_auth(request):
            return JSONResponse(status_code=401, content={"error": "Unauthorized"})
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

        event_type = payload.get("type", "")
        data = payload.get("data", {})

        valid_types = {"system.annotation", "system.panel", "system.highlight",
                       "system.focus", "system.command"}
        if event_type not in valid_types:
            return JSONResponse(status_code=422, content={
                "error": f"Invalid type. Must be one of: {sorted(valid_types)}"})

        # Build envelope and ingest
        envelope = {
            "source": "system",
            "type": event_type,
            "data": data,
        }
        try:
            result = await server.ingest_event(envelope)
            return JSONResponse(status_code=201, content=result)
        except Exception as exc:
            return JSONResponse(status_code=500, content={"error": str(exc)})

    # ------------------------------------------------------------------
    # GET /api/config — get current dashboard configuration
    # ------------------------------------------------------------------

    @app.get("/api/config")
    async def get_dashboard_config() -> JSONResponse:
        """Return the current dashboard configuration."""
        config = _load_dashboard_config()
        return JSONResponse(content=config)

    # ------------------------------------------------------------------
    # POST /api/config — update dashboard configuration
    # ------------------------------------------------------------------

    @app.post("/api/config")
    async def post_dashboard_config(request: Request) -> JSONResponse:
        """Update dashboard configuration.

        Body: {"highlight_tasks": ["T03"], "focus_source": "engine",
               "custom_panels": [...], "auto_scroll": true}
        """
        if not server.check_auth(request):
            return JSONResponse(status_code=401, content={"error": "Unauthorized"})
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

        # Merge with existing config
        current = _load_dashboard_config()
        for key in ("highlight_tasks", "focus_source", "auto_scroll", "custom_panels", "annotations"):
            if key in payload:
                current[key] = payload[key]
        _save_dashboard_config(current)

        # Emit a config.changed event so dashboard live-updates
        try:
            await server.ingest_event({
                "source": "system",
                "type": "system.config_changed",
                "data": current,
            })
        except Exception:
            pass

        return JSONResponse(content={"ok": True, "config": current})

    # ------------------------------------------------------------------
    # POST /api/panel — add/update a custom panel
    # ------------------------------------------------------------------

    @app.post("/api/panel")
    async def post_custom_panel(request: Request) -> JSONResponse:
        """Add or update a custom panel on the dashboard.

        Body: {"title": "My Panel", "content": "markdown content", "position": "bottom"}
        """
        if not server.check_auth(request):
            return JSONResponse(status_code=401, content={"error": "Unauthorized"})
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

        title = payload.get("title", "Untitled")
        content = payload.get("content", "")
        position = payload.get("position", "bottom")

        # Generate panel ID from title
        panel_id = "panel-" + hashlib.md5(title.encode()).hexdigest()[:8]

        config = _load_dashboard_config()
        panels = config.get("custom_panels", [])

        # Update existing panel with same ID, or add new
        existing = next((p for p in panels if p.get("id") == panel_id), None)
        panel = {"id": panel_id, "title": title, "content": content,
                 "position": position, "created_by": "agent"}
        if existing:
            existing.update(panel)
        else:
            panels.append(panel)
        config["custom_panels"] = panels
        _save_dashboard_config(config)

        # Inject panel event for live update
        try:
            await server.ingest_event({
                "source": "system",
                "type": "system.panel",
                "data": panel,
            })
        except Exception:
            pass

        return JSONResponse(status_code=201, content={"ok": True, "panel": panel})

    # ------------------------------------------------------------------
    # DELETE /api/panel/<panel_id> — remove a custom panel
    # ------------------------------------------------------------------

    @app.delete("/api/panel/{panel_id}")
    async def delete_custom_panel(panel_id: str, request: Request) -> JSONResponse:
        """Remove a custom panel from the dashboard."""
        if not server.check_auth(request):
            return JSONResponse(status_code=401, content={"error": "Unauthorized"})

        config = _load_dashboard_config()
        panels = config.get("custom_panels", [])
        config["custom_panels"] = [p for p in panels if p.get("id") != panel_id]
        _save_dashboard_config(config)

        # Inject removal event
        try:
            await server.ingest_event({
                "source": "system",
                "type": "system.panel_removed",
                "data": {"id": panel_id},
            })
        except Exception:
            pass

        return JSONResponse(content={"ok": True, "removed": panel_id})

    return app
