"""
streaming_server.py — CLI entry point for the ACE Watch streaming server.

All implementation lives in streaming_core.py and streaming_routes.py.
This file only parses CLI arguments and starts Uvicorn.
"""
from __future__ import annotations

import argparse, logging, os, signal
from typing import Any, Optional

import uvicorn

from streaming_routes import create_app  # noqa: F401 — re-export for backward compat

DB_DEFAULT_PATH: str = "streaming-events.db"
RING_BUFFER_MAXLEN: int = 500

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    datefmt="%Y-%m-%dT%H:%M:%S")
logger = logging.getLogger("streaming_server")


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    p = argparse.ArgumentParser(
        description="ACE Live Streaming Server — SSE event broadcasting with SQLite persistence.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 streaming_server.py\n"
            "  python3 streaming_server.py --port 3081 --db streaming-events.db\n"
            "  python3 streaming_server.py --host 0.0.0.0 --port 3081\n"
        ),
    )
    p.add_argument("--port", type=int, default=3081,
                   help="Port to listen on (default: 3081)")
    p.add_argument("--host", type=str, default="127.0.0.1",
                   help="Host to bind to (default: 127.0.0.1)")
    p.add_argument("--db", type=str, default=DB_DEFAULT_PATH,
                   help=f"SQLite database path (default: {DB_DEFAULT_PATH})")
    p.add_argument("--token", type=str, default=None,
                   help="bearer <token> for authenticating POST /events requests")
    p.add_argument("--ring-size", type=int, default=RING_BUFFER_MAXLEN,
                   help=f"Ring buffer size (default: {RING_BUFFER_MAXLEN})")
    p.add_argument("--log-level", type=str, default="info",
                   choices=["debug", "info", "warning", "error"],
                   help="Logging level (default: info)")
    p.add_argument("--workers", type=int, default=1,
                   help="Number of Uvicorn workers (default: 1)")
    p.add_argument("--with-dsh", action="store_true", default=False,
                   help="Auto-start the DSH adapter alongside the streaming server")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    """Entry point: parse args, configure logging, start Uvicorn."""
    args = parse_args(argv)

    log_level_num = getattr(logging, args.log_level.upper(), logging.INFO)
    logging.getLogger().setLevel(log_level_num)
    db_path = os.path.expanduser(args.db)
    app = create_app(db_path=db_path, ring_buffer_size=args.ring_size,
                     auth_token=args.token)
    def _signal_handler(sig: int, frame: Any) -> None:
        logger.info("Received signal %s, shutting down...", signal.Signals(sig).name)
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    logger.info(
        "Starting streaming server on %s:%d (db=%s, ring_size=%d)",
        args.host, args.port, db_path, args.ring_size,
    )

    if getattr(args, 'with_dsh', False):
        try:
            from dsh_adapter import DSHAdapter
            import asyncio
            import threading
            dsh = DSHAdapter(
                dsh_url="http://127.0.0.1:3080",
                stream_url=f"http://127.0.0.1:{args.port}",
            )
            dsh_thread = threading.Thread(target=lambda: asyncio.run(dsh.start()), daemon=True)
            dsh_thread.start()
            logger.info("DSH adapter started")
        except Exception as exc:
            logger.warning("Failed to start DSH adapter: %s", exc)

    uvicorn.run(app, host=args.host, port=args.port,
        workers=args.workers, log_level=args.log_level, access_log=True,
    )

if __name__ == "__main__":
    main()
