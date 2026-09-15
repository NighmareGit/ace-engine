#!/usr/bin/env python3
"""Resume PRD-#8 (RLM Engine v1) from T02 with escalation JSONL projection."""

import os, sys, json, time
sys.path.insert(0, '/home/<user>/projects/ace-engine')
os.environ["GITEA_TOKEN"] = "<GITEA_TOKEN>"
os.environ["GITEA_URL"] = "http://localhost:3000"

from engine.engine import Engine, EngineConfig
config = EngineConfig(
    model_config="3070-qwen35-9b",  # 9B subject on :8082
    judge_mode="deterministic",
    judge_port=8080,
    subject_port=8082,
    judge_temperature=0.0,
    max_retries_generate=3,
    max_retries_test=2,
    timeout_inference=300,
    timeout_test=180,
    timeout_commit=60,
    dry_run=False,
    sandbox=False,
    judge=True,
    max_llm_calls=100,
    max_total_tokens=5_000_000,
    max_wall_clock_s=7200,
)

print("=" * 60)
print("PRD-#8 Resume: rlm_engine v1 — T02 → T08")
print(f"Config: subject={config.subject_port}, judge={config.judge_mode} port:{config.judge_port}")
print("=" * 60)

prd_path = "/home/<user>/projects/ace-engine/.scratch/research/PRD-rlm-engine-v1-PREPARED.md"

engine = Engine(config=config)
result = engine.run(prd_path, "/home/<user>/rlm-engine")

summary = {"per_task": [{"task_id": t.task_id, "state": t.state} for t in result.tasks]}
with open("/tmp/run-summary.json", "w") as f:
    json.dump(summary, f)
