"""Re-plan brain — protocol layer (T4b).

Thin adapter between the transport-independent RePlanRequest and the
engine's existing transport.curl_beellama (OpenAI-compatible chat
completions). No new deps. The adapter:

  * builds a JSON-only chat payload from a RePlanRequest
  * parses the LLM's text response into a patch dict (tolerates fences)
"""

import json
import re

from engine.orchestrator.replan_types import RePlanRequest

# System prompt enforcing the T3 strict schema. The LLM may ONLY emit a patch
# object — never code, never file edits. The T3 validator is the backstop.
_SYSTEM_PROMPT = """You are the ACE orchestrator re-plan brain. A task has hit
an escalation trigger (repeated or exhausted failures). Your job is to amend
the TASK DEFINITION so the deterministic pipeline can make progress.

CLASSIFICATION (do this first):
First classify the failure: TARGETING | MISSING-SIBLING | LOGIC | MODEL-LIMIT |
UNRECOVERABLE. State the classification and pick the matching action. Re-spec
only for LOGIC.
- TARGETING -> swap-targeting (declared file paths differ from generated output).
- MISSING-SIBLING -> create-stub (a DAG-sibling module is missing; generate it).
- LOGIC -> re-spec (amend the task definition).
- MODEL-LIMIT -> escalate-model (the model cannot solve this task).
- UNRECOVERABLE -> fail (the task cannot make progress).

RULES:
- You edit the TASK DEFINITION only. You NEVER write, edit, or reference
  source code, files, or commands.
- Respond with ONLY a JSON object conforming to the schema below.
- No markdown fences, no prose, no explanations outside the JSON.

REQUIRED JSON SCHEMA:
{
  "action": "re-spec" | "reorder" | "escalate-model" | "fail" | "create-stub" | "swap-targeting",
  "task_id": "<the task id>",
  "spec_patch": {
    "hypothesis": "<one sentence on why the task is stuck>",
    "constraint_updates": ["<new or tightened constraint>"],
    "dependency_allowlist_additions": ["<task id to allow as a dependency>"],
    "acceptance_amendments": ["<WEAKENED acceptance criterion>"]
  },
  "model_override": "<optional model id for escalate-model>",
  "reason": "<one sentence human-readable reason>"
}

- acceptance_amendments must WEAKEN criteria only (remove/relax/drop), never
  add or strengthen.
- constraint_updates guide the code generator; keep them actionable and
  specific to the failure.
"""


def _build_user_prompt(req: RePlanRequest) -> str:
    """Build the user message describing the failure context."""
    lines = [
        f"Task: {req.task_id} — {req.task_title}",
        f"Description: {req.task_description}",
        f"Dependencies: {', '.join(req.task_dependencies) or '(none)'}",
        f"Trigger: stage={req.trigger_signature[0]} "
        f"error_class={req.trigger_signature[1]}",
        f"Reason: {req.trigger_reason}",
    ]
    if req.hypothesis:
        lines.append(f"Prior hypothesis: {req.hypothesis}")
    if req.error_history:
        lines.append(f"Error history ({len(req.error_history)} attempts):")
        for i, e in enumerate(req.error_history[-5:], 1):
            lines.append(f"  {i}. {e}")
    lines.append(f"Re-plan depth: {req.replan_depth}")
    lines.append("\nEmit the JSON patch object now.")
    return "\n".join(lines)


def build_replan_payload(req: RePlanRequest, max_tokens: int = 4096,
                         temperature: float = 0.2) -> dict:
    """Build an OpenAI-compatible chat payload from a RePlanRequest."""
    return {
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(req)},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }


def parse_replan_response(text: str) -> dict:
    """Parse the LLM's text response into a patch dict.

    Tolerates markdown fences and surrounding prose: extracts the first JSON
    object. Raises ValueError on unparseable output.
    """
    if not text:
        raise ValueError("empty re-plan response")
    # Strip fences if present.
    stripped = text.strip()
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", stripped,
                            re.DOTALL)
    if fence_match:
        stripped = fence_match.group(1).strip()
    # Extract the first JSON object.
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"no JSON object in re-plan response: {text!r}")
    return json.loads(stripped[start:end + 1])
