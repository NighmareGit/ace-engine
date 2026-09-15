#!/usr/bin/env python3
"""Methodology pilot — validates benchmark methodology before full batch.

Runs a 5-task pilot across 3 configs to measure:
  - Within-model variance (reliability)
  - Between-model effect sizes (discrimination)
  - Judge scoring consistency

Outputs a pilot report with go/no-go recommendation.
"""

import json
import math
import os
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

DB_PATH = os.path.join(os.path.dirname(__file__), 'benchmark-results.db')
CORPUS_PATH = os.path.join(os.path.dirname(__file__), 'corpus.json')
PILOT_REPORT_DIR = os.path.join(os.path.dirname(__file__), 'reports')

# ---------------------------------------------------------------------------
# Pilot design: 3 configs × 5 tasks × 3 reps = 45 runs
# ---------------------------------------------------------------------------
# Configs chosen to span the performance spectrum:
PILOT_CONFIGS = [
    '3090-qwen36-35b',    # Best 3090 — fastest MoE, 128K ctx, KVarN5
    '3070-qwen35-9b',     # Best 3070 — budget, 8K ctx, KVarN2
    '3090-qwen35-9b',     # Baseline — same model as 3070 but with full VRAM headroom
]

# Tasks chosen to cover all roles and difficulties:
PILOT_TASKS = [
    ('T01', 'orchestrator', 'easy',   'ticket understanding'),
    ('T05', 'orchestrator', 'hard',   'prioritization'),
    ('T14', 'coder',        'easy',   'LRU cache implementation'),
    ('T18', 'coder',        'hard',   'Trie data structure'),
    ('T22', 'multi-turn',   'medium', 'code review feedback loop'),
]

REPS = 3  # Repetitions per task per config


# ── Statistics helpers ─────────────────────────────────────────────────────

def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _std(xs):
    """Sample standard deviation."""
    if len(xs) < 2:
        return None
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _cohens_d(group_a, group_b):
    """Cohen's d effect size between two groups (pooled std)."""
    if not group_a or not group_b:
        return None
    na, nb = len(group_a), len(group_b)
    if na < 2 or nb < 2:
        return None
    sa, sb = _std(group_a), _std(group_b)
    pooled_std = math.sqrt(
        ((na - 1) * sa ** 2 + (nb - 1) * sb ** 2) / (na + nb - 2)
    )
    if pooled_std == 0:
        return 0.0
    return (_mean(group_a) - _mean(group_b)) / pooled_std


# ── Pilot runner ───────────────────────────────────────────────────────────

