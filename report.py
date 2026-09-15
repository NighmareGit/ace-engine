#!/usr/bin/env python3
"""Results reporter — generates comparison tables, scatter plots, and recommendations.

Usage:
    python3 report.py                          # default: benchmark-results.db, markdown
    python3 report.py --db mydb.db --format json
    python3 report.py --format md              # also generates PNG scatter plots
"""

import argparse
import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "benchmark-results.db")
REPORT_DIR = os.path.join(SCRIPT_DIR, "reports")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _median(values: List[float]) -> Optional[float]:
    """Return median of a list of floats, or None if empty."""
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


def _iqr(values: List[float]) -> Tuple[Optional[float], Optional[float]]:
    """Return (Q1, Q3) or (None, None) if empty."""
    if not values:
        return None, None
    s = sorted(values)
    n = len(s)
    q1_idx = n // 4
    q3_idx = (3 * n) // 4
    return s[q1_idx], s[min(q3_idx, n - 1)]


def _fmt(v: Optional[float], decimals: int = 1) -> str:
    if v is None:
        return "—"
    return f"{v:.{decimals}f}"


def _config_label(row: dict) -> str:
    """Build a human-friendly label from a model_configs row."""
    parts = [row.get("id", "?")]
    model = row.get("model_name", "")
    if model:
        parts.append(model)
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Reporter
# ---------------------------------------------------------------------------

