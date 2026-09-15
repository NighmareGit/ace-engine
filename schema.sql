-- Coder Harness Benchmark Platform — SQLite Schema
-- Enable foreign key enforcement before any DDL
PRAGMA foreign_keys = ON;

-- =============================================================================
-- Model configurations: every testable model/GPU/quant combo
-- =============================================================================
CREATE TABLE IF NOT EXISTS model_configs (
  id            TEXT PRIMARY KEY,                     -- e.g. '3090-qwen36-35b'
  gpu           TEXT    NOT NULL,                     -- e.g. '3090', '3070'
  model_name    TEXT    NOT NULL,                     -- e.g. 'Qwen3.6-35B-A3B IQ4_XS'
  quantization  TEXT,                                 -- e.g. 'IQ4_XS'
  context_size  INTEGER DEFAULT 4096,
  thinking_enabled INTEGER DEFAULT 0,                 -- 0 = off, 1 = on
  kvarn_level   TEXT,                                 -- e.g. 'kvarn5', 'f16', NULL
  speculative_type TEXT,                              -- e.g. 'draft-dflash', 'draft-mtp', 'none'
  draft_model   TEXT,                                 -- e.g. 'Qwen3.5-9B-DFlash.gguf', NULL
  port          INTEGER NOT NULL,
  model_path    TEXT    NOT NULL,                     -- full path on Triton
  notes         TEXT
);

-- =============================================================================
-- Benchmark task definitions
-- =============================================================================
CREATE TABLE IF NOT EXISTS tasks (
  id                TEXT PRIMARY KEY,                 -- e.g. 'T01' through 'T23'
  role              TEXT    NOT NULL CHECK(role IN ('orchestrator','coder','multi-turn')),
  category          TEXT    NOT NULL,
  difficulty        TEXT CHECK(difficulty IN ('easy','medium','hard')),
  prompt            TEXT    NOT NULL,
  expected_behavior TEXT,
  scoring_criteria  TEXT,
  automated_check   TEXT,                             -- optional shell command for coder tasks
  test_cases        TEXT,                             -- JSON array of test case descriptions
  turns             INTEGER DEFAULT 1                 -- for multi-turn tasks
);

-- =============================================================================
-- Individual inference results — one row per (model, task, turn, repetition)
-- =============================================================================
CREATE TABLE IF NOT EXISTS benchmark_runs (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  model_config_id      TEXT NOT NULL REFERENCES model_configs(id),
  task_id              TEXT NOT NULL REFERENCES tasks(id),
  turn_index           INTEGER DEFAULT 0,
  repetition           INTEGER DEFAULT 1,
  started_at           TEXT NOT NULL DEFAULT (datetime('now')),
  completed_at         TEXT,
  status               TEXT CHECK(status IN ('running','complete','failed','incomplete')) DEFAULT 'running',
  response_text        TEXT,
  predicted_per_second REAL,
  prompt_per_second    REAL,
  predicted_ms         REAL,
  prompt_ms            REAL,
  predicted_n          INTEGER,
  thinking_tokens      INTEGER DEFAULT 0,
  total_tokens         INTEGER,
  error_message        TEXT,
  gpu_temperature      REAL,
  beellama_commit      TEXT,
  model_sha256         TEXT,
  sampling_params      TEXT,
  session_id           TEXT,
  UNIQUE(model_config_id, task_id, turn_index, repetition)
);

-- =============================================================================
-- Judge / quality scoring results
-- =============================================================================
CREATE TABLE IF NOT EXISTS judge_scores (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id              INTEGER NOT NULL REFERENCES benchmark_runs(id),
  judge_model         TEXT    NOT NULL,
  completeness        INTEGER CHECK(completeness BETWEEN 0 AND 10),
  correctness         INTEGER CHECK(correctness BETWEEN 0 AND 10),
  quality             INTEGER CHECK(quality BETWEEN 0 AND 10),
  intelligence        INTEGER CHECK(intelligence BETWEEN 0 AND 10),
  role_fit            INTEGER CHECK(role_fit BETWEEN 0 AND 10),
  overall             REAL GENERATED ALWAYS AS (
    (completeness + correctness + quality + intelligence + role_fit) / 5.0
  ) STORED,
  judge_reasoning     TEXT,
  is_cross_validated  INTEGER DEFAULT 0,
  scored_at           TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(run_id, judge_model)
);

-- =============================================================================
-- Multi-bracket latency measurements
-- =============================================================================
CREATE TABLE IF NOT EXISTS latency_profiles (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  model_config_id      TEXT    NOT NULL REFERENCES model_configs(id),
  target_tokens        INTEGER NOT NULL,
  actual_tokens        INTEGER,
  ttft_ms              REAL,
  generation_ms        REAL,
  total_ms             REAL,
  predicted_per_second REAL,
  measured_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- =============================================================================
-- Model/context GPU fit data
-- =============================================================================
CREATE TABLE IF NOT EXISTS gpu_fit_matrix (
  id                      INTEGER PRIMARY KEY AUTOINCREMENT,
  model_config_id         TEXT    NOT NULL REFERENCES model_configs(id),
  gpu                     TEXT    NOT NULL,
  context_size            INTEGER NOT NULL,
  fits                    INTEGER NOT NULL,          -- 1 = fits, 0 = OOM
  vram_used_mb            REAL,
  vram_total_mb           REAL,
  inference_ok            INTEGER,
  inference_tokens_per_sec REAL,
  error_message           TEXT,
  measured_at             TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(model_config_id, gpu, context_size)
);

-- =============================================================================
-- Schema version tracking
-- =============================================================================
CREATE TABLE IF NOT EXISTS schema_version (
  version    INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Seed initial version
INSERT INTO schema_version (version) VALUES (1);

-- =============================================================================
-- Indexes for common query patterns
-- =============================================================================
CREATE INDEX IF NOT EXISTS idx_runs_model_task   ON benchmark_runs(model_config_id, task_id);
CREATE INDEX IF NOT EXISTS idx_runs_status       ON benchmark_runs(status);
CREATE INDEX IF NOT EXISTS idx_scores_run        ON judge_scores(run_id);
