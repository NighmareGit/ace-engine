# PRD: E2E Validation and Testing Strategy

## Problem Statement

The coder-harness codebase has 274 tests that validate infrastructure (transport, streaming, prompts, integration) but zero tests that validate output quality. The engine has never generated, validated, tested, or committed real code. The `demo_e2e.py` module was verified only in dry-run mode. The `ENGINE-AUDIT.md` identified this as the single most critical weakness: "No code generation validation" (CRITICAL) and "Never tested E2E" (CRITICAL).

The core problem: we have built 48,455 lines of infrastructure and never tested whether it produces working code. The 274 tests prove the plumbing works, not the product.

## Solution

Build a comprehensive testing strategy with three layers: unit tests for each engine module (with mock transport), integration tests for the full pipeline (with mock BeeLlama), and one critical E2E test that runs a real PRD through real BeeLlama inference, validates the output, runs pytest, and commits to Gitea. The E2E test is the single most important test in the entire codebase.

## User Stories

1. As a developer, I want unit tests for each `engine/` module so that individual components are verified in isolation
2. As a developer, I want a `MockTransport` class that simulates BeeLlama responses so that unit tests don't require a running server
3. As a developer, I want the `MockTransport` to return pre-canned code responses for specific prompts so that tests are deterministic
4. As a developer, I want integration tests that exercise the full pipeline with mock transport so that state machine transitions are verified
5. As a developer, I want an E2E test that runs `test-prd.md` through real BeeLlama inference so that we prove the engine actually works
6. As a developer, I want the E2E test to verify that generated code passes AST validation so that we catch syntax errors
7. As a developer, I want the E2E test to verify that generated code can be imported (no missing dependencies) so that we catch import errors
8. As a developer, I want the E2E test to run pytest against the generated code so that we catch runtime errors
9. As a developer, I want the E2E test to commit the generated code to a test branch on Gitea so that we verify the full PRD → commit path
10. As a developer, I want the E2E test to produce a structured report with timing, token usage, and quality scores so that we can track improvements over time
11. As a developer, I want the E2E test to be runnable with `python3 -m pytest tests/e2e/test_prd_to_commit.py -v` so that it integrates with the standard test runner
12. As a developer, I want the E2E test to be marked `@pytest.mark.e2e` so that it can be excluded from fast test runs
13. As a developer, I want the E2E test to clean up after itself (delete test branch, remove generated files) so that it doesn't leave artifacts
14. As a developer, I want the E2E test to timeout after 10 minutes so that hung inference doesn't block CI
15. As a developer, I want a `--run-e2e` flag on the CLI so that I can trigger the E2E test from the command line
16. As a developer, I want the validator to have 4 stages (AST, syntax, imports, execution) so that we catch progressively more issues
17. As a developer, I want the validator to report which stage failed so that the generator gets targeted feedback for retry
18. As a developer, I want the E2E test to verify that the pipeline state machine transitions through all expected states so that we catch missing transitions
19. As a developer, I want the E2E test to verify that events are emitted to the streaming server so that observability works
20. As a developer, I want the E2E test to verify that the RunResult contains all expected fields so that the output contract is enforced
21. As a developer, I want the E2E test to be repeatable (same PRD, same config, same result) so that regressions are detectable
22. As a developer, I want the E2E test to log all BeeLlama requests and responses so that we can debug failures
23. As a developer, I want the E2E test to measure first-pass success rate (code that passes validation on first try) so that we can track generator quality
24. As a developer, I want the E2E test to measure test pass rate (code that passes pytest on first try) so that we can track end-to-end quality
25. As a developer, I want the E2E test to produce a machine-readable JSON report so that we can build dashboards from it

## Implementation Decisions

### Decision 1: Test Pyramid

```
                    ┌──────────┐
                    │ E2E (1)  │  Real BeeLlama, real Gitea, real pytest
                    │ ~10 min  │  THE critical test
                    ├──────────┤
                    │Integration│  Mock transport, real pipeline state machine
                    │   (3-5)  │  Verify transitions, retry, checkpoint
                    ├──────────┤
                    │  Unit    │  Mock everything, test each module
                    │  (30+)   │  Fast, deterministic, comprehensive
                    └──────────┘
```

### Decision 2: MockTransport

A `MockTransport` class that implements the same interface as `LocalTransport` but returns canned responses:

```python
class MockTransport:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []  # recorded for assertions

    def curl_beellama(self, port, messages, **kwargs):
        self.calls.append({"port": port, "messages": messages})
        prompt = messages[-1]["content"] if messages else ""
        # Match prompt to canned response
        for pattern, response in self.responses.items():
            if pattern in prompt:
                return response
        return {"content": "# Default mock response\npass", "predicted_per_second": 100.0}

    def run_command(self, cmd, timeout=30, cwd=None):
        self.calls.append({"cmd": cmd})
        if "pytest" in cmd:
            return '{"passed": 1, "failed": 0, "errors": 0}', "", 0
        return "", "", 0

    def write_file(self, path, content):
        self.calls.append({"path": path, "content_len": len(content)})
        return True

    def check_beellama_health(self, port):
        return True
```

### Decision 3: E2E Test Flow