class BenchmarkReporter:
    """Generates comparison reports from benchmark results."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or DB_PATH
        os.makedirs(REPORT_DIR, exist_ok=True)

    # -- DB helpers --------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _config_map(self, conn: sqlite3.Connection) -> Dict[str, dict]:
        """Map config id → full row dict."""
        rows = conn.execute("SELECT * FROM model_configs").fetchall()
        return {r["id"]: dict(r) for r in rows}

    def _thinking_map(self, conn: sqlite3.Connection) -> Dict[str, bool]:
        """Map config id → thinking_enabled."""
        cfgs = self._config_map(conn)
        return {cid: bool(c.get("thinking_enabled", 0)) for cid, c in cfgs.items()}

    # -- Data extraction ---------------------------------------------------

    def _scored_runs(self, conn: sqlite3.Connection) -> List[dict]:
        """Return all complete runs with their judge scores, joined."""
        rows = conn.execute("""
            SELECT
                br.id                AS run_id,
                br.model_config_id   AS config_id,
                br.task_id           AS task_id,
                br.predicted_per_second AS tps,
                br.predicted_ms      AS gen_ms,
                br.prompt_ms         AS prompt_ms,
                br.thinking_tokens   AS thinking_tokens,
                br.total_tokens      AS total_tokens,
                js.completeness      AS completeness,
                js.correctness       AS correctness,
                js.quality           AS quality,
                js.intelligence      AS intelligence,
                js.role_fit          AS role_fit,
                js.overall           AS overall,
                js.judge_model       AS judge_model,
                t.role               AS role,
                t.category           AS category,
                t.difficulty         AS difficulty
            FROM benchmark_runs br
            JOIN judge_scores js ON js.run_id = br.id
            JOIN tasks t ON t.id = br.task_id
            WHERE br.status = 'complete'
        """).fetchall()
        return [dict(r) for r in rows]

    # -- Per-role aggregation ----------------------------------------------

    @staticmethod
    def _aggregate_by_config(runs: List[dict]) -> Dict[str, dict]:
        """Group runs by config_id, compute median + IQR for each numeric column."""
        grouped: Dict[str, List[dict]] = defaultdict(list)
        for r in runs:
            grouped[r["config_id"]].append(r)

        dims = ["completeness", "correctness", "quality", "intelligence", "role_fit", "overall", "tps", "gen_ms"]
        result: Dict[str, dict] = {}
        for cid, items in sorted(grouped.items()):
            entry: dict = {"config_id": cid, "n": len(items)}
            for dim in dims:
                vals = [it[dim] for it in items if it.get(dim) is not None]
                med = _median(vals)
                q1, q3 = _iqr(vals)
                entry[f"{dim}_med"] = med
                entry[f"{dim}_q1"] = q1
                entry[f"{dim}_q3"] = q3
            result[cid] = entry
        return result

    # -- Speed summary -----------------------------------------------------

    def _speed_summary(self, conn: sqlite3.Connection) -> List[dict]:
        rows = conn.execute("""
            SELECT model_config_id,
                   AVG(predicted_per_second)    AS avg_tps,
                   MIN(predicted_per_second)    AS min_tps,
                   MAX(predicted_per_second)    AS max_tps,
                   AVG(predicted_ms)            AS avg_gen_ms,
                   AVG(prompt_ms)               AS avg_prompt_ms,
                   AVG(thinking_tokens)          AS avg_think_tok,
                   COUNT(*)                     AS n
            FROM benchmark_runs
            WHERE status = 'complete'
            GROUP BY model_config_id
            ORDER BY avg_tps DESC
        """).fetchall()
        return [dict(r) for r in rows]

    # -- Pairwise comparison -----------------------------------------------

    def _pairwise_matrix(self, runs: List[dict]) -> Dict[str, Dict[str, dict]]:
        """For each (config_a, config_b), count tasks where a beats b on overall."""
        # Group by (config_id, task_id) → list of overall scores
        task_scores: Dict[Tuple[str, str], List[float]] = defaultdict(list)
        for r in runs:
            task_scores[(r["config_id"], r["task_id"])].append(r["overall"])

        # Average per (config, task)
        avg: Dict[Tuple[str, str], float] = {}
        for (cid, tid), vals in task_scores.items():
            avg[(cid, tid)] = sum(vals) / len(vals)

        configs = sorted({cid for cid, _ in avg.keys()})
        matrix: Dict[str, Dict[str, dict]] = {}
        for a in configs:
            matrix[a] = {}
            for b in configs:
                if a == b:
                    matrix[a][b] = {"wins": 0, "losses": 0, "ties": 0}
                    continue
                wins, losses, ties = 0, 0, 0
                shared_tasks = {t for (c, t) in avg if c == a} & {t for (c, t) in avg if c == b}
                for tid in shared_tasks:
                    sa = avg[(a, tid)]
                    sb = avg[(b, tid)]
                    if sa > sb + 0.1:
                        wins += 1
                    elif sb > sa + 0.1:
                        losses += 1
                    else:
                        ties += 1
                matrix[a][b] = {"wins": wins, "losses": losses, "ties": ties}
        return matrix

    # -- Recommendation ----------------------------------------------------

    def _recommendation(self, conn: sqlite3.Connection, runs: List[dict]) -> dict:
        """Best config per role, with evidence."""
        cfg_map = self._config_map(conn)

        by_role_config: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
        by_role_config_tps: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
        for r in runs:
            by_role_config[r["role"]][r["config_id"]].append(r["overall"])
            if r["tps"] is not None:
                by_role_config_tps[r["role"]][r["config_id"]].append(r["tps"])

        recs: dict = {}
        for role in ("orchestrator", "coder"):
            scores = by_role_config.get(role, {})
            if not scores:
                recs[role] = None
                continue
            # Median overall per config
            medians = {cid: _median(vals) for cid, vals in scores.items()}
            best_cid = max(medians, key=lambda c: medians[c] or 0)
            n_tasks = len({r["task_id"] for r in runs if r["role"] == role and r["config_id"] == best_cid})
            n_runs = len(scores[best_cid])
            tps_vals = by_role_config_tps.get(role, {}).get(best_cid, [])
            tps_med = _median(tps_vals)

            # Runner-up for comparison
            sorted_configs = sorted(medians.items(), key=lambda x: x[1] or 0, reverse=True)
            runner_up = sorted_configs[1] if len(sorted_configs) > 1 else None

            recs[role] = {
                "config_id": best_cid,
                "config": dict(cfg_map.get(best_cid, {})),
                "median_overall": medians[best_cid],
                "n_runs": n_runs,
                "n_tasks": n_tasks,
                "median_tps": tps_med,
                "runner_up": {
                    "config_id": runner_up[0],
                    "median_overall": runner_up[1],
                } if runner_up else None,
            }
        return recs

    # -- Scatter plots (matplotlib) ----------------------------------------

    def _generate_scatter_plots(self, runs: List[dict], thinking_map: Dict[str, bool]) -> List[str]:
        """Generate speed-vs-quality scatter PNGs. Returns list of file paths."""
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import matplotlib.patches as mpatches
        except ImportError:
            print("⚠ matplotlib not installed — skipping scatter plots")
            return []

        if not runs:
            return []

        generated: List[str] = []

        # --- 1. Overall vs TPS, one dot per (config, task) ---
        fig, ax = plt.subplots(figsize=(12, 7))

        configs_seen = sorted(set(r["config_id"] for r in runs))
        import matplotlib.colors as mcolors
        cmap = mcolors.ListedColormap(plt.cm.tab10.colors[:len(configs_seen)])

        for idx, cid in enumerate(configs_seen):
            subset = [r for r in runs if r["config_id"] == cid]
            # Aggregate per task
            task_agg: Dict[str, List[float]] = defaultdict(list)
            tps_agg: Dict[str, List[float]] = defaultdict(list)
            for r in subset:
                task_agg[r["task_id"]].append(r["overall"])
                if r["tps"] is not None:
                    tps_agg[r["task_id"]].append(r["tps"])

            task_ids = sorted(set(task_agg.keys()))
            x_vals = [_median(tps_agg.get(tid, [])) or 0 for tid in task_ids]
            y_vals = [_median(task_agg[tid]) for tid in task_ids]
            thinking = thinking_map.get(cid, False)
            marker = "^" if thinking else "o"
            label = f"{cid} ({'thinking' if thinking else 'no-thinking'})"
            ax.scatter(x_vals, y_vals, color=cmap(idx), marker=marker, s=80,
                       alpha=0.8, edgecolors="black", linewidth=0.5, label=label)

        ax.set_xlabel("Tokens / Second", fontsize=12)
        ax.set_ylabel("Overall Quality Score", fontsize=12)
        ax.set_title("Speed vs Quality — All Tasks (marker: ▲ thinking ON, ● thinking OFF)", fontsize=13)
        ax.set_ylim(-0.2, 10.5)
        ax.legend(fontsize=9, loc="lower right")
        ax.grid(True, alpha=0.3)
        path1 = os.path.join(REPORT_DIR, "scatter-speed-vs-quality-all.png")
        fig.tight_layout()
        fig.savefig(path1, dpi=150)
        plt.close(fig)
        generated.append(path1)

        # --- 2. Orchestrator tasks only ---
        for role_label, role_filter in [("Orchestrator", "orchestrator"), ("Coder", "coder")]:
            role_runs = [r for r in runs if r["role"] == role_filter]
            if not role_runs:
                continue

            fig, ax = plt.subplots(figsize=(12, 7))
            for idx, cid in enumerate(configs_seen):
                subset = [r for r in role_runs if r["config_id"] == cid]
                if not subset:
                    continue
                task_agg: Dict[str, List[float]] = defaultdict(list)
                tps_agg: Dict[str, List[float]] = defaultdict(list)
                for r in subset:
                    task_agg[r["task_id"]].append(r["overall"])
                    if r["tps"] is not None:
                        tps_agg[r["task_id"]].append(r["tps"])

                task_ids = sorted(set(task_agg.keys()))
                x_vals = [_median(tps_agg.get(tid, [])) or 0 for tid in task_ids]
                y_vals = [_median(task_agg[tid]) for tid in task_ids]
                thinking = thinking_map.get(cid, False)
                marker = "^" if thinking else "o"
                label = f"{cid} ({'thinking' if thinking else 'no-thinking'})"
                ax.scatter(x_vals, y_vals, color=cmap(idx), marker=marker, s=80,
                           alpha=0.8, edgecolors="black", linewidth=0.5, label=label)

            ax.set_xlabel("Tokens / Second", fontsize=12)
            ax.set_ylabel("Overall Quality Score", fontsize=12)
            ax.set_title(f"Speed vs Quality — {role_label} Tasks", fontsize=13)
            ax.set_ylim(-0.2, 10.5)
            ax.legend(fontsize=9, loc="lower right")
            ax.grid(True, alpha=0.3)
            fname = f"scatter-speed-vs-quality-{role_filter}.png"
            path = os.path.join(REPORT_DIR, fname)
            fig.tight_layout()
            fig.savefig(path, dpi=150)
            plt.close(fig)
            generated.append(path)

        # --- 3. Individual score dimensions vs TPS ---
        for dim in ("completeness", "correctness", "quality", "intelligence", "role_fit"):
            fig, ax = plt.subplots(figsize=(12, 7))
            for idx, cid in enumerate(configs_seen):
                subset = [r for r in runs if r["config_id"] == cid and r.get(dim) is not None]
                if not subset:
                    continue
                x_vals = [r["tps"] or 0 for r in subset if r["tps"] is not None]
                y_vals = [r[dim] for r in subset if r["tps"] is not None]
                thinking = thinking_map.get(cid, False)
                marker = "^" if thinking else "o"
                label = f"{cid} ({'thinking' if thinking else 'no-thinking'})"
                ax.scatter(x_vals, y_vals, color=cmap(idx), marker=marker, s=80,
                           alpha=0.8, edgecolors="black", linewidth=0.5, label=label)

            ax.set_xlabel("Tokens / Second", fontsize=12)
            ax.set_ylabel(dim.replace("_", " ").title(), fontsize=12)
            ax.set_title(f"Speed vs {dim.replace('_', ' ').title()}", fontsize=13)
            ax.set_ylim(-0.2, 10.5)
            ax.legend(fontsize=9, loc="lower right")
            ax.grid(True, alpha=0.3)
            fname = f"scatter-speed-vs-{dim}.png"
            path = os.path.join(REPORT_DIR, fname)
            fig.tight_layout()
            fig.savefig(path, dpi=150)
            plt.close(fig)
            generated.append(path)

        return generated

    # -- Markdown output ---------------------------------------------------

    @staticmethod
    def _md_table(headers: List[str], rows: List[List[str]], align: Optional[List[str]] = None) -> str:
        """Build a markdown table string."""
        if not rows:
            return "_No data_\n"
        lines = []
        lines.append("| " + " | ".join(headers) + " |")
        al = align or ["---"] * len(headers)
        lines.append("| " + " | ".join(al) + " |")
        for row in rows:
            lines.append("| " + " | ".join(str(c) for c in row) + " |")
        return "\n".join(lines) + "\n"

    def _role_table_md(self, label: str, agg: Dict[str, dict], cfg_map: Dict[str, dict]) -> str:
        """Markdown table for a single role."""
        headers = ["Config", "Model", "GPU", "Thinking", "N", "Overall (med)", "Quality (med)",
                    "Intelligence (med)", "Role-Fit (med)", "Speed tok/s (med)", "IQR Overall"]
        rows = []
        for cid, entry in sorted(agg.items(), key=lambda x: x[1].get("overall_med") or 0, reverse=True):
            cfg = cfg_map.get(cid, {})
            thinking = "✓" if cfg.get("thinking_enabled") else "✗"
            q1 = entry.get("overall_q1")
            q3 = entry.get("overall_q3")
            iqr_str = f"{_fmt(q1)}–{_fmt(q3)}" if q1 is not None else "—"
            rows.append([
                cid,
                cfg.get("model_name", "?"),
                cfg.get("gpu", "?"),
                thinking,
                entry["n"],
                _fmt(entry.get("overall_med")),
                _fmt(entry.get("quality_med")),
                _fmt(entry.get("intelligence_med")),
                _fmt(entry.get("role_fit_med")),
                _fmt(entry.get("tps_med")),
                iqr_str,
            ])
        return f"### {label}\n\n" + self._md_table(headers, rows)

    def _pairwise_md(self, matrix: Dict[str, Dict[str, dict]]) -> str:
        """Markdown pairwise comparison matrix."""
        configs = sorted(matrix.keys())
        if not configs:
            return "_No data_\n"
        headers = ["Config"] + configs
        rows = []
        for a in configs:
            row = [a]
            for b in configs:
                if a == b:
                    row.append("—")
                else:
                    w = matrix[a][b]["wins"]
                    l = matrix[a][b]["losses"]
                    t = matrix[a][b]["ties"]
                    row.append(f"{w}W / {l}L / {t}T")
            rows.append(row)
        return self._md_table(headers, rows, ["---"] + [":---:"] * len(configs))

    def _write_markdown(self, all_runs: List[dict], agg_orch: dict, agg_coder: dict,
                        speed: List[dict], matrix: dict, recs: dict,
                        cfg_map: dict, scatter_paths: List[str]) -> str:
        path = os.path.join(REPORT_DIR, "benchmark-report.md")
        lines: List[str] = []

        lines.append("# Coder Harness Benchmark Report\n")
        lines.append(f"_Generated from `{os.path.basename(self.db_path)}`_\n")
        lines.append(f"_Completed runs: {len(all_runs)}_\n\n")

        # --- Orchestrator ---
        lines.append(self._role_table_md("Orchestrator Tasks", agg_orch, cfg_map))
        lines.append("")

        # --- Coder ---
        lines.append(self._role_table_md("Coder Tasks", agg_coder, cfg_map))
        lines.append("")

        # --- Speed ---
        lines.append("## Speed Summary\n")
        spd_headers = ["Config", "Avg tok/s", "Min", "Max", "Avg Gen (ms)", "Avg Prompt (ms)", "Avg Think Tokens", "N"]
        spd_rows = []
        for r in speed:
            spd_rows.append([
                r["model_config_id"],
                _fmt(r.get("avg_tps")),
                _fmt(r.get("min_tps")),
                _fmt(r.get("max_tps")),
                _fmt(r.get("avg_gen_ms"), 0),
                _fmt(r.get("avg_prompt_ms"), 0),
                _fmt(r.get("avg_think_tok"), 0),
                r["n"],
            ])
        lines.append(self._md_table(spd_headers, spd_rows))
        lines.append("")

        # --- Pairwise ---
        lines.append("## Pairwise Comparison (W = wins, L = losses, T = ties)\n")
        lines.append("A **win** means config A scored higher than B on the same task (Δ > 0.1).\n")
        lines.append(self._pairwise_md(matrix))
        lines.append("")

        # --- Scatter plots ---
        if scatter_paths:
            lines.append("## Scatter Plots\n")
            for p in scatter_paths:
                name = os.path.basename(p)
                lines.append(f"![{name}]({name})\n")

        # --- Recommendations ---
        lines.append("## Final Recommendations\n")
        for role in ("orchestrator", "coder"):
            r = recs.get(role)
            title = role.replace("_", " ").title()
            if r is None:
                lines.append(f"### Best Config for {title}: _No data_\n")
                continue
            cfg = r.get("config", {})
            lines.append(f"### Best Config for {title}: `{r['config_id']}`\n")
            lines.append(f"- **Model**: {cfg.get('model_name', '?')}")
            lines.append(f"- **GPU**: {cfg.get('gpu', '?')}")
            lines.append(f"- **Thinking**: {'ON' if cfg.get('thinking_enabled') else 'OFF'}")
            lines.append(f"- **Median overall score**: {_fmt(r['median_overall'])}/10")
            lines.append(f"- **Median speed**: {_fmt(r['median_tps'])} tok/s")
            lines.append(f"- **Runs evaluated**: {r['n_runs']} across {r['n_tasks']} tasks")
            if r.get("runner_up"):
                ru = r["runner_up"]
                lines.append(f"- **Runner-up**: {ru['config_id']} (median {_fmt(ru['median_overall'])}/10)")
            lines.append("")
            # Evidence
            lines.append(f"**Evidence**: On {role} tasks, `{r['config_id']}` achieved the highest "
                         f"median overall quality score of {_fmt(r['median_overall'])}/10 "
                         f"across {r['n_tasks']} distinct tasks ({r['n_runs']} total runs), "
                         f"with a median throughput of {_fmt(r['median_tps'])} tok/s.")
            lines.append("")

        content = "\n".join(lines)
        with open(path, "w") as f:
            f.write(content)
        return path

    def _write_json(self, all_runs: List[dict], agg_orch: dict, agg_coder: dict,
                    speed: List[dict], matrix: dict, recs: dict,
                    scatter_paths: List[str]) -> str:
        path = os.path.join(REPORT_DIR, "benchmark-report.json")
        report = {
            "total_runs": len(all_runs),
            "orchestrator_scores": agg_orch,
            "coder_scores": agg_coder,
            "speed_summary": speed,
            "pairwise_matrix": matrix,
            "recommendations": recs,
            "scatter_plots": [os.path.basename(p) for p in scatter_paths],
        }
        with open(path, "w") as f:
            json.dump(report, f, indent=2, default=str)
        return path

    # -- Public API --------------------------------------------------------

    def generate_full_report(self, fmt: str = "md") -> Optional[str]:
        """Generate the complete comparison report. Returns output file path."""
        conn = self._conn()

        # Check for data
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM benchmark_runs WHERE status = 'complete'"
        ).fetchone()["c"]
        if total == 0:
            print("No completed benchmark runs yet. Run the benchmark first.")
            conn.close()
            return None

        all_runs = self._scored_runs(conn)
        cfg_map = self._config_map(conn)
        thinking_map = self._thinking_map(conn)

        agg_orch = self._aggregate_by_config([r for r in all_runs if r["role"] == "orchestrator"])
        agg_coder = self._aggregate_by_config([r for r in all_runs if r["role"] == "coder"])
        speed = self._speed_summary(conn)
        matrix = self._pairwise_matrix(all_runs)
        recs = self._recommendation(conn, all_runs)

        # Generate scatter plots
        scatter_paths = self._generate_scatter_plots(all_runs, thinking_map)
        if scatter_paths:
            print(f"Generated {len(scatter_paths)} scatter plot(s) in {REPORT_DIR}/")

        if fmt == "md":
            out_path = self._write_markdown(all_runs, agg_orch, agg_coder, speed,
                                            matrix, recs, cfg_map, scatter_paths)
        elif fmt == "json":
            out_path = self._write_json(all_runs, agg_orch, agg_coder, speed,
                                        matrix, recs, scatter_paths)
        else:
            print(f"Unknown format: {fmt}", file=sys.stderr)
            conn.close()
            return None

        print(f"Report written to: {out_path}")
        conn.close()
        return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Coder Harness Benchmark Reporter")
    parser.add_argument("--db", type=str, default=DB_PATH, help="Path to SQLite database")
    parser.add_argument("--format", choices=["md", "json"], default="md", help="Output format")
    args = parser.parse_args()

    reporter = BenchmarkReporter(db_path=args.db)
    reporter.generate_full_report(fmt=args.format)
