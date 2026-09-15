"""IIL lane-A adapter — OpenAI-compatible tool-call request to the subject model.

Turns an IntentRequest into a chat-completion call with tool definitions, then
parses the ``tool_calls`` array out of the response (the jinja path the spike
proved works on beellama-3070 :8082). Uses the engine's existing transport
pattern (``transport.get_transport()``) — no new deps.

The native lane is the ESCALATION path (~6% edge the router can't resolve).
Latency is GPU-bound (~1.9s p50, documented in spikes) and is NOT counted
against the mechanical dispatch budget (perf law applies to the router path).

Output format this parses (OpenAI ``tool_calls`` array element)::

    {
      "function": {"name": "<action>", "arguments": "<json string>"},
      "id": "...",
      "type": "function"
    }

The spike also showed the subject model emits reasoning tokens on every call
(~48 tok) even for trivial routing — that overhead is accepted, not optimized.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from engine.intent.types import IntentRequest, IntentResult, Lane, Route, Verdict

log = logging.getLogger("engine.intent.native")

# Default subject endpoint (beellama-3070, jinja active). Overridable for tests.
DEFAULT_PORT = int(os.environ.get("ACE_SUBJECT_PORT", "8082"))
DEFAULT_BASE_URL = os.environ.get("ACE_SUBJECT_BASE_URL", "http://127.0.0.1")
DEFAULT_MODEL = os.environ.get("ACE_SUBJECT_MODEL", "qwen3.5-9b-mtp")

# Tool-definition schema sent with every native call. The IIL only sends the
# tools the registry advertises, so the model can't invent an unknown tool
# name via the schema (a second line of defense behind the registry guard).
_TOOL_SCHEMA_TEMPLATE = {
    "type": "function",
    "function": {
        "name": "{name}",
        "description": "{description}",
        "parameters": {"type": "object", "properties": {}},
    },
}


class NativeLane:
    """Lane-A adapter: OpenAI tool-call request → parsed IntentResult.

    Uses the engine's transport layer (``get_transport().curl_beellama``) so it
    inherits the local/ssh/http mode handling the engine already supports.
    """

    def __init__(
        self,
        port: int = DEFAULT_PORT,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        tool_definitions: list[dict[str, Any]] | None = None,
    ) -> None:
        self.port = port
        self.base_url = base_url
        self.model = model
        self._tool_definitions: list[dict[str, Any]] = tool_definitions or []

    # -- tool-definition helpers --------------------------------------------

    def set_tools_from_registry(self, actions: dict[str, str]) -> None:
        """Build the OpenAI tool schema from a {name: description} registry."""
        tools = []
        for name, desc in actions.items():
            tool = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": desc,
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            tools.append(tool)
        self._tool_definitions = tools

    # -- public API ----------------------------------------------------------

    def resolve(self, request: IntentRequest, route: Route) -> IntentResult:
        """Send the intent to the subject model as a tool-call request.

        Returns an IntentResult with lane=NATIVE. ``arguments`` is the parsed
        tool-call argument dict; ``raw`` holds the full tool_calls payload.
        """
        t0 = time.perf_counter()
        try:
            result = self._call_native(request)
            latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            return IntentResult(
                request=request,
                verdict=Verdict.DISPATCH,
                route=route,
                lane=Lane.NATIVE,
                action=result["action"],
                arguments=result["arguments"],
                raw=result["raw"],
                latency_ms=latency_ms,
                reason="native lane resolved the intent",
            )
        except Exception as exc:  # noqa: BLE001 — surface, never crash the layer
            latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            log.warning("native lane failed: %s", exc)
            return IntentResult(
                request=request,
                verdict=Verdict.REJECT,
                route=route,
                lane=Lane.NATIVE,
                latency_ms=latency_ms,
                reason=f"native lane error: {exc}",
            )

    # -- internal -----------------------------------------------------------

    def _call_native(self, request: IntentRequest) -> dict[str, Any]:
        """Build the chat-completion payload and dispatch via transport."""
        messages = [
            {
                "role": "system",
                "content": (
                    "You are the ACE intent resolver. Given the user's intent, "
                    "call exactly one tool. Only call tools that are defined."
                ),
            },
            {"role": "user", "content": request.text},
        ]
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": self._tool_definitions,
            "tool_choice": "auto",
            "temperature": 0.3,
            "max_tokens": 1024,
        }
        # Lazy import: transport may pull in urllib only when actually called.
        # Use raw_chat_completion (NOT curl_beellama): the native lane needs
        # the raw OpenAI response with the tool_calls array intact —
        # curl_beellama flattens the response and drops tool_calls, which made
        # _parse_tool_calls always raise "no choices" (caught by Wave-3b smoke).
        from transport import get_transport  # type: ignore[import-untyped]
        transport = get_transport()
        resp = transport.raw_chat_completion(self.port, payload)
        return self._parse_tool_calls(resp)

    def _parse_tool_calls(self, resp: dict[str, Any]) -> dict[str, Any]:
        """Parse the OpenAI ``tool_calls`` array from a completion response.

        Expected shape::

            {"choices": [{"message": {"tool_calls": [{"function": {"name":.., "arguments":..}}]}}]}
        """
        choices = resp.get("choices") or []
        if not choices:
            raise ValueError("native response has no choices")
        message = choices[0].get("message") or {}
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            raise ValueError("native response has no tool_calls (model declined)")
        # Take the first tool call (single-tool contract).
        tc = tool_calls[0]
        fn = tc.get("function") or {}
        name = fn.get("name", "")
        arguments_raw = fn.get("arguments", "{}")
        try:
            arguments = json.loads(arguments_raw) if isinstance(arguments_raw, str) else (arguments_raw or {})
        except json.JSONDecodeError:
            arguments = {"_raw": arguments_raw}
        return {"action": name, "arguments": arguments, "raw": tool_calls}
