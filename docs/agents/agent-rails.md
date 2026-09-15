# Agent Execution Rails

Worktree rule: use `scripts/agent-worktree.sh <branch>` to create an isolated
`../ace-engine-<suffix>` worktree + branch. Work ONLY there. Main checkout is
bookkeeping. Push branch when done.

Preflight rule: NEVER run the engine without preflight PASS. Run
`deploy/host/preflight.py --workspace <path>` first. War story: an agent
pointed the engine at localhost:8080 serving an unrelated local model — got 200s
with wrong answers for 30 minutes before anyone noticed. Preflight kills this.

Endpoint constants:
  Subject: <LAN_IP>:8082 (Qwen3.5-9B)  TRITON_SUBJECT_URL
  Judge:   <LAN_IP>:8080 (Qwen3.6-35B) TRITON_JUDGE_URL
  Gitea:   http://<LAN_IP>:3000       TRITON_GITEA_URL

Invalid-run protocol: kill the process, annotate the log (don't delete it),
never silently discard. A failed run's log is forensic evidence.

Commit discipline: one logical change per commit. Suite green before commit.
`python3 -m pytest tests/engine tests/unit -q` must pass.

Logs: deploy/host/logs/<run-label>_<timestamp>.log.
Run reports: <workspace>/run/<run_id>/.
