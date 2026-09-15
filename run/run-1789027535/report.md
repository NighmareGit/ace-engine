# Run Report: run-1789027535

- **State**: DONE
- **Success**: PASS
- **PRD**: `/home/<user>/projects/ace-engine-research-ws1/.run-prds/PRD-G1-T02.md`
- **Project**: `/home/<user>/projects/ace-engine-research-ws1`
- **Wall clock**: 120.83s
- **Total tokens**: 29362

## Budget

- `llm_calls`: 2
- `max_llm_calls`: 40
- `total_tokens`: 11176
- `max_total_tokens`: 2000000
- `wall_clock_s`: 120.83
- `max_wall_clock_s`: 2400

## Checkpoint

- **Last pushed SHA**: `c48c07ec45b9be544df08fadd4028aa922720989`

## Task Outcomes

| Task | State | Attempts | Commit | Tokens | Error |
|------|-------|----------|--------|--------|-------|
| G1 | COMMIT | 2 | `c48c07ec45b9be544df08fadd4028aa922720989` | 29362 |  |

## Re-plan History

(no re-plans attempted)

## Acceptance Criteria Checklist

| Task | Criterion | Met |
|------|-----------|-----|
| G1 | Parses Aider-style SEARCH/REPLACE fences into EditOp dataclass | PASS |
| G1 | Falls back to <edit><old>/<new> XML tags | PASS |
| G1 | Malformed blocks skipped with warning, never crash | PASS |
| G1 | Session log row written per LLM call | PASS |
| G1 | New code lives in engine/edit_ops/extract.py (not inline in generator.py) | PASS |