class MethodologyPilot:
    """Runs a pilot to validate benchmark methodology before full batch."""

    def __init__(self, db_path=None):
        self.db_path = db_path or DB_PATH

    # ------------------------------------------------------------------
    # Dry-run plan
    # ------------------------------------------------------------------

    def print_plan(self):
        """Print the execution plan without running anything."""
        total = len(PILOT_CONFIGS) * len(PILOT_TASKS) * REPS
        print("=" * 62)
        print("  METHODOLOGY PILOT — EXECUTION PLAN")
        print("=" * 62)
        print(f"\n  Configs ({len(PILOT_CONFIGS)}):")
        for c in PILOT_CONFIGS:
            print(f"    • {c}")
        print(f"\n  Tasks ({len(PILOT_TASKS)}):")
        for tid, role, diff, desc in PILOT_TASKS:
            print(f"    • {tid} [{role}/{diff}] — {desc}")
        print(f"\n  Repetitions per config×task: {REPS}")
        print(f"  Total inference runs: {total}")
        print(f"  Total judge calls: {total} (one per run)")
        print(f"  Estimated time: ~{total * 30 // 60} minutes (at ~30s/run)")
        print()
        print("  Will measure:")
        print("    1. Within-model variance (std-dev across repetitions)")
        print("    2. Between-model effect sizes (Cohen's d)")
        print("    3. Judge scoring consistency")
        print()
        for i, config in enumerate(PILOT_CONFIGS):
            for j, (tid, role, diff, desc) in enumerate(PILOT_TASKS):
                for rep in range(1, REPS + 1):
                    print(f"    [{config}] × {tid} rep={rep}")
        print()

    # ------------------------------------------------------------------
    # Execute pilot
    # ------------------------------------------------------------------

    def run(self, dry_run=False):
        """Run the full pilot sequence."""
        if dry_run:
            self.print_plan()
            return

        from ssh_utils import SSHClient
        from runner import BenchmarkRunner
        from judge import JudgeScorer

        print("=" * 62)
        print("  METHODOLOGY PILOT — RUNNING")
        print("=" * 62)

        total = len(PILOT_CONFIGS) * len(PILOT_TASKS) * REPS
        print(f"  Total runs: {total}")
        print()

        # Connect to Triton
        ssh = SSHClient()
        ssh.connect()

        runner = BenchmarkRunner(db_path=self.db_path, ssh_client=ssh)
        judge = JudgeScorer(db_path=self.db_path, ssh_client=ssh)

        completed = 0
        failed = 0

        # Run benchmark
        for config in PILOT_CONFIGS:
            print(f"\n{'─' * 62}")
            print(f"  Config: {config}")
            print(f"{'─' * 62}")

            for tid, role, diff, desc in PILOT_TASKS:
                for rep in range(1, REPS + 1):
                    run_id = runner.run_single(config, tid, repetition=rep)
                    if run_id:
                        completed += 1
                        print(f"    ✅ {tid} ({diff}) rep={rep} → run {run_id}")
                    else:
                        failed += 1
                        print(f"    ❌ {tid} ({diff}) rep={rep} FAILED")

        print(f"\n  Runs completed: {completed}/{total}")
        if failed:
            print(f"  Runs failed: {failed}")

        # Score results
        print(f"\n{'─' * 62}")
        print("  SCORING RESULTS")
        print(f"{'─' * 62}")
        scored = judge.score_batch()
        print(f"  Scored {len(scored)} runs")

        # Generate pilot report
        self.generate_report()

    # ------------------------------------------------------------------
    # Report generation
    # ------------------------------------------------------------------

    def generate_report(self):
        """Generate the pilot analysis report and print a summary."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row

        task_ids = [t[0] for t in PILOT_TASKS]

        # ── 1. Per-config per-task variance ───────────────────────────
        variance_data = conn.execute(
            """
            SELECT
                br.model_config_id,
                br.task_id,
                AVG(js.overall)   AS mean_score,
                MIN(js.overall)   AS min_score,
                MAX(js.overall)   AS max_score,
                COUNT(*)          AS n,
                AVG(js.completeness)   AS avg_completeness,
                AVG(js.correctness)    AS avg_correctness,
                AVG(js.quality)        AS avg_quality,
                AVG(js.intelligence)   AS avg_intelligence,
                AVG(js.role_fit)       AS avg_role_fit,
                AVG(br.predicted_per_second) AS avg_tps
            FROM benchmark_runs br
            JOIN judge_scores js ON br.id = js.run_id
            WHERE br.status = 'complete'
              AND br.model_config_id IN ({configs})
              AND br.task_id IN ({tasks})
            GROUP BY br.model_config_id, br.task_id
            ORDER BY br.model_config_id, br.task_id
            """.format(
                configs=','.join('?' * len(PILOT_CONFIGS)),
                tasks=','.join('?' * len(task_ids)),
            ),
            PILOT_CONFIGS + task_ids,
        ).fetchall()

        # ── 2. Within-model std-dev (pooled across tasks) ────────────
        within_model_stds = {}
        for config in PILOT_CONFIGS:
            all_scores = []
            for task_row in variance_data:
                if task_row['model_config_id'] == config:
                    # Fetch individual rep scores for this config×task
                    reps = conn.execute(
                        """
                        SELECT js.overall
                        FROM benchmark_runs br
                        JOIN judge_scores js ON br.id = js.run_id
                        WHERE br.model_config_id = ?
                          AND br.task_id = ?
                          AND br.status = 'complete'
                        """,
                        (config, task_row['task_id']),
                    ).fetchall()
                    all_scores.extend([r['overall'] for r in reps])
            within_model_stds[config] = _std(all_scores) if all_scores else None

        # ── 3. Between-model effect sizes (Cohen's d) ────────────────
        # Collect mean score per config (averaged across all tasks)
        config_means = {}
        for config in PILOT_CONFIGS:
            scores = [r['mean_score'] for r in variance_data
                      if r['model_config_id'] == config and r['mean_score'] is not None]
            config_means[config] = _mean(scores) if scores else None

        # Pairwise effect sizes
        effect_sizes = {}
        for i, c1 in enumerate(PILOT_CONFIGS):
            for c2 in PILOT_CONFIGS[i + 1:]:
                # Collect per-task means for each config
                means_a = [r['mean_score'] for r in variance_data
                           if r['model_config_id'] == c1 and r['mean_score'] is not None]
                means_b = [r['mean_score'] for r in variance_data
                           if r['model_config_id'] == c2 and r['mean_score'] is not None]
                effect_sizes[(c1, c2)] = _cohens_d(means_a, means_b)

        # ── 4. Judge consistency (avg std across reps per task×config) ─
        judge_stds = []
        for config in PILOT_CONFIGS:
            for tid in task_ids:
                reps = conn.execute(
                    """
                    SELECT js.overall
                    FROM benchmark_runs br
                    JOIN judge_scores js ON br.id = js.run_id
                    WHERE br.model_config_id = ?
                      AND br.task_id = ?
                      AND br.status = 'complete'
                    """,
                    (config, tid),
                ).fetchall()
                scores = [r['overall'] for r in reps]
                s = _std(scores)
                if s is not None:
                    judge_stds.append(s)

        avg_judge_std = _mean(judge_stds) if judge_stds else None

        # ── 5. Performance data ──────────────────────────────────────
        perf_data = conn.execute(
            """
            SELECT
                br.model_config_id,
                AVG(br.predicted_per_second) AS avg_tps,
                AVG(br.predicted_ms)         AS avg_latency_ms,
                AVG(br.total_tokens)         AS avg_tokens
            FROM benchmark_runs br
            WHERE br.status = 'complete'
              AND br.model_config_id IN ({})
            GROUP BY br.model_config_id
            """.format(','.join('?' * len(PILOT_CONFIGS))),
            PILOT_CONFIGS,
        ).fetchall()

        conn.close()

        # ── Go/no-go verdict ─────────────────────────────────────────
        max_std = max((s for s in within_model_stds.values() if s is not None), default=None)
        min_effect = min((abs(d) for d in effect_sizes.values() if d is not None), default=None)

        variance_ok = max_std is not None and max_std < 2.0
        separation_ok = min_effect is not None and min_effect > 0.3
        judge_ok = avg_judge_std is not None and avg_judge_std < 1.5

        if variance_ok and separation_ok and judge_ok:
            verdict = "✅ PROCEED"
            verdict_detail = (
                "Within-model variance is low, between-model separation is clear, "
                "and judge scoring is consistent. The methodology is sound."
            )
        elif variance_ok and judge_ok:
            verdict = "⚠️  REVISE — weak separation"
            verdict_detail = (
                "Variance is acceptable and judge is consistent, but model separation "
                "is marginal. Consider adding more tasks or using different configs."
            )
        else:
            verdict = "❌ DO NOT PROCEED"
            verdict_detail = (
                "High variance, poor separation, or unreliable judge scoring. "
                "Revise the methodology before the full batch."
            )

        # ── Write markdown report ────────────────────────────────────
        os.makedirs(PILOT_REPORT_DIR, exist_ok=True)
        report_path = os.path.join(PILOT_REPORT_DIR, 'pilot-report.md')

        with open(report_path, 'w') as f:
            f.write("# Methodology Pilot Report\n\n")
            f.write(f"**Generated**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

            # Verdict box
            f.write(f"## Verdict: {verdict}\n\n")
            f.write(f"{verdict_detail}\n\n")

            # Configuration
            f.write("## Configuration\n\n")
            f.write(f"| Parameter | Value |\n")
            f.write(f"|-----------|-------|\n")
            f.write(f"| Configs tested | {len(PILOT_CONFIGS)} |\n")
            f.write(f"| Tasks tested | {len(PILOT_TASKS)} |\n")
            f.write(f"| Repetitions | {REPS} |\n")
            f.write(f"| Total inference runs | {len(PILOT_CONFIGS) * len(PILOT_TASKS) * REPS} |\n\n")

            f.write("### Configs\n\n")
            for c in PILOT_CONFIGS:
                f.write(f"- `{c}`\n")
            f.write("\n")

            f.write("### Tasks\n\n")
            f.write("| ID | Role | Difficulty | Description |\n")
            f.write("|----|------|------------|-------------|\n")
            for tid, role, diff, desc in PILOT_TASKS:
                f.write(f"| {tid} | {role} | {diff} | {desc} |\n")
            f.write("\n")

            # Variance analysis
            f.write("## Variance Analysis\n\n")
            f.write("Within-model score variance across repetitions:\n\n")
            f.write("| Config | Pooled Std Dev | Verdict |\n")
            f.write("|--------|----------------|---------|\n")
            for config in PILOT_CONFIGS:
                sd = within_model_stds.get(config)
                sd_str = f"{sd:.2f}" if sd is not None else "N/A"
                if sd is not None:
                    v = "✅ Low" if sd < 1.5 else ("⚠️  Moderate" if sd < 2.0 else "❌ High")
                else:
                    v = "N/A"
                f.write(f"| {config} | {sd_str} | {v} |\n")
            f.write("\n")

            # Per-task per-config detail
            f.write("### Score Breakdown (per config × task)\n\n")
            f.write("| Config | Task | Mean | Min | Max | Range | N | Avg TPS |\n")
            f.write("|--------|------|------|-----|-----|-------|---|---------|\n")
            for row in variance_data:
                r = dict(row)
                mean_s = f"{r['mean_score']:.1f}" if r['mean_score'] is not None else "N/A"
                min_s = f"{r['min_score']:.1f}" if r['min_score'] is not None else "N/A"
                max_s = f"{r['max_score']:.1f}" if r['max_score'] is not None else "N/A"
                rng = (f"{r['max_score'] - r['min_score']:.1f}"
                       if r['max_score'] is not None and r['min_score'] is not None
                       else "N/A")
                tps = f"{r['avg_tps']:.1f}" if r['avg_tps'] else "N/A"
                f.write(f"| {r['model_config_id']} | {r['task_id']} "
                        f"| {mean_s} | {min_s} | {max_s} | {rng} | {r['n']} | {tps} |\n")
            f.write("\n")

            # Effect sizes
            f.write("## Between-Model Separation\n\n")
            f.write("Cohen's d effect sizes (0.2=small, 0.5=medium, 0.8=large):\n\n")
            f.write("| Comparison | Cohen's d | Interpretation |\n")
            f.write("|------------|-----------|----------------|\n")
            for (c1, c2), d in effect_sizes.items():
                if d is not None:
                    ad = abs(d)
                    interp = ("Large" if ad >= 0.8 else
                               "Medium" if ad >= 0.5 else
                               "Small" if ad >= 0.2 else "Negligible")
                    f.write(f"| {c1} vs {c2} | {d:.3f} | {interp} |\n")
                else:
                    f.write(f"| {c1} vs {c2} | N/A | Insufficient data |\n")
            f.write("\n")

            # Judge reliability
            f.write("## Judge Reliability\n\n")
            f.write(f"- **Avg std across repetitions**: {avg_judge_std:.2f}\n" if avg_judge_std else "- **Avg std**: N/A\n")
            f.write(f"- **Threshold**: < 1.5 for reliable\n")
            if judge_ok:
                f.write(f"- **Verdict**: ✅ Judge scoring is consistent\n\n")
            else:
                f.write(f"- **Verdict**: ❌ Judge scoring is inconsistent\n\n")

            # Performance
            f.write("## Performance Summary\n\n")
            f.write("| Config | Avg TPS | Avg Latency (ms) | Avg Tokens |\n")
            f.write("|--------|---------|-------------------|------------|\n")
            for row in perf_data:
                r = dict(row)
                f.write(f"| {r['model_config_id']} "
                        f"| {r['avg_tps']:.1f} "
                        f"| {r['avg_latency_ms']:.0f} "
                        f"| {r['avg_tokens']:.0f} |\n")
            f.write("\n")

            # Recommendations
            f.write("## Recommendations\n\n")
            if verdict.startswith("✅"):
                f.write("The methodology is sound. Proceed to full batch with confidence.\n\n")
                f.write("**Next steps:**\n")
                f.write("1. Run full benchmark: `python3 orchestrate.py --phase 1`\n")
                f.write("2. Monitor GPU temperatures during batch\n")
                f.write("3. Review results after batch completion\n")
            elif verdict.startswith("⚠️"):
                f.write("The methodology needs minor adjustments.\n\n")
                f.write("**Suggestions:**\n")
                f.write("1. Add 2-3 more hard tasks to increase discrimination\n")
                f.write("2. Consider testing more config variants\n")
                f.write("3. Increase repetitions to 5 for more reliable variance estimates\n")
            else:
                f.write("The methodology needs significant revision.\n\n")
                f.write("**Issues to address:**\n")
                if not variance_ok:
                    f.write("- Within-model variance is too high — investigate judge consistency\n")
                    f.write("- Consider using a stronger judge model or more detailed rubric\n")
                if not separation_ok:
                    f.write("- Models are not well-separated — try more distinct configs\n")
                    f.write("- Current configs may be too similar in capability\n")
                if not judge_ok:
                    f.write("- Judge scoring is unreliable — review rubric anchors\n")
                    f.write("- Consider cross-validation with multiple judge models\n")

        print(f"\n  📄 Pilot report written to: {report_path}")

        # ── Console summary ──────────────────────────────────────────
        print("\n" + "=" * 62)
        print("  PILOT SUMMARY")
        print("=" * 62)
        print(f"\n  Verdict: {verdict}")
        print(f"\n  Within-model variance:")
        for config in PILOT_CONFIGS:
            sd = within_model_stds.get(config)
            sd_str = f"{sd:.2f}" if sd is not None else "N/A"
            print(f"    {config}: σ = {sd_str}")
        print(f"\n  Between-model effect sizes:")
        for (c1, c2), d in effect_sizes.items():
            if d is not None:
                print(f"    {c1} vs {c2}: d = {d:.3f}")
        print(f"\n  Judge avg std: {avg_judge_std:.2f}" if avg_judge_std else "\n  Judge avg std: N/A")
        print()

        return {
            'verdict': verdict,
            'within_model_stds': within_model_stds,
            'effect_sizes': effect_sizes,
            'judge_avg_std': avg_judge_std,
            'variance_ok': variance_ok,
            'separation_ok': separation_ok,
            'judge_ok': judge_ok,
        }


# ── CLI entry point ────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description='Methodology Pilot — validates benchmark before full batch',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 pilot.py --dry-run          # Print plan without executing
  python3 pilot.py                    # Run the full pilot
  python3 pilot.py --report           # Regenerate report from existing data
        """,
    )
    parser.add_argument('--dry-run', action='store_true',
                        help='Print execution plan without running')
    parser.add_argument('--report', action='store_true',
                        help='Regenerate report from existing data (no runs)')
    parser.add_argument('--db', type=str, default=DB_PATH,
                        help=f'Database path (default: {DB_PATH})')
    args = parser.parse_args()

    pilot = MethodologyPilot(db_path=args.db)

    if args.dry_run:
        pilot.print_plan()
    elif args.report:
        pilot.generate_report()
    else:
        pilot.run(dry_run=False)
