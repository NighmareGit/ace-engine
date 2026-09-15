"""Lane B+ protocol — code-gen prompt builder + response parser.

The subject model is asked to emit a SHORT python function ``run(ctx)`` that
composes registry tool calls (code-as-intent, per CodeAct 2402.01030). This
module builds that prompt (tool signatures + docstrings + budget constraints)
and parses the model's text response back into a code string.

Parser contract (strict, per spec §B2): the response MUST contain exactly one
``def run(ctx)`` function. We tolerate markdown fences (like T4's
replan_protocol) and surrounding prose, but if anything that ISN'T a
``def run(ctx)`` function can be extracted, the caller rejects + retries once
then fails with telemetry.

No new deps. No network.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from engine.intent.types import LaneBConfig

log = logging.getLogger("engine.intent.laneb_protocol")

# The one primitive exposed to generated code besides tool() — the queued-intent
# mechanism (grill G3). The prompt MUST advertise it so the model uses it
# instead of trying to call the network / import arbitrary modules.
_LLM_QUERY_DOC = (
    "llm_query(question: str) -> str  "
    "Queue a question for the judge model OUTSIDE the sandbox. The sandbox\n"
    "    pauses, the parent answers it via a separate LLM call, and resumes "
    "with the\n    answer injected. Depth-limited (a queued answer's own "
    "follow-up may recurse\n    once, no deeper). Counts against the call "
    "budget."
)

# Hard constraint preamble the prompt injects so the model knows the sandbox
# contract. The container enforces this too; the prompt is the first line.
_SANDBOX_CONSTRAINTS = (
    "SANDBOX CONSTRAINTS (enforced by the container — do not try to bypass):\n"
    "  - NO network access. NO sockets, NO urllib, NO http, NO subprocess.\n"
    "  - NO file IO (no open/read/write of files).\n"
    "  - Imports limited to a stdlib-safe set (math, json, textwrap, "
    "re, datetime,\n"
    "    collections, itertools, functools, statistics, typing, hashlib, "
    "random).\n"
    "  - Use tool(name, **kwargs) to call registry tools and llm_query(q) to "
    "ask the\n"
    "    judge a question. These are the ONLY external interactions available."
)


def build_code_gen_prompt(
    intent_text: str,
    intent_params: dict[str, Any],
    tools: dict[str, str],
    config: LaneBConfig,
) -> list[dict[str, str]]:
    """Build the chat-completion messages for the code-gen call.

    ``tools`` is {name: description} from the registry. Returns a
    two-message list [system, user] the caller hands to raw_chat_completion.
    """
    tool_lines = []
    for name, desc in tools.items():
        tool_lines.append(f"  - {name}: {desc}")
    tool_block = "\n".join(tool_lines) if tool_lines else "  (none registered)"

    params_block = ""
    if intent_params:
        params_block = (
            "\nIntent parameters (read them via ctx.param('name')):\n"
            + "\n".join(f"  - {k}: {v!r}" for k, v in intent_params.items())
        )

    system = (
        "You are the ACE lane-B+ intent coder. Given a complex or ambiguous "
        "intent, you write a SHORT python function `run()` that composes "
        "registry tool calls to fulfill it (code-as-intent, CodeAct style).\n\n"
        f"{_SANDBOX_CONSTRAINTS}\n\n"
        "RULES:\n"
        "  - Define EXACTLY ONE top-level function: `def run():` (NO arguments).\n"
        "  - `run()` must return a JSON-serializable value (dict/str/list).\n"
        "  - Call registry tools as a GLOBAL function: `tool(name, **kwargs)`.\n"
        "  - Ask the judge a question as a GLOBAL function:\n"
        "    `llm_query(question) -> str` (use when you need more depth).\n"
        "  - Read intent parameters as a GLOBAL function: `param(name)`.\n"
        "  - `tool`, `llm_query`, and `param` are already defined — do NOT\n"
        "    redefine them, do NOT prefix them with `ctx.`.\n"
        "  - Keep it SHORT — a few lines, not a novel.\n"
        "  - Output ONLY the python function. No prose, no explanations "
        "(markdown fences ok).\n\n"
        "Pre-defined globals:\n"
        "  - tool(name, **kwargs) -> result  (call a registry tool)\n"
        f"  - llm_query(question) -> str     ({_LLM_QUERY_DOC.split(chr(10))[0]})\n"
        "  - param(name) -> value            (read an intent parameter)\n\n"
        f"Budget: at most {config.max_llm_queries} llm_query() calls, "
        f"depth <= {config.max_depth}."
    )

    user = (
        f"Intent: {intent_text}\n"
        f"{params_block}\n\n"
        "Available registry tools:\n"
        f"{tool_block}\n\n"
        "Write `def run():` now. Remember: NO `ctx` argument — use the global\n"
        "`tool(...)`, `llm_query(...)`, and `param(...)` functions directly."
    )

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def parse_code_gen_response(text: str) -> str:
    """Parse the model's text response into a ``def run(ctx)`` code string.

    Tolerates markdown fences and surrounding prose (like replan_protocol's
    fence tolerance). STRICT: raises ValueError unless exactly one
    ``def run(ctx)`` function can be extracted. The caller rejects + retries
    once on this, then fails with telemetry.

    Returns the extracted code string (the function + any imports/helpers it
    declares above).
    """
    if not text or not text.strip():
        raise ValueError("empty code-gen response")

    stripped = text.strip()

    # Strip markdown fences if present (```python ... ``` or ``` ... ```).
    fence_match = re.search(r"```(?:python)?\s*\n(.*?)```", stripped, re.DOTALL)
    if fence_match:
        stripped = fence_match.group(1).strip()

    # Find the `def run():` signature (zero-arg; primitives are global).
    m = re.search(r"^def\s+run\s*\(\s*\)\s*:", stripped, re.MULTILINE)
    if not m:
        raise ValueError(
            f"code-gen response has no `def run():` function: {text!r}"
        )

    # There must be exactly one top-level `def run():`.
    all_defs = re.findall(r"^def\s+run\s*\(\s*\)\s*:", stripped, re.MULTILINE)
    if len(all_defs) != 1:
        raise ValueError(
            f"code-gen response must define exactly one `def run():`, "
            f"found {len(all_defs)}: {text!r}"
        )

    # Extract from the def to end of string. Also pull in any import lines
    # that precede it (the model may declare `import json` etc.).
    code = stripped[m.start():] if m.start() > 0 else stripped
    # Prepend any import/from lines that appeared before the def.
    prefix_lines = []
    for line in stripped[:m.start()].splitlines():
        stripped_line = line.strip()
        if stripped_line.startswith(("import ", "from ")):
            prefix_lines.append(line)
    if prefix_lines:
        code = "\n".join(prefix_lines) + "\n\n" + code

    return code


def build_judge_prompt(question: str, intent_text: str,
                       tool_results: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Build the chat-completion messages for an llm_query() judge call.

    The judge answers a queued question OUTSIDE the sandbox. It sees the
    originating intent and any tool results the code produced so far, but is
    told to answer concisely (the answer is injected back into the code).
    """
    context_lines = [f"Originating intent: {intent_text}"]
    if tool_results:
        context_lines.append("Tool results so far:")
        for r in tool_results:
            context_lines.append(f"  - {r}")
    context_block = "\n".join(context_lines)

    system = (
        "You are the ACE lane-B+ judge. A sandboxed intent program has queued "
        "a question that requires more depth than the registry tools provide. "
        "Answer concisely and factually. Output ONLY the answer — no prose, "
        "no fences, no explanations."
    )
    user = f"{context_block}\n\nQuestion: {question}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
