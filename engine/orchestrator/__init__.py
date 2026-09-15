"""ACE orchestrator layer — session, trigger, patch, budget, checkpoint.

Wave-1 core: T1 session-state, T2 trigger, T3 patch contract, T5 stop +
timeouts, T6 git checkpoint, T8 budget caps. The deterministic gate stays
authoritative (ADR-0002); LLMs/patches edit task definitions, never code.
"""