```python
@pytest.mark.e2e
def test_prd_to_commit():
    """THE critical test: PRD → generate → validate → test → commit."""

    # 1. Setup
    engine = Engine(
        transport=get_transport(),  # real transport
        streaming_url="http://127.0.0.1:3081",  # optional
        config=EngineConfig(
            model_config="3090-qwen36-35b",
            max_retries_generate=3,
            max_retries_test=2,
            timeout_inference=300,
            timeout_test=180,
        )
    )

    # 2. Run
    result = engine.run(
        prd_path="test-prd.md",
        project_path="/home/<user>/projects/ace-demo",
        dry_run=False
    )

    # 3. Verify result structure
    assert result.success is not None
    assert result.tasks is not None
    assert len(result.tasks) > 0
    assert result.total_time_s > 0

    # 4. Verify each task
    for task_result in result.tasks:
        assert task_result.task_id is not None
        assert task_result.state in ("DONE", "FAILED")
        if task_result.state == "DONE":
            assert task_result.commit_sha is not None
            assert task_result.generated_code is not None

    # 5. Verify pipeline states were visited
    assert State.PARSING in result.states_visited
    assert State.GENERATE in result.states_visited
    assert State.VALIDATE in result.states_visited

    # 6. Verify Gitea commit exists
    if result.commit_sha:
        # Check Gitea for the commit
        pass

    # 7. Cleanup (delete test branch)
```

### Decision 4: Validator Stages

| Stage | Input | Output | What It Catches |
|-------|-------|--------|-----------------|
| AST Parse | Code string | AST tree | Syntax errors, incomplete code |
| Syntax Check | AST tree | Compiled bytecode | Indentation, naming, structure |
| Import Resolution | Code + project | Import status | Missing modules, circular imports |
| Execution Test | Code + namespace | Pass/fail | Runtime errors, type errors |

Each stage returns a `ValidationStageResult`:
```python
@dataclass
class ValidationStageResult:
    stage: str  # "ast", "syntax", "imports", "execution"
    passed: bool
    error: str | None
    details: dict
```

### Decision 5: Test Report Schema

```json
{
  "run_id": "ace-20260904-123456",
  "prd_path": "test-prd.md",
  "project_path": "/home/<user>/projects/ace-demo",
  "config": "3090-qwen36-35b",
  "total_time_s": 142.5,
  "tasks_total": 3,
  "tasks_succeeded": 2,
  "tasks_failed": 1,
  "first_pass_rate": 0.67,
  "test_pass_rate": 0.50,
  "total_tokens": 15420,
  "total_thinking_tokens": 3200,
  "states_visited": ["PARSING", "QUEUED", "CONTEXT", "GENERATE", "VALIDATE", "TEST", "COMMIT", "NEXT", "DONE"],
  "tasks": [
    {
      "task_id": "T01",
      "title": "Create health check endpoint",
      "state": "DONE",
      "attempts": 1,
      "validation_stages_passed": 4,
      "tests_passed": 1,
      "tests_failed": 0,
      "commit_sha": "abc123",
      "time_s": 45.2,
      "tokens": 5140
    }
  ]
}
```

### Decision 6: Test Markers

```python
# pytest.ini or pyproject.toml
[tool.pytest.ini_options]
markers = [
    "e2e: end-to-end tests requiring BeeLlama (deselect with '-m not e2e')",
    "slow: tests taking > 30 seconds",
    "integration: tests with mock transport",
]
```

Run fast tests: `python3 -m pytest tests/ -m "not e2e and not slow"`
Run all tests: `python3 -m pytest tests/`
Run E2E only: `python3 -m pytest tests/e2e/ -v`

### Decision 7: E2E Cleanup

The E2E test creates a Git branch `ace-e2e-test-{timestamp}` and commits generated code to it. After the test completes (pass or fail), it:
1. Checks out the original branch
2. Deletes the test branch (local and remote)
3. Removes any generated files not in the original project

This ensures the test is idempotent and leaves no artifacts.

## Testing Decisions

- **Unit test count target**: 30+ tests across 9 engine modules
- **Integration test count target**: 3-5 tests covering the full pipeline
- **E2E test count**: 1 test (the critical one)
- **Mock pattern**: `MockTransport` with canned responses, verified via `transport.calls`
- **Assertion pattern**: Structural (fields exist, types correct) + behavioral (states visited, retries used)
- **Prior art**: `test_streaming.py` (1,027 lines) — comprehensive mocking pattern for this codebase
- **CI integration**: Fast tests run on every commit. E2E runs weekly or on demand.

## Out of Scope

1. Performance benchmarks (measuring tok/s of generated code)
2. Security scanning of generated code
3. Multi-language test support (Python only in V1)
4. Visual regression testing
5. Load testing the engine
6. Testing the legacy modules (they're frozen)

## Further Notes

### Why One E2E Test

The E2E test is intentionally a single, comprehensive test rather than multiple small E2E tests. Reasoning:
1. The E2E test requires a running BeeLlama instance — expensive to set up
2. The test exercises the full pipeline — splitting it would test partial paths
3. A single test with detailed assertions is easier to debug than multiple partial tests
4. The test produces a machine-readable report that serves as a regression baseline

### The 30% Rule

If only 30% of the testing strategy survives, keep:
1. The E2E test (`test_prd_to_commit.py`) — proves the engine works
2. The `MockTransport` — enables all other tests
3. The pipeline state machine tests — verify transitions and checkpointing

Everything else is nice-to-have.

### Glossary

| Term | Definition |
|------|------------|
| **E2E** | End-to-end test with real BeeLlama inference |
| **MockTransport** | Test double simulating the transport layer |
| **First-pass rate** | Percentage of tasks that pass validation on first try |
| **Test pass rate** | Percentage of tasks whose generated code passes pytest |
