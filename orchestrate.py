#!/usr/bin/env python3
"""Main orchestrator — sequences the full benchmark pipeline."""

import argparse
import json
import os
import signal
import sys
import threading
import time
from datetime import datetime

# Add parent dir to path for imports
sys.path.insert(0, os.path.dirname(__file__))

from ssh_utils import SSHClient
from runner import BenchmarkRunner
from gpu_fit import GPUFitProbe
from preflight import preflight_check
from watchdog import GPUWatchdog
from checkpoint import CheckpointManager

MANIFEST_PATH = os.path.join(os.path.dirname(__file__), 'models', 'manifest.json')
DB_PATH = os.path.join(os.path.dirname(__file__), 'benchmark-results.db')
LOG_DIR = os.path.join(os.path.dirname(__file__), 'logs')


class BenchmarkOrchestrator:
    """Main orchestrator that sequences the full benchmark pipeline."""

    def __init__(self, db_path=None, resume=False):
        self.db_path = db_path or DB_PATH
        self.resume = resume
        self.ssh = SSHClient()
        self.runner = None
        self.watchdog = None
        self.checkpoint = None
        self._shutdown_requested = False

        # Load manifest
        with open(MANIFEST_PATH) as f:
            self.configs = {c['id']: c for c in json.load(f)['configs']}

    def _setup(self):
        """Initialize all subsystems."""
        print("Connecting to Triton...")
        self.ssh.connect()

        self.runner = BenchmarkRunner(db_path=self.db_path, ssh_client=self.ssh)
        self.watchdog = GPUWatchdog(ssh_client=self.ssh)
        self.checkpoint = CheckpointManager(self.db_path)

        # Set up signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        # Create log directory
        os.makedirs(LOG_DIR, exist_ok=True)

    def _signal_handler(self, signum, frame):
        """Handle SIGINT/SIGTERM — save checkpoint and exit cleanly."""
        print(f"\nReceived signal {signum}, shutting down gracefully...")
        self._shutdown_requested = True
        if self.watchdog:
            self.watchdog.stop()
        if self.checkpoint:
            self.checkpoint.save_checkpoint()
        print("Checkpoint saved. Exiting.")
        sys.exit(0)

    def _run_preflight(self):
        """Run pre-flight health checks."""
        print("\n" + "=" * 60)
        print("PRE-FLIGHT CHECKS")
        print("=" * 60)
        results = preflight_check()
        all_pass = True
        for name, result in results.items():
            status = '✅' if result['status'] == 'pass' else '❌'
            print(f"  {status} {name}: {result.get('detail', '')}")
            if result['status'] != 'pass':
                all_pass = False

        if not all_pass:
            print("\n⚠️  Some pre-flight checks failed. Proceed with caution.")
        return all_pass

    def phase0_gpu_fit(self, dry_run=False, sizes=None):
        """Phase 0: Run GPU fit probe for all model/context combinations."""
        print("\n" + "=" * 60)
        print("PHASE 0: GPU FIT MATRIX")
        print("=" * 60)

        probe = GPUFitProbe(db_path=self.db_path, ssh_client=self.ssh)
        context_sizes = sizes or [4096, 8192, 32768, 65536, 131072]

        results = probe.probe_all(context_sizes=context_sizes, dry_run=dry_run)
        probe.print_summary(results)

        return results

    def phase1_isolated_eval(self, config_ids=None, dry_run=False):
        """Phase 1: Run all configs × all tasks with scoring."""
        print("\n" + "=" * 60)
        print("PHASE 1: ISOLATED MODEL EVAL")
        print("=" * 60)

        # Resume from checkpoint if needed
        if self.resume:
            resume_data = self.checkpoint.resume_from_checkpoint()
            if resume_data['should_resume']:
                print(f"Resuming from checkpoint: {len(resume_data['completed_run_ids'])} runs already complete")

        # Start GPU watchdog
        self.watchdog.start()
        print("GPU watchdog started (polling every 30s)")

        try:
            # Run benchmark matrix
            configs_to_test = config_ids or sorted(self.configs.keys())
            print(f"\nTesting {len(configs_to_test)} configs × all tasks")

            for config_id in configs_to_test:
                if self._shutdown_requested:
                    break

                config = self.configs[config_id]
                print(f"\n{'─' * 40}")
                print(f"Config: {config_id} ({config['model_name']} on GPU {config['gpu']})")
                print(f"{'─' * 40}")

                # Check if we should skip this config
                if self.resume:
                    completed = self.runner.get_completed_runs(model_config_id=config_id)
                    if len(completed) >= 23:  # All tasks done
                        print(f"  Skipping — all tasks already completed")
                        continue

                # Run batch
                results = self.runner.run_batch(config_id, dry_run=dry_run)

                # Check for abort
                if self.watchdog.is_aborted():
                    print("\n⚠️  GPU watchdog triggered abort!")
                    self.checkpoint.save_checkpoint()
                    break

                # Score results (if not dry run)
                if not dry_run and results:
                    print(f"\n  Scoring {len([r for r in results if r])} runs...")
                    try:
                        from judge import JudgeScorer
                        judge = JudgeScorer(db_path=self.db_path, ssh_client=self.ssh)
                        scored = judge.score_batch(
                            run_ids=[r for r in results if r],
                            dry_run=False
                        )
                        print(f"  Scored {len(scored)} runs")
                    except Exception as e:
                        print(f"  Scoring failed: {e}")

                # Checkpoint after each config
                if not dry_run:
                    self.checkpoint.save_checkpoint()

        finally:
            self.watchdog.stop()
            print("GPU watchdog stopped")

    def single_config_run(self, config_id, dry_run=False):
        """Run one config against all tasks. Produces complete profile."""
        print(f"\nSingle config run: {config_id}")

        self._setup()
        self._run_preflight()

        if not dry_run:
            self.watchdog.start()

        try:
            results = self.runner.run_batch(config_id, dry_run=dry_run)

            if not dry_run and results:
                try:
                    from judge import JudgeScorer
                    judge = JudgeScorer(db_path=self.db_path, ssh_client=self.ssh)
                    judge.score_batch(run_ids=[r for r in results if r])
                except ImportError:
                    print("  Judge module not available, skipping scoring")
                except Exception as e:
                    print(f"  Scoring failed: {e}")

            self.runner.print_summary()
        finally:
            if not dry_run:
                self.watchdog.stop()

    def run(self, phase=1, config_ids=None, dry_run=False):
        """Main entry point."""
        self._setup()

        # Always run preflight
        self._run_preflight()

        if phase == 0:
            self.phase0_gpu_fit(dry_run=dry_run)
        elif phase == 1:
            self.phase1_isolated_eval(config_ids=config_ids, dry_run=dry_run)
            self.runner.print_summary()

    def cleanup(self):
        """Cleanup resources."""
        if self.watchdog:
            self.watchdog.stop()
        if self.ssh:
            self.ssh.close()


