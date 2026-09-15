# Autonomous Coding Engine — Honest Audit

> Generated with metacognitive friction applied. No sandwich method. No softening.

## Confidence Level: 45%
The engine has never generated a single line of real code for a real project. Everything is dry-run verified. The gap between "code compiles" and "produces working software" is the size of a canyon.

---

## STRENGTHS (what actually works)

### 1. SSH Transport Layer (Score: 9/10)
- Proven working: 9/9 E2E validation, 201 tok/s live fire test
- Consistent pattern across all modules (`_ssh_run`, `subprocess.run`)
- Handles timeouts, connection failures, key-based auth
- **This is genuinely solid.** It's the foundation everything else builds on.

### 2. Benchmark Platform (Score: 8/10)
- 6 model configs benchmarked with real throughput data
- SQLite schema with 8 tables, proper indexes
- Judge model integration with 5-dimension scoring
- Crash recovery via checkpoints
- **This is the most mature part of the codebase.** It has real data backing it.

### 3. Documentation (Score: 7/10)
- 14-section AGENTS.md with correct schema
- API reference, contributing guide, remote control guide
- Blueprint integration with dsh-hub
- Auto-generated README
- **The docs are good but some claims are unverified** (e.g., "engine works end-to-end" — it doesn't, yet)

### 4. Prompt Engineering (Score: 7/10)
- 43-line system prompt with chain-of-thought
- 7 specialized templates for different task types
- Auto-detection routes tasks to correct template
- Retry prompt includes original task + error analysis
- **The templates are well-designed but never tested against real LLM output quality**

### 5. Test Infrastructure (Score: 6/10)
- 131 tests (69 prompt + 62 integration)
- Schema validation, config mapping, CLI help
- **But: zero tests for actual code generation quality**
- **No tests verify the engine produces CORRECT code**

---

## WEAKNESSES (what's broken or missing)

### 1. CRITICAL: No Code Generation Validation
**Confidence: 95% this is the #1 weakness**

The `code_generator.py` sends a prompt to BeeLlama and writes whatever it gets back. There is:
- No AST validation of generated code
- No import resolution checking
- No type checking (mypy/pyright)
- No runtime testing of generated code
- No comparison against expected behavior

**The engine is a prompt wrapper, not a code generator.** It trusts the LLM output 100%.

**Fix:** Add `ast.parse()` validation after extraction, import checking against project dependencies, and a "dry run" mode that validates code without writing it.

### 2. CRITICAL: No Real End-to-End Test
**Confidence: 90%**

The demo runs in `--dry-run` mode. We've never:
- Parsed a real PRD
- Generated real code
- Written it to Triton
- Run real tests
- Committed real changes

**The entire engine is theoretical.** The live-fire test (RT08) was a single inference call, not a pipeline.

**Fix:** Run `demo_e2e.py` on a simple PRD (e.g., "create a hello world FastAPI app") and verify the output works.

### 3. HIGH: Module Redundancy
**Confidence: 85%**

Three overlapping modules do similar things:
- `work_engine.py` (1440 lines) — full pipeline with Docker sandboxes
- `work_engine_simple.py` (731 lines) — simplified pipeline without sandboxes
- `task_queue.py` (1497 lines) — another pipeline orchestrator
- `demo_e2e.py` (709 lines) — yet another pipeline orchestrator

**Four different ways to do the same thing.** This is architectural debt.

**Fix:** Consolidate into ONE pipeline module. Delete the other three.

### 4. HIGH: No Project Structure Awareness
**Confidence: 80%**

The engine generates code for individual tasks but doesn't understand:
- How files relate to each other (imports, class hierarchies)
- The project's framework (FastAPI vs Flask vs Django)
- Existing patterns (how errors are handled, how routes are structured)
- File naming conventions

**The `context_manager.py` tracks files but doesn't understand relationships.**

**Fix:** Add AST analysis of existing code to extract: imports, class definitions, function signatures, decorators.

### 5. HIGH: No Deployment Pipeline
**Confidence: 75%**

The engine generates code and commits it, but:
- No CI/CD integration (no GitHub Actions, no GitLab CI)
- No Docker build verification
- No deployment to staging
- No rollback on failed deployment

**The engine writes code that may or may not work, and there's no way to find out without manual testing.**

### 6. MEDIUM: Error Recovery is Primitive
**Confidence: 70%**

The retry mechanism sends the error back to the LLM and asks it to fix it. This works for simple errors but fails for:
- Multi-file dependency issues
- Framework-specific bugs
- Performance problems
- Security vulnerabilities

**The LLM is good at fixing syntax errors but bad at fixing architectural issues.**

### 7. MEDIUM: No Multi-Language Support
**Confidence: 65%**

The system prompt says "adapt to Python/TypeScript/JavaScript/Rust" but:
- No TypeScript compilation checking
- No Rust cargo build verification
- No npm/pip dependency resolution
- All test infrastructure is Python-only

**The engine is Python-first with theoretical support for other languages.**

### 8. MEDIUM: Telemetry is Write-Only
**Confidence: 80%**

The telemetry system collects data but:
- No dashboard that actually queries the data in real-time
- No alerts for failures
- No performance trending
- No cost tracking per task

**Data goes in but nothing useful comes out.**

### 9. LOW: No Caching
**Confidence: 60%**

Every inference call goes through SSH → curl → parse. No caching of:
- Inference results (same prompt → same output)
- File reads (same file → same content)
- Health checks (same endpoint → same status)

**This makes the engine slower than necessary for repeated operations.**

---

## BLIND SPOTS (what I'm not seeing)

### 1. The "Autonomous" Claim
The engine is not autonomous. It requires:
- A human to write the PRD
- A human to approve the code
- A human to merge the PR
- A human to deploy

It's a **semi-automated code generation tool**, not an autonomous coding engine.

### 2. The Model Quality Assumption
We're assuming Qwen3.6-35B at 195 tok/s can produce production-quality code. This is unverified. The live-fire test produced valid JSON but the code was simple (manifest entry). Complex code generation (multi-file, with dependencies) may produce garbage.

### 3. The Gitea Integration Gap
The Gitea API is proven to work for listing repos, but:
- PR creation was never tested from within the engine
- Branch protection rules aren't handled
- Merge conflicts aren't handled
- Code review automation isn't implemented

### 4. The Security Surface
The engine:
- Stores a Gitea token in plain text (`gitea_utils.py`)
- Stores SSH credentials (implicitly via key)
- Writes arbitrary code to Triton
- Executes arbitrary code on Triton

**If the LLM generates malicious code, the engine will execute it.**

---

## SUGGESTED IMPROVEMENTS (prioritized)

### Tier 1: Fix the Foundation (do first)
1. **Add AST validation** to `code_generator.py` — parse every generated file, reject if syntax invalid
2. **Run a real end-to-end test** — simple PRD → generate → test → commit
3. **Consolidate pipeline modules** — delete `work_engine.py`, `work_engine_simple.py`, keep `task_queue.py` + `demo_e2e.py`

### Tier 2: Add Real Intelligence
4. **AST analysis of existing code** — extract imports, classes, functions from project
5. **Framework detection** — auto-detect FastAPI/Flask/Django from imports
6. **Import resolution** — verify generated imports exist in project or are stdlib
7. **Multi-file dependency tracking** — understand how files relate

### Tier 3: Add Quality Gates
8. **Type checking** — run mypy/pyright on generated code
9. **Security scanning** — bandit for Python, npm audit for JS
10. **Performance profiling** — detect obvious O(n²) patterns
11. **Test coverage tracking** — ensure generated code has tests

### Tier 4: Add Deployment
12. **Docker build verification** — build Dockerfile, verify it works
13. **Staging deployment** — deploy to a staging environment
14. **Health check after deploy** — verify the app works
15. **Rollback on failure** — revert if health check fails

### Tier 5: Add Observability
16. **Real-time dashboard** — not just HTML, but WebSocket updates
17. **Cost tracking** — tokens per task, total budget
18. **Performance trending** — speed over time, quality over time
19. **Alert system** — notify on failures or anomalies

---

## FEATURE SUGGESTIONS

### High Value
1. **PRD-from-conversation** — chat with the engine to define what to build
2. **Code review automation** — use judge model to review before commit
3. **Dependency-aware generation** — generate Task B knowing Task A's output
4. **Incremental refinement** — "make this function async", "add error handling"
5. **Multi-model routing** — use fast model for simple tasks, smart model for complex

### Medium Value
6. **Template library** — pre-built templates for common patterns (REST API, CLI tool, etc.)
7. **Project scaffolding** — generate initial project structure from template
8. **Migration assistant** — migrate code between frameworks
9. **Documentation generator** — auto-generate API docs from code
10. **Changelog generator** — auto-generate changelog from git commits

### Low Value (but cool)
11. **Voice interface** — "build me a REST API for user management"
12. **Visual preview** — show generated UI in a browser
13. **A/B testing** — generate multiple implementations, pick the best
14. **Learning from failures** — track what errors occur and adapt prompts

---

## HONEST ASSESSMENT

### What the engine IS:
- A well-structured Python project with clear module separation
- A solid SSH transport layer that actually works
- A good benchmark platform with real data
- A prompt engineering system that's better than average
- A documentation set that's comprehensive

### What the engine IS NOT:
- Autonomous (requires human oversight at every step)
- Production-ready (never tested end-to-end)
- Multi-language (Python-only in practice)
- Secure (stores tokens in plain text, executes arbitrary code)
- Fast (SSH overhead on every operation)

### The 30% that matters most:
1. The SSH transport (proven, reliable)
2. The prompt engineering (well-designed, tested)
3. The benchmark data (real, actionable)

### The 70% that's scaffolding:
Everything else is infrastructure that wraps these three core capabilities. The actual "intelligence" is in the LLM, not in the Python code.

---

## VERDICT

**The engine is a well-built FRAMEWORK for autonomous coding, but it's not yet an autonomous coding engine.** The infrastructure is solid, the prompts are good, the tests are comprehensive. But the core loop (generate → validate → fix → commit) has never been tested with real code on a real project.

**Next step:** Run `demo_e2e.py` on a trivial PRD and see if it actually works. That single test will tell us more than 1000 lines of audit.
