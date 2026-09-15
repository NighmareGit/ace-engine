#!/usr/bin/env python3
"""
Coder Harness Telemetry Dashboard Generator

Reads from:
  - ~/coder-harness-telemetry.db (work sessions + events)
  - ~/projects/dsh-hub/coder-harness/benchmark-results.db (benchmarks, configs, GPU fit)

Produces:
  - reports/telemetry-dashboard.html  (single self-contained HTML file)

Usage:
    python3 telemetry_dashboard.py [--output path/to/output.html] [--open]
"""

import sqlite3
import os
import sys
import html
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ─── Paths ────────────────────────────────────────────────────────────────────

TELEMETRY_DB = os.path.expanduser("~/coder-harness-telemetry.db")
BENCHMARK_DB = os.path.expanduser("~/projects/dsh-hub/coder-harness/benchmark-results.db")
DEFAULT_OUTPUT = "reports/telemetry-dashboard.html"


class TelemetryDashboard:
    """Generates a self-contained HTML dashboard from SQLite telemetry data."""

    def __init__(self):
        self.telemetry_db = TELEMETRY_DB
        self.benchmark_db = BENCHMARK_DB
        self._bench_conn: Optional[sqlite3.Connection] = None
        self._tele_conn: Optional[sqlite3.Connection] = None

    def _bench_conn_get(self) -> Optional[sqlite3.Connection]:
        """Lazy-connect to benchmark DB."""
        if self._bench_conn is None:
            if os.path.exists(self.benchmark_db):
                self._bench_conn = sqlite3.connect(self.benchmark_db)
                self._bench_conn.row_factory = sqlite3.Row
                self._bench_conn.execute("PRAGMA journal_mode=WAL")
            else:
                return None
        return self._bench_conn

    def _tele_conn_get(self) -> Optional[sqlite3.Connection]:
        """Lazy-connect to telemetry DB."""
        if self._tele_conn is None:
            if os.path.exists(self.telemetry_db):
                self._tele_conn = sqlite3.connect(self.telemetry_db)
                self._tele_conn.row_factory = sqlite3.Row
                self._tele_conn.execute("PRAGMA journal_mode=WAL")
            else:
                return None
        return self._tele_conn

    def _table_exists(self, conn: sqlite3.Connection, table: str) -> bool:
        """Check if a table exists in the connected database."""
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        )
        return cur.fetchone() is not None

    def _safe_query(self, conn, query, params=(), default=None):
        """Execute a query safely, returning default on error."""
        if conn is None:
            return default or []
        try:
            if not self._table_exists(conn, query):
                # This is a heuristic; actual table check happens below
                pass
        except Exception:
            pass
        try:
            return conn.execute(query, params).fetchall()
        except Exception as e:
            print(f"  [warn] Query failed: {e}", file=sys.stderr)
            return default if default is not None else []

    # ─── Data Collection ──────────────────────────────────────────────────────

    def _get_system_overview(self) -> dict:
        """Current system state — configs on each GPU, model info, uptime."""
        bench = self._bench_conn_get()
        data = {
            "gpu_3090": [],
            "gpu_3070": [],
            "gpu_fit_3090": [],
            "gpu_fit_3070": [],
            "uptime_since": None,
            "last_swap": None,
        }

        if bench and self._table_exists(bench, "model_configs"):
            rows = bench.execute(
                "SELECT * FROM model_configs ORDER BY gpu, id"
            ).fetchall()
            for r in rows:
                entry = dict(r)
                if entry.get("gpu") == "3090":
                    data["gpu_3090"].append(entry)
                elif entry.get("gpu") == "3070":
                    data["gpu_3070"].append(entry)

        # GPU fit data for memory bar charts
        if bench and self._table_exists(bench, "gpu_fit_matrix"):
            fit_rows = bench.execute(
                """SELECT model_config_id, gpu, MAX(vram_used_mb) as vram_used,
                          MAX(vram_total_mb) as vram_total, MAX(context_size) as ctx
                   FROM gpu_fit_matrix
                   WHERE fits = 1
                   GROUP BY model_config_id, gpu
                   ORDER BY gpu, vram_used DESC"""
            ).fetchall()
            for r in fit_rows:
                entry = dict(r)
                if entry.get("gpu") == "3090":
                    data["gpu_fit_3090"].append(entry)
                elif entry.get("gpu") == "3070":
                    data["gpu_fit_3070"].append(entry)

        # Telemetry DB — work sessions for uptime
        tele = self._tele_conn_get()
        if tele and self._table_exists(tele, "work_sessions"):
            swap_row = tele.execute(
                """SELECT created_at FROM work_sessions
                   WHERE status IN ('running','completed')
                   ORDER BY created_at DESC LIMIT 1"""
            ).fetchone()
            if swap_row:
                data["last_swap"] = swap_row[0]

        return data

    def _get_benchmark_results(self) -> list:
        """All benchmark results with scores."""
        bench = self._bench_conn_get()
        if bench is None:
            return []
        if not self._table_exists(bench, "benchmark_runs"):
            return []

        query = """
            SELECT
                br.id AS run_id,
                br.model_config_id,
                mc.gpu,
                mc.model_name,
                mc.quantization,
                mc.context_size,
                mc.kvarn_level,
                mc.speculative_type,
                t.id AS task_id,
                t.role,
                t.category,
                t.difficulty,
                br.status,
                br.predicted_per_second,
                br.prompt_per_second,
                br.predicted_ms,
                br.thinking_tokens,
                br.total_tokens,
                br.started_at,
                br.completed_at,
                br.error_message,
                js.overall AS score_overall,
                js.completeness,
                js.correctness,
                js.quality,
                js.intelligence,
                js.role_fit,
                js.judge_reasoning
            FROM benchmark_runs br
            JOIN model_configs mc ON br.model_config_id = mc.id
            JOIN tasks t ON br.task_id = t.id
            LEFT JOIN judge_scores js ON js.run_id = br.id AND js.judge_model = (
                SELECT js2.judge_model FROM judge_scores js2
                WHERE js2.run_id = br.id
                ORDER BY js2.scored_at DESC LIMIT 1
            )
            ORDER BY br.started_at DESC
        """
        rows = bench.execute(query).fetchall()
        return [dict(r) for r in rows]

    def _get_benchmark_summary(self) -> dict:
        """Aggregated benchmark stats per config."""
        bench = self._bench_conn_get()
        summary = {
            "per_config": [],
            "total_runs": 0,
            "complete_runs": 0,
            "failed_runs": 0,
        }

        if bench is None:
            return summary
        if not self._table_exists(bench, "benchmark_runs"):
            return summary

        # Status counts
        counts = bench.execute(
            "SELECT status, COUNT(*) as cnt FROM benchmark_runs GROUP BY status"
        ).fetchall()
        for c in counts:
            d = dict(c)
            if d["status"] == "complete":
                summary["complete_runs"] = d["cnt"]
            elif d["status"] == "failed":
                summary["failed_runs"] = d["cnt"]
            summary["total_runs"] += d["cnt"]

        # Per-config summary
        rows = bench.execute("""
            SELECT
                mc.id AS config_id,
                mc.gpu,
                mc.model_name,
                mc.quantization,
                mc.context_size,
                mc.kvarn_level,
                COUNT(br.id) AS total_runs,
                SUM(CASE WHEN br.status='complete' THEN 1 ELSE 0 END) AS complete,
                SUM(CASE WHEN br.status='failed' THEN 1 ELSE 0 END) AS failed,
                AVG(CASE WHEN br.status='complete' THEN br.predicted_per_second END) AS avg_tps,
                MAX(CASE WHEN br.status='complete' THEN br.predicted_per_second END) AS max_tps,
                AVG(CASE WHEN br.status='complete' THEN br.total_tokens END) AS avg_tokens,
                AVG(CASE WHEN br.status='complete' THEN br.thinking_tokens END) AS avg_thinking
            FROM model_configs mc
            LEFT JOIN benchmark_runs br ON br.model_config_id = mc.id
            GROUP BY mc.id
            ORDER BY avg_tps DESC
        """).fetchall()

        summary["per_config"] = [dict(r) for r in rows]
        return summary

    def _get_gpu_fit_summary(self) -> list:
        """GPU fit matrix — VRAM usage per config."""
        bench = self._bench_conn_get()
        if bench is None:
            return []
        if not self._table_exists(bench, "gpu_fit_matrix"):
            return []

        rows = bench.execute("""
            SELECT
                mc.id AS config_id,
                mc.gpu,
                mc.model_name,
                mc.quantization,
                gfm.context_size,
                gfm.fits,
                gfm.vram_used_mb,
                gfm.vram_total_mb,
                gfm.inference_ok,
                gfm.inference_tokens_per_sec,
                gfm.error_message,
                gfm.measured_at
            FROM gpu_fit_matrix gfm
            JOIN model_configs mc ON gfm.model_config_id = mc.id
            ORDER BY mc.gpu, gfm.vram_used_mb DESC, mc.id
        """).fetchall()
        return [dict(r) for r in rows]

    def _get_work_sessions(self) -> list:
        """Recent work sessions from telemetry DB."""
        tele = self._tele_conn_get()
        if tele is None:
            return []
        if not self._table_exists(tele, "work_sessions"):
            return []

        rows = tele.execute("""
            SELECT
                ws.id,
                ws.sandbox_name,
                ws.project,
                ws.branch,
                ws.config_id,
                ws.created_at,
                ws.completed_at,
                ws.status,
                ws.inference_tokens,
                ws.inference_seconds,
                ws.inference_tokens_per_sec,
                ws.git_commits,
                ws.git_files_changed,
                ws.error_message
            FROM work_sessions ws
            ORDER BY ws.created_at DESC
            LIMIT 100
        """).fetchall()
        return [dict(r) for r in rows]

    def _get_events(self, limit: int = 50) -> list:
        """Recent events from telemetry DB."""
        tele = self._tele_conn_get()
        if tele is None:
            return []
        if not self._table_exists(tele, "work_events"):
            return []

        rows = tele.execute("""
            SELECT
                we.id,
                we.session_id,
                we.event_type,
                we.event_data,
                we.timestamp,
                ws.sandbox_name,
                ws.project
            FROM work_events we
            LEFT JOIN work_sessions ws ON we.session_id = ws.id
            ORDER BY we.timestamp DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def _get_recommendations(self) -> dict:
        """Best configs based on data with evidence."""
        bench = self._bench_conn_get()
        recs = {
            "best_orchestrator": None,
            "best_coder": None,
            "best_overall_speed": None,
            "active_sandboxes": 0,
            "evidence": [],
        }

        # Active sandboxes from telemetry
        tele = self._tele_conn_get()
        if tele and self._table_exists(tele, "work_sessions"):
            try:
                cnt = tele.execute(
                    "SELECT COUNT(DISTINCT sandbox_name) FROM work_sessions WHERE status='running'"
                ).fetchone()
                recs["active_sandboxes"] = cnt[0] if cnt else 0
            except Exception:
                pass

        if bench is None:
            return recs

        # Best orchestrator config
        if self._table_exists(bench, "benchmark_runs") and self._table_exists(bench, "judge_scores"):
            orch = bench.execute("""
                SELECT
                    br.model_config_id,
                    mc.model_name,
                    mc.gpu,
                    AVG(js.overall) AS avg_score,
                    AVG(br.predicted_per_second) AS avg_tps,
                    COUNT(br.id) AS runs
                FROM benchmark_runs br
                JOIN model_configs mc ON br.model_config_id = mc.id
                JOIN judge_scores js ON js.run_id = br.id
                WHERE br.status = 'complete'
                  AND br.task_id IN (SELECT id FROM tasks WHERE role = 'orchestrator')
                GROUP BY br.model_config_id
                HAVING COUNT(br.id) >= 3
                ORDER BY avg_score DESC, avg_tps DESC
                LIMIT 1
            """).fetchone()
            if orch:
                recs["best_orchestrator"] = dict(orch)

            coder = bench.execute("""
                SELECT
                    br.model_config_id,
                    mc.model_name,
                    mc.gpu,
                    AVG(js.overall) AS avg_score,
                    AVG(br.predicted_per_second) AS avg_tps,
                    COUNT(br.id) AS runs
                FROM benchmark_runs br
                JOIN model_configs mc ON br.model_config_id = mc.id
                JOIN judge_scores js ON js.run_id = br.id
                WHERE br.status = 'complete'
                  AND br.task_id IN (SELECT id FROM tasks WHERE role = 'coder')
                GROUP BY br.model_config_id
                HAVING COUNT(br.id) >= 3
                ORDER BY avg_score DESC, avg_tps DESC
                LIMIT 1
            """).fetchone()
            if coder:
                recs["best_coder"] = dict(coder)

        # Best overall speed
        if self._table_exists(bench, "benchmark_runs"):
            speed = bench.execute("""
                SELECT
                    br.model_config_id,
                    mc.model_name,
                    mc.gpu,
                    AVG(br.predicted_per_second) AS avg_tps,
                    COUNT(br.id) AS runs
                FROM benchmark_runs br
                JOIN model_configs mc ON br.model_config_id = mc.id
                WHERE br.status = 'complete' AND br.predicted_per_second IS NOT NULL
                GROUP BY br.model_config_id
                HAVING COUNT(br.id) >= 3
                ORDER BY avg_tps DESC
                LIMIT 1
            """).fetchone()
            if speed:
                recs["best_overall_speed"] = dict(speed)

        # Evidence — top 5 fastest runs
        if self._table_exists(bench, "benchmark_runs"):
            evidence = bench.execute("""
                SELECT
                    br.model_config_id,
                    mc.gpu,
                    t.role,
                    t.difficulty,
                    br.predicted_per_second,
                    br.total_tokens,
                    js.overall AS score,
                    br.started_at
                FROM benchmark_runs br
                JOIN model_configs mc ON br.model_config_id = mc.id
                JOIN tasks t ON br.task_id = t.id
                LEFT JOIN judge_scores js ON js.run_id = br.id AND js.judge_model = (
                    SELECT js2.judge_model FROM judge_scores js2
                    WHERE js2.run_id = br.id
                    ORDER BY js2.scored_at DESC LIMIT 1
                )
                WHERE br.status = 'complete' AND br.predicted_per_second IS NOT NULL
                ORDER BY br.predicted_per_second DESC
                LIMIT 10
            """).fetchall()
            recs["evidence"] = [dict(e) for e in evidence]

        return recs

    # ─── SVG Chart Generation ─────────────────────────────────────────────────

    @staticmethod
    def _svg_bar_chart(items: list, label_key: str, value_key: str,
                       title: str, color: str = "#4fc3f7",
                       max_width: int = 560, bar_height: int = 28,
                       max_val: float = 0) -> str:
        """Generate an inline SVG bar chart."""
        if not items:
            return '<p class="empty">No data available</p>'

        labels = []
        values = []
        for item in items:
            lbl = str(item.get(label_key, "?"))
            val = item.get(value_key)
            if val is None:
                continue
            labels.append(lbl)
            values.append(float(val))

        if not values:
            return '<p class="empty">No data available</p>'

        if max_val <= 0:
            max_val = max(values) if values else 1
        if max_val == 0:
            max_val = 1

        row_height = bar_height + 8
        chart_height = len(values) * row_height + 50
        label_width = 140
        bar_area = max_width - label_width - 50

        svg_parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {max_width} {chart_height}" '
            f'style="width:100%;max-width:{max_width}px;font-family:monospace;">'
        ]
        svg_parts.append(f'<text x="{max_width//2}" y="18" text-anchor="middle" '
                         f'fill="#e0e0e0" font-size="13" font-weight="bold">{html.escape(title)}</text>')

        for i, (label, value) in enumerate(zip(labels, values)):
            y = 32 + i * row_height
            bar_w = max(2, (value / max_val) * bar_area)
            opacity = 0.5 + 0.5 * (value / max_val)

            # Label
            svg_parts.append(
                f'<text x="{label_width - 8}" y="{y + bar_height * 0.65}" '
                f'text-anchor="end" fill="#b0b0b0" font-size="11">{html.escape(label[:20])}</text>'
            )
            # Bar background
            svg_parts.append(
                f'<rect x="{label_width}" y="{y}" width="{bar_area}" height="{bar_height}" '
                f'rx="3" fill="#2a2a2a"/>'
            )
            # Bar fill
            svg_parts.append(
                f'<rect x="{label_width}" y="{y}" width="{bar_w}" height="{bar_height}" '
                f'rx="3" fill="{color}" opacity="{opacity:.2f}"/>'
            )
            # Value label
            svg_parts.append(
                f'<text x="{label_width + bar_w + 6}" y="{y + bar_height * 0.65}" '
                f'fill="#e0e0e0" font-size="11">{value:.1f}</text>'
            )

        svg_parts.append("</svg>")
        return "\n".join(svg_parts)

    @staticmethod
    def _vram_bar_chart(items: list, max_width: int = 560) -> str:
        """VRAM usage bar chart with dual-color used/remaining."""
        if not items:
            return '<p class="empty">No GPU fit data available</p>'

        bar_height = 24
        row_height = bar_height + 12
        chart_height = len(items) * row_height + 50
        label_width = 160
        bar_area = max_width - label_width - 80

        svg_parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {max_width} {chart_height}" '
            f'style="width:100%;max-width:{max_width}px;font-family:monospace;">'
        ]
        svg_parts.append(f'<text x="{max_width//2}" y="18" text-anchor="middle" '
                         f'fill="#e0e0e0" font-size="13" font-weight="bold">GPU Memory Usage</text>')

        for i, item in enumerate(items):
            y = 32 + i * row_height
            used = item.get("vram_used") or item.get("vram_used_mb") or 0
            total = item.get("vram_total") or item.get("vram_total_mb") or 24000
            config_id = item.get("config_id") or item.get("model_config_id", "?")
            gpu = item.get("gpu", "")
            pct = (used / total * 100) if total > 0 else 0
            bar_w = (used / total * bar_area) if total > 0 else 0

            # Color based on usage
            if pct > 90:
                fill = "#ef5350"
            elif pct > 75:
                fill = "#ffa726"
            else:
                fill = "#66bb6a"

            label = f"{gpu} {config_id[:15]}"
            svg_parts.append(
                f'<text x="{label_width - 8}" y="{y + bar_height * 0.65}" '
                f'text-anchor="end" fill="#b0b0b0" font-size="10">{html.escape(label)}</text>'
            )
            svg_parts.append(
                f'<rect x="{label_width}" y="{y}" width="{bar_area}" height="{bar_height}" '
                f'rx="3" fill="#2a2a2a"/>'
            )
            svg_parts.append(
                f'<rect x="{label_width}" y="{y}" width="{bar_w}" height="{bar_height}" '
                f'rx="3" fill="{fill}" opacity="0.8"/>'
            )
            svg_parts.append(
                f'<text x="{label_width + bar_w + 6}" y="{y + bar_height * 0.65}" '
                f'fill="#e0e0e0" font-size="10">{used:.0f} / {total:.0f} MB ({pct:.0f}%)</text>'
            )

        svg_parts.append("</svg>")
        return "\n".join(svg_parts)

    @staticmethod
    def _speed_chart_svg(summary: dict, max_width: int = 600) -> str:
        """Speed bar chart from benchmark summary per-config data."""
        items = []
        for cfg in summary.get("per_config", []):
            tps = cfg.get("avg_tps")
            if tps and tps > 0:
                label = f"{cfg.get('gpu','?')} {cfg.get('config_id','?')[:20]}"
                items.append({"label": label, "value": float(tps)})

        if not items:
            return '<p class="empty">No throughput data yet — run benchmarks first</p>'

        items.sort(key=lambda x: x["value"], reverse=True)
        max_val = items[0]["value"] if items else 1

        bar_height = 30
        row_height = bar_height + 10
        chart_height = len(items) * row_height + 50
        label_width = 180
        bar_area = max_width - label_width - 60

        svg_parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {max_width} {chart_height}" '
            f'style="width:100%;max-width:{max_width}px;font-family:monospace;">'
        ]
        svg_parts.append(f'<text x="{max_width//2}" y="18" text-anchor="middle" '
                         f'fill="#e0e0e0" font-size="13" font-weight="bold">Tokens/sec by Config</text>')

        for i, item in enumerate(items):
            y = 32 + i * row_height
            bar_w = max(2, (item["value"] / max_val) * bar_area)
            opacity = 0.5 + 0.5 * (item["value"] / max_val)

            svg_parts.append(
                f'<text x="{label_width - 8}" y="{y + bar_height * 0.65}" '
                f'text-anchor="end" fill="#b0b0b0" font-size="11">{html.escape(item["label"])}</text>'
            )
            svg_parts.append(
                f'<rect x="{label_width}" y="{y}" width="{bar_area}" height="{bar_height}" '
                f'rx="3" fill="#2a2a2a"/>'
            )
            svg_parts.append(
                f'<rect x="{label_width}" y="{y}" width="{bar_w}" height="{bar_height}" '
                f'rx="3" fill="#4fc3f7" opacity="{opacity:.2f}"/>'
            )
            svg_parts.append(
                f'<text x="{label_width + bar_w + 6}" y="{y + bar_height * 0.65}" '
                f'fill="#e0e0e0" font-size="11">{item["value"]:.1f} tok/s</text>'
            )

        svg_parts.append("</svg>")
        return "\n".join(svg_parts)

    # ─── HTML Rendering ───────────────────────────────────────────────────────

    @staticmethod
    def _badge(status: str) -> str:
        """Render a status badge."""
        status_l = (status or "").lower()
        badges = {
            "running":   '<span class="badge badge-running">● running</span>',
            "complete":  '<span class="badge badge-complete">✅ complete</span>',
            "completed": '<span class="badge badge-complete">✅ complete</span>',
            "failed":    '<span class="badge badge-failed">❌ failed</span>',
            "error":     '<span class="badge badge-failed">❌ error</span>',
            "pending":   '<span class="badge badge-pending">⏳ pending</span>',
            "incomplete":'<span class="badge badge-pending">⏳ incomplete</span>',
        }
        return badges.get(status_l, f'<span class="badge">{html.escape(status or "?")}</span>')

    @staticmethod
    def _ts(ts: str) -> str:
        """Format a timestamp for display."""
        if not ts:
            return "—"
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return str(ts)[:19]

    @staticmethod
    def _num(v, fmt=".1f") -> str:
        """Format a number, returning — for None."""
        if v is None:
            return "—"
        return f"{v:{fmt}}"

    @staticmethod
    def _esc(v) -> str:
        """HTML-escape a value."""
        return html.escape(str(v)) if v is not None else "—"

    def generate(self, output_path: str = DEFAULT_OUTPUT) -> str:
        """Generate the HTML dashboard. Returns the output path."""
        print(f"[dashboard] Collecting data...")
        data = {
            "system": self._get_system_overview(),
            "summary": self._get_benchmark_summary(),
            "benchmarks": self._get_benchmark_results(),
            "gpu_fit": self._get_gpu_fit_summary(),
            "sessions": self._get_work_sessions(),
            "events": self._get_events(limit=50),
            "recommendations": self._get_recommendations(),
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        print(f"[dashboard] Generating HTML...")
        html_content = self._render_html(data)

        # Write output
        out_dir = os.path.dirname(output_path)
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html_content)

        size_kb = os.path.getsize(output_path) / 1024
        print(f"[dashboard] Written to {output_path} ({size_kb:.1f} KB)")
        return output_path

    def _render_html(self, data: dict) -> str:
        """Render complete HTML page."""
        sys = data["system"]
        summ = data["summary"]
        benchmarks = data["benchmarks"]
        gpu_fit = data["gpu_fit"]
        sessions = data["sessions"]
        events = data["events"]
        recs = data["recommendations"]
        gen_at = data["generated_at"]

        # Build section content
        sec1 = self._render_system_overview(sys)
        sec2 = self._render_benchmarks(summ, gpu_fit)
        sec3 = self._render_work_sessions(sessions)
        sec4 = self._render_event_log(events)
        sec5 = self._render_recommendations(recs)

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="60">
<title>Coder Harness Telemetry Dashboard</title>
{self._css()}
</head>
<body>
<header>
  <div class="header-inner">
    <h1>⚡ Coder Harness Telemetry Dashboard</h1>
    <div class="header-meta">
      Generated: {html.escape(gen_at)} &nbsp;|&nbsp; Auto-refresh: 60s
    </div>
  </div>
</header>

<main>
  <section id="system-overview">
    <h2>🖥️ System Overview</h2>
    {sec1}
  </section>

  <section id="benchmarks">
    <h2>📊 Benchmark Results</h2>
    {sec2}
  </section>

  <section id="work-sessions">
    <h2>🔧 Work Sessions</h2>
    {sec3}
  </section>

  <section id="event-log">
    <h2>📋 Event Log</h2>
    {sec4}
  </section>

  <section id="recommendations">
    <h2>🎯 Recommendations</h2>
    {sec5}
  </section>
</main>

<footer>
  <p>Coder Harness Telemetry Dashboard &mdash; DSH Platform</p>
</footer>
</body>
</html>"""

    def _css(self) -> str:
        return """<style>
:root {
  --bg-primary: #0d1117;
  --bg-secondary: #161b22;
  --bg-tertiary: #1c2128;
  --bg-card: #1a1f27;
  --border: #30363d;
  --text-primary: #e6edf3;
  --text-secondary: #8b949e;
  --text-muted: #6e7681;
  --accent-blue: #4fc3f7;
  --accent-green: #66bb6a;
  --accent-orange: #ffa726;
  --accent-red: #ef5350;
  --accent-purple: #b39ddb;
  --font-mono: 'JetBrains Mono', 'Fira Code', 'Cascadia Code', 'Consolas', monospace;
  --font-sans: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
}

* { margin: 0; padding: 0; box-sizing: border-box; }

body {
  font-family: var(--font-sans);
  background: var(--bg-primary);
  color: var(--text-primary);
  line-height: 1.6;
  min-height: 100vh;
}

header {
  background: var(--bg-secondary);
  border-bottom: 1px solid var(--border);
  padding: 1.2rem 2rem;
  position: sticky;
  top: 0;
  z-index: 100;
}
.header-inner { max-width: 1200px; margin: 0 auto; }
header h1 {
  font-size: 1.4rem;
  font-weight: 700;
  color: var(--accent-blue);
  font-family: var(--font-mono);
}
.header-meta {
  font-size: 0.8rem;
  color: var(--text-muted);
  margin-top: 0.25rem;
  font-family: var(--font-mono);
}

main {
  max-width: 1200px;
  margin: 0 auto;
  padding: 1.5rem 2rem 3rem;
}

section {
  margin-bottom: 2.5rem;
}
section h2 {
  font-size: 1.15rem;
  font-weight: 600;
  color: var(--accent-blue);
  border-bottom: 1px solid var(--border);
  padding-bottom: 0.5rem;
  margin-bottom: 1rem;
  font-family: var(--font-mono);
}

/* Cards */
.card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 1rem;
  margin-bottom: 1.5rem;
}
.card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 1rem 1.2rem;
}
.card-label {
  font-size: 0.75rem;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  font-family: var(--font-mono);
}
.card-value {
  font-size: 1.5rem;
  font-weight: 700;
  color: var(--text-primary);
  font-family: var(--font-mono);
  margin-top: 0.25rem;
}
.card-value.accent-blue { color: var(--accent-blue); }
.card-value.accent-green { color: var(--accent-green); }
.card-value.accent-orange { color: var(--accent-orange); }
.card-value.accent-red { color: var(--accent-red); }

/* Config cards */
.config-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 1rem 1.2rem;
  margin-bottom: 0.5rem;
}
.config-card h3 {
  font-size: 0.9rem;
  color: var(--accent-purple);
  font-family: var(--font-mono);
  margin-bottom: 0.5rem;
}
.config-card .detail {
  font-size: 0.8rem;
  color: var(--text-secondary);
  font-family: var(--font-mono);
  line-height: 1.8;
}
.config-card .detail span { color: var(--text-muted); }

/* Tables */
.table-wrap {
  overflow-x: auto;
  border: 1px solid var(--border);
  border-radius: 8px;
  margin-bottom: 1rem;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-family: var(--font-mono);
  font-size: 0.8rem;
}
th {
  background: var(--bg-tertiary);
  color: var(--text-secondary);
  text-align: left;
  padding: 0.6rem 0.8rem;
  font-weight: 600;
  text-transform: uppercase;
  font-size: 0.7rem;
  letter-spacing: 0.05em;
  border-bottom: 2px solid var(--border);
  white-space: nowrap;
}
td {
  padding: 0.5rem 0.8rem;
  border-bottom: 1px solid var(--border);
  color: var(--text-primary);
  white-space: nowrap;
}
tr:nth-child(even) { background: var(--bg-tertiary); }
tr:hover { background: rgba(79, 195, 247, 0.05); }
tr td.numeric { text-align: right; font-variant-numeric: tabular-nums; }

/* Badges */
.badge {
  display: inline-block;
  padding: 0.15rem 0.5rem;
  border-radius: 12px;
  font-size: 0.7rem;
  font-family: var(--font-mono);
  font-weight: 500;
}
.badge-running { background: rgba(79, 195, 247, 0.15); color: var(--accent-blue); }
.badge-complete { background: rgba(102, 187, 106, 0.15); color: var(--accent-green); }
.badge-failed { background: rgba(239, 83, 80, 0.15); color: var(--accent-red); }
.badge-pending { background: rgba(255, 167, 38, 0.15); color: var(--accent-orange); }

/* Charts */
.chart-container {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 1rem;
  margin-bottom: 1rem;
  text-align: center;
}

/* Timeline */
.timeline {
  position: relative;
  padding-left: 2rem;
  margin-top: 1rem;
}
.timeline::before {
  content: '';
  position: absolute;
  left: 8px;
  top: 0;
  bottom: 0;
  width: 2px;
  background: var(--border);
}
.timeline-item {
  position: relative;
  margin-bottom: 0.8rem;
  padding: 0.6rem 1rem;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 6px;
}
.timeline-item::before {
  content: '';
  position: absolute;
  left: -1.55rem;
  top: 0.85rem;
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: var(--accent-blue);
}
.timeline-item.complete::before { background: var(--accent-green); }
.timeline-item.failed::before { background: var(--accent-red); }
.timeline-item.running::before { background: var(--accent-orange); }
.timeline-time {
  font-size: 0.7rem;
  color: var(--text-muted);
  font-family: var(--font-mono);
}
.timeline-detail {
  font-size: 0.8rem;
  color: var(--text-primary);
  font-family: var(--font-mono);
}

/* Recommendation cards */
.rec-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-left: 3px solid var(--accent-green);
  border-radius: 8px;
  padding: 1rem 1.2rem;
  margin-bottom: 1rem;
}
.rec-card h3 {
  font-size: 0.9rem;
  color: var(--accent-green);
  font-family: var(--font-mono);
  margin-bottom: 0.4rem;
}
.rec-card .rec-detail {
  font-size: 0.82rem;
  color: var(--text-secondary);
  font-family: var(--font-mono);
  line-height: 1.7;
}
.rec-card .rec-detail strong { color: var(--text-primary); }
.rec-card.no-data {
  border-left-color: var(--text-muted);
}
.rec-card.no-data h3 { color: var(--text-muted); }

.empty {
  color: var(--text-muted);
  font-style: italic;
  font-family: var(--font-mono);
  font-size: 0.85rem;
  padding: 1rem;
}

footer {
  text-align: center;
  padding: 2rem;
  color: var(--text-muted);
  font-size: 0.75rem;
  font-family: var(--font-mono);
  border-top: 1px solid var(--border);
}

@media (max-width: 768px) {
  main { padding: 1rem; }
  .card-grid { grid-template-columns: 1fr; }
  header { padding: 1rem; }
}
</style>"""

    def _render_system_overview(self, sys: dict) -> str:
        """Section 1: System Overview."""
        parts = []

        # GPU summary cards
        cfg_3090 = sys.get("gpu_3090", [])
        cfg_3070 = sys.get("gpu_3070", [])

        parts.append('<div class="card-grid">')
        parts.append(f'''
          <div class="card">
            <div class="card-label">3090 Configs</div>
            <div class="card-value accent-blue">{len(cfg_3090)}</div>
          </div>
          <div class="card">
            <div class="card-label">3070 Configs</div>
            <div class="card-value accent-green">{len(cfg_3070)}</div>
          </div>
          <div class="card">
            <div class="card-label">Total VRAM</div>
            <div class="card-value">32 GB</div>
          </div>
          <div class="card">
            <div class="card-label">Last Swap</div>
            <div class="card-value" style="font-size:1rem">{self._ts(sys.get("last_swap"))}</div>
          </div>
        ''')
        parts.append('</div>')

        # 3090 configs
        parts.append('<h3 style="color:#b39ddb;font-size:0.9rem;margin:1rem 0 0.5rem;font-family:var(--font-mono)">RTX 3090 (24 GB) — Port 8080</h3>')
        for cfg in cfg_3090:
            parts.append(f'''<div class="config-card">
              <h3>{self._esc(cfg.get('id', '?'))}</h3>
              <div class="detail">
                <span>Model:</span> {self._esc(cfg.get('model_name', '?'))}<br>
                <span>Quant:</span> {self._esc(cfg.get('quantization', '—'))} &nbsp;
                <span>Context:</span> {self._esc(cfg.get('context_size', '?'))} &nbsp;
                <span>KVarN:</span> {self._esc(cfg.get('kvarn_level', '—'))} &nbsp;
                <span>Speculative:</span> {self._esc(cfg.get('speculative_type', 'none'))} &nbsp;
                <span>Port:</span> {self._esc(cfg.get('port', '?'))}
              </div>
            </div>''')

        # 3070 configs
        parts.append('<h3 style="color:#b39ddb;font-size:0.9rem;margin:1rem 0 0.5rem;font-family:var(--font-mono)">RTX 3070 (8 GB) — Port 8082</h3>')
        for cfg in cfg_3070:
            parts.append(f'''<div class="config-card">
              <h3>{self._esc(cfg.get('id', '?'))}</h3>
              <div class="detail">
                <span>Model:</span> {self._esc(cfg.get('model_name', '?'))}<br>
                <span>Quant:</span> {self._esc(cfg.get('quantization', '—'))} &nbsp;
                <span>Context:</span> {self._esc(cfg.get('context_size', '?'))} &nbsp;
                <span>KVarN:</span> {self._esc(cfg.get('kvarn_level', '—'))} &nbsp;
                <span>Speculative:</span> {self._esc(cfg.get('speculative_type', 'none'))} &nbsp;
                <span>Port:</span> {self._esc(cfg.get('port', '?'))}
              </div>
            </div>''')

        # GPU Memory bar chart
        fit_3090 = sys.get("gpu_fit_3090", [])
        fit_3070 = sys.get("gpu_fit_3070", [])
        all_fit = fit_3090 + fit_3070
        if all_fit:
            parts.append('<div class="chart-container">')
            parts.append(self._vram_bar_chart(all_fit))
            parts.append('</div>')
        else:
            parts.append('<p class="empty">GPU fit data not yet collected — run <code>python3 gpu_fit.py</code> to populate VRAM usage charts</p>')

        return "\n".join(parts)

    def _render_benchmarks(self, summ: dict, gpu_fit: list) -> str:
        """Section 2: Benchmark Results."""
        parts = []

        # Summary cards
        parts.append('<div class="card-grid">')
        parts.append(f'''
          <div class="card">
            <div class="card-label">Total Runs</div>
            <div class="card-value accent-blue">{summ['total_runs']}</div>
          </div>
          <div class="card">
            <div class="card-label">Complete</div>
            <div class="card-value accent-green">{summ['complete_runs']}</div>
          </div>
          <div class="card">
            <div class="card-label">Failed</div>
            <div class="card-value accent-red">{summ['failed_runs']}</div>
          </div>
        ''')
        parts.append('</div>')

        # Config summary table
        if summ["per_config"]:
            parts.append('<div class="table-wrap"><table>')
            parts.append('''<thead><tr>
              <th>Config</th><th>GPU</th><th>Model</th><th>Quant</th>
              <th>Context</th><th>KVarN</th><th>Runs</th>
              <th>Avg tok/s</th><th>Max tok/s</th><th>Avg Tokens</th>
            </tr></thead><tbody>''')
            for cfg in summ["per_config"]:
                parts.append(f'''<tr>
                  <td>{self._esc(cfg['config_id'])}</td>
                  <td>{self._esc(cfg['gpu'])}</td>
                  <td>{self._esc(cfg['model_name'])}</td>
                  <td>{self._esc(cfg['quantization'])}</td>
                  <td class="numeric">{self._esc(cfg['context_size'])}</td>
                  <td>{self._esc(cfg['kvarn_level'])}</td>
                  <td class="numeric">{self._num(cfg['total_runs'], '.0f')}</td>
                  <td class="numeric">{self._num(cfg['avg_tps'])}</td>
                  <td class="numeric">{self._num(cfg['max_tps'])}</td>
                  <td class="numeric">{self._num(cfg['avg_tokens'], '.0f')}</td>
                </tr>''')
            parts.append('</tbody></table></div>')

            # Speed chart
            parts.append('<div class="chart-container">')
            parts.append(self._speed_chart_svg(summ))
            parts.append('</div>')
        else:
            parts.append('<p class="empty">No benchmark runs yet — run <code>python3 orchestrate.py --phase 1</code> to start benchmarking</p>')

        # GPU Fit Matrix table
        if gpu_fit:
            parts.append('<h3 style="color:#b39ddb;font-size:0.9rem;margin:1rem 0 0.5rem;font-family:var(--font-mono)">GPU Fit Matrix</h3>')
            parts.append('<div class="table-wrap"><table>')
            parts.append('''<thead><tr>
              <th>Config</th><th>GPU</th><th>Model</th><th>Context</th>
              <th>Fits</th><th>VRAM Used</th><th>VRAM Total</th>
              <th>Inference OK</th><th>Inference tok/s</th><th>Error</th>
            </tr></thead><tbody>''')
            for row in gpu_fit:
                fits_badge = "✅" if row.get("fits") else "❌"
                inf_badge = "✅" if row.get("inference_ok") else ("❌" if row.get("inference_ok") is not None else "—")
                parts.append(f'''<tr>
                  <td>{self._esc(row['config_id'])}</td>
                  <td>{self._esc(row['gpu'])}</td>
                  <td>{self._esc(row['model_name'])}</td>
                  <td class="numeric">{self._num(row['context_size'], '.0f')}</td>
                  <td>{fits_badge}</td>
                  <td class="numeric">{self._num(row.get('vram_used'), '.0f')} MB</td>
                  <td class="numeric">{self._num(row.get('vram_total'), '.0f')} MB</td>
                  <td>{inf_badge}</td>
                  <td class="numeric">{self._num(row.get('inference_tokens_per_sec'))}</td>
                  <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis">{self._esc(row.get('error_message'))}</td>
                </tr>''')
            parts.append('</tbody></table></div>')

        return "\n".join(parts)

    def _render_work_sessions(self, sessions: list) -> str:
        """Section 3: Work Sessions."""
        parts = []

        if not sessions:
            parts.append('<p class="empty">No work sessions recorded yet — sessions will appear here when the coder harness executes work</p>')
            return "\n".join(parts)

        # Summary cards
        running = sum(1 for s in sessions if (s.get("status") or "").lower() == "running")
        completed = sum(1 for s in sessions if (s.get("status") or "").lower() in ("complete", "completed"))
        failed = sum(1 for s in sessions if (s.get("status") or "").lower() in ("failed", "error"))
        parts.append('<div class="card-grid">')
        parts.append(f'''
          <div class="card">
            <div class="card-label">Total Sessions</div>
            <div class="card-value accent-blue">{len(sessions)}</div>
          </div>
          <div class="card">
            <div class="card-label">Running</div>
            <div class="card-value accent-orange">{running}</div>
          </div>
          <div class="card">
            <div class="card-label">Completed</div>
            <div class="card-value accent-green">{completed}</div>
          </div>
          <div class="card">
            <div class="card-label">Failed</div>
            <div class="card-value accent-red">{failed}</div>
          </div>
        ''')
        parts.append('</div>')

        # Sessions table
        parts.append('<div class="table-wrap"><table>')
        parts.append('''<thead><tr>
          <th>ID</th><th>Sandbox</th><th>Project</th><th>Branch</th>
          <th>Config</th><th>Status</th><th>Tokens</th>
          <th>Speed</th><th>Duration</th><th>Commits</th>
        </tr></thead><tbody>''')
        for s in sessions[:50]:
            # Calculate duration
            dur = "—"
            if s.get("completed_at") and s.get("created_at"):
                try:
                    start = datetime.fromisoformat(s["created_at"])
                    end = datetime.fromisoformat(s["completed_at"])
                    delta = end - start
                    mins = int(delta.total_seconds() / 60)
                    secs = int(delta.total_seconds() % 60)
                    dur = f"{mins}m {secs}s"
                except (ValueError, TypeError):
                    dur = "—"
            elif s.get("inference_seconds"):
                secs = s["inference_seconds"]
                dur = f"{secs:.1f}s"

            parts.append(f'''<tr>
              <td>{self._esc(s.get('id', '?'))}</td>
              <td>{self._esc(s.get('sandbox_name'))}</td>
              <td>{self._esc(s.get('project'))}</td>
              <td>{self._esc(s.get('branch'))}</td>
              <td>{self._esc(s.get('config_id'))}</td>
              <td>{self._badge(s.get('status'))}</td>
              <td class="numeric">{self._num(s.get('inference_tokens'), '.0f')}</td>
              <td class="numeric">{self._num(s.get('inference_tokens_per_sec'))} tok/s</td>
              <td class="numeric">{dur}</td>
              <td class="numeric">{self._num(s.get('git_commits'), '.0f')}</td>
            </tr>''')
        parts.append('</tbody></table></div>')

        # Timeline view
        recent = sessions[:10]
        if recent:
            parts.append('<h3 style="color:#b39ddb;font-size:0.9rem;margin:1rem 0 0.5rem;font-family:var(--font-mono)">Recent Timeline</h3>')
            parts.append('<div class="timeline">')
            for s in reversed(recent):
                status_l = (s.get("status") or "").lower()
                cls = "complete" if status_l in ("complete", "completed") else (
                    "failed" if status_l in ("failed", "error") else (
                    "running" if status_l == "running" else ""))
                parts.append(f'''<div class="timeline-item {cls}">
                  <div class="timeline-time">{self._ts(s.get('created_at'))}</div>
                  <div class="timeline-detail">
                    {self._badge(s.get('status'))} &nbsp;
                    {self._esc(s.get('project', '?'))} / {self._esc(s.get('sandbox_name', '?'))}
                    &mdash; {self._esc(s.get('config_id', '?'))}
                    &nbsp; {self._num(s.get('inference_tokens_per_sec'))} tok/s
                  </div>
                </div>''')
            parts.append('</div>')

        return "\n".join(parts)

    def _render_event_log(self, events: list) -> str:
        """Section 4: Event Log."""
        parts = []

        if not events:
            parts.append('<p class="empty">No events recorded yet — events will appear here as work progresses</p>')
            return "\n".join(parts)

        parts.append('<div class="table-wrap"><table>')
        parts.append('''<thead><tr>
          <th>Timestamp</th><th>Event</th><th>Session</th>
          <th>Sandbox</th><th>Project</th><th>Details</th>
        </tr></thead><tbody>''')
        for e in events:
            event_data = e.get("event_data") or ""
            # Truncate long event data
            if len(event_data) > 120:
                event_data = event_data[:117] + "..."
            parts.append(f'''<tr>
              <td>{self._ts(e.get('timestamp'))}</td>
              <td><span class="badge badge-running">{self._esc(e.get('event_type'))}</span></td>
              <td>{self._esc(e.get('session_id'))}</td>
              <td>{self._esc(e.get('sandbox_name'))}</td>
              <td>{self._esc(e.get('project'))}</td>
              <td style="max-width:300px;overflow:hidden;text-overflow:ellipsis" title="{html.escape(e.get('event_data') or '')}">{html.escape(event_data)}</td>
            </tr>''')
        parts.append('</tbody></table></div>')

        return "\n".join(parts)

    def _render_recommendations(self, recs: dict) -> str:
        """Section 5: Recommendations."""
        parts = []

        # Best for orchestrator
        orch = recs.get("best_orchestrator")
        if orch:
            parts.append(f'''<div class="rec-card">
              <h3>🎛️ Best Config for Orchestrator</h3>
              <div class="rec-detail">
                <strong>{self._esc(orch.get('model_name', '?'))}</strong> on {self._esc(orch.get('gpu', '?'))}<br>
                Config: <strong>{self._esc(orch.get('model_config_id', '?'))}</strong><br>
                Avg Score: <strong>{self._num(orch.get('avg_score'))}</strong>/10 &nbsp;|&nbsp;
                Avg Speed: <strong>{self._num(orch.get('avg_tps'))}</strong> tok/s &nbsp;|&nbsp;
                Runs: <strong>{self._num(orch.get('runs'), '.0f')}</strong>
              </div>
            </div>''')
        else:
            parts.append('''<div class="rec-card no-data">
              <h3>🎛️ Best Config for Orchestrator</h3>
              <div class="rec-detail">Insufficient benchmark data — need ≥3 complete runs per config with judge scores</div>
            </div>''')

        # Best for coder
        coder = recs.get("best_coder")
        if coder:
            parts.append(f'''<div class="rec-card">
              <h3>💻 Best Config for Coder</h3>
              <div class="rec-detail">
                <strong>{self._esc(coder.get('model_name', '?'))}</strong> on {self._esc(coder.get('gpu', '?'))}<br>
                Config: <strong>{self._esc(coder.get('model_config_id', '?'))}</strong><br>
                Avg Score: <strong>{self._num(coder.get('avg_score'))}</strong>/10 &nbsp;|&nbsp;
                Avg Speed: <strong>{self._num(coder.get('avg_tps'))}</strong> tok/s &nbsp;|&nbsp;
                Runs: <strong>{self._num(coder.get('runs'), '.0f')}</strong>
              </div>
            </div>''')
        else:
            parts.append('''<div class="rec-card no-data">
              <h3>💻 Best Config for Coder</h3>
              <div class="rec-detail">Insufficient benchmark data — need ≥3 complete coder runs with judge scores</div>
            </div>''')

        # Best overall speed
        speed = recs.get("best_overall_speed")
        if speed:
            parts.append(f'''<div class="rec-card">
              <h3>⚡ Fastest Overall</h3>
              <div class="rec-detail">
                <strong>{self._esc(speed.get('model_name', '?'))}</strong> on {self._esc(speed.get('gpu', '?'))}<br>
                Config: <strong>{self._esc(speed.get('model_config_id', '?'))}</strong><br>
                Avg Speed: <strong>{self._num(speed.get('avg_tps'))}</strong> tok/s &nbsp;|&nbsp;
                Runs: <strong>{self._num(speed.get('runs'), '.0f')}</strong>
              </div>
            </div>''')
        else:
            parts.append('''<div class="rec-card no-data">
              <h3>⚡ Fastest Overall</h3>
              <div class="rec-detail">No throughput data available yet</div>
            </div>''')

        # Active sandboxes
        parts.append(f'''<div class="rec-card" style="border-left-color: var(--accent-orange);">
          <h3>📦 Active Sandboxes</h3>
          <div class="rec-detail">
            <strong>{recs.get('active_sandboxes', 0)}</strong> sandbox(es) currently active
          </div>
        </div>''')

        # Evidence table
        evidence = recs.get("evidence", [])
        if evidence:
            parts.append('<h3 style="color:#b39ddb;font-size:0.9rem;margin:1rem 0 0.5rem;font-family:var(--font-mono)">Evidence — Top 10 Fastest Runs</h3>')
            parts.append('<div class="table-wrap"><table>')
            parts.append('''<thead><tr>
              <th>Config</th><th>GPU</th><th>Role</th><th>Difficulty</th>
              <th>Speed (tok/s)</th><th>Tokens</th><th>Score</th><th>Started</th>
            </tr></thead><tbody>''')
            for e in evidence:
                score = e.get("score")
                score_str = f"{score:.1f}/10" if score is not None else "—"
                parts.append(f'''<tr>
                  <td>{self._esc(e.get('model_config_id'))}</td>
                  <td>{self._esc(e.get('gpu'))}</td>
                  <td>{self._esc(e.get('role'))}</td>
                  <td>{self._esc(e.get('difficulty'))}</td>
                  <td class="numeric">{self._num(e.get('predicted_per_second'))}</td>
                  <td class="numeric">{self._num(e.get('total_tokens'), '.0f')}</td>
                  <td class="numeric">{score_str}</td>
                  <td>{self._ts(e.get('started_at'))}</td>
                </tr>''')
            parts.append('</tbody></table></div>')

        return "\n".join(parts)


# ─── CLI Entry Point ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Generate Coder Harness Telemetry Dashboard"
    )
    parser.add_argument(
        "--output", "-o",
        default=DEFAULT_OUTPUT,
        help=f"Output HTML path (default: {DEFAULT_OUTPUT})"
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open the dashboard in a browser after generating"
    )
    args = parser.parse_args()

    dashboard = TelemetryDashboard()
    output = dashboard.generate(args.output)

    if args.open:
        import webbrowser
        webbrowser.open(f"file://{os.path.abspath(output)}")
        print(f"[dashboard] Opened in browser")

    print(f"[dashboard] Done: {output}")


if __name__ == "__main__":
    main()