def main():
    parser = argparse.ArgumentParser(
        description='Coder Harness Benchmark Orchestrator',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 orchestrate.py --phase 0 --dry-run          # GPU fit matrix (dry run)
  python3 orchestrate.py --phase 1 --dry-run           # Full benchmark (dry run)
  python3 orchestrate.py --config 3090-qwen35-9b       # Run single config
  python3 orchestrate.py --phase 1 --resume             # Resume interrupted run
        """
    )
    parser.add_argument('--phase', type=int, choices=[0, 1], default=1,
                        help='Phase to run (0=GPU fit, 1=full eval)')
    parser.add_argument('--config', type=str,
                        help='Single config ID to test (skips other configs)')
    parser.add_argument('--resume', action='store_true',
                        help='Resume from last checkpoint')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print execution plan without running')
    parser.add_argument('--db', type=str, default=DB_PATH,
                        help=f'Database path (default: {DB_PATH})')
    parser.add_argument('--sizes', type=str,
                        help='Context sizes for GPU fit (comma-separated)')

    args = parser.parse_args()

    sizes = None
    if args.sizes:
        sizes = [int(s) for s in args.sizes.split(',')]

    orchestrator = BenchmarkOrchestrator(
        db_path=args.db,
        resume=args.resume
    )

    try:
        if args.config:
            orchestrator.single_config_run(args.config, dry_run=args.dry_run)
        else:
            orchestrator.run(
                phase=args.phase,
                dry_run=args.dry_run
            )
    except KeyboardInterrupt:
        print("\nInterrupted by user")
    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
    finally:
        orchestrator.cleanup()


if __name__ == "__main__":
    main()
