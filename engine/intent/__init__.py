"""Intent Interception Layer (IIL) — deterministic intent parse → validate → dispatch.

Wave-3 (S1/S2/S3): the IIL sits UNDER the orchestrator's trigger/patch/replan
primitives (see .scratch/specs/PREEPIC-intent-interception.md, lane table
FROZEN v1.4). It is the deterministic ACE component that intercepts a model's
expressed intent, validates it against the tool registry, and dispatches the
right tool/code — never mutating tasks/code directly (ADR-0002 preserved).

Three-layer separation (per grok addendum, tickets #7/#11):
  * types.py      — pure dataclasses, no logic (this package's contracts)
  * runtime.py    — router / native lane / pipeline (the mechanical engine)
  * protocol.py   — wire formats, parsers, the OpenAI/jinja bridge

Lane map (FROZEN, spike-w0-results):
  * B+ pre-router : TF-IDF mechanical router → high-confidence hit returns fast
  * A native      : uncertain/low-confidence escalated to OpenAI tool-call lane
  * C fallback    : Hammer/C stays fallback-only — NOT integrated this wave
"""

__version__ = "0.1.0-wave3"
