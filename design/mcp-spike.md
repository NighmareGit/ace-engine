# MCP Client Integration — Spike Design Assessment (P5)

Status: SPIKE ONLY — no MCP runtime code this wave. This document assesses
whether ACE should integrate an MCP client, what it would gain, the protocol
surface, sandbox interplay, security concerns, and a GO/NO-GO recommendation
with evidence. It is the concretization of EPIC-orchestrator ORCH-6's spike
gate and PREEPIC-intent-interception P5.

Owner verdict target: a single GO / CONDITIONAL-GO / NO-GO with evidence.

## 1. What the engine would gain

MCP (Model Context Protocol) is an open standard for connecting LLMs to
external tools and data sources via a client/server model. An MCP client
inside ACE's IIL would gain:

| Capability | Mechanism | Value to ACE |
|---|---|---|
| **Tool marketplace draw** | 18,166+ public servers (mcp.so), 3,938 curated (Glama) — LEDGER entry 5 | Reuse existing tool servers instead of hand-writing each (web_search, github, docs already built this wave are examples of tools an MCP server could have provided). |
| **Standardized tool schema** | MCP advertises tool names, descriptions, JSON-Schema params | Fits the IIL registry contract: registry entries could be derived from MCP tool descriptions. |
| **Multi-transport tool access** | stdio, SSE, streamable-HTTP transports | Lets ACE reach both local (stdio) and remote (HTTP) tool servers. |
| **Ecosystem portability** | Swap providers without changing the model | Decouples ACE from any single tool vendor. |

**Honest accounting:** the three tools built this wave (T1 web_search, T2
github_lookup, T3 docs_domains) cover ACE's actual P2-P4 needs with ~900 LOC
total and full egress control. MCP's value to ACE is therefore *marginal until
the engine needs a 4th+ external tool* — at which point the per-tool cost of
the hand-rolled allowlist pattern rises and MCP's marketplace becomes
attractive. MCP is a force-multiplier for tool breadth, not a reliability fix.

## 2. Protocol surface

MCP is a JSON-RPC 2.0 protocol with a lifecycle:

```
client ──initialize──▶ server   (capability negotiation)
client ──tools/list──▶ server   (advertised tool surface)
client ──tools/call──▶ server   (invoke a tool → result)
server ──resources/read──▶ client (optional: server pushes data)
server ──prompts/list──▶ client  (optional: prompt templates)
```

Key surfaces an ACE client must implement:

| Surface | Complexity | Notes |
|---|---|---|
| `initialize` handshake | Low | Version + capability negotiation. |
| `tools/list` + `tools/call` | Medium | The core. Tool args are arbitrary JSON — must be validated before dispatch (ACE's existing guard). |
| `resources/read`, `prompts/list` | Low | Optional; out of scope for a v1 client. |
| Transport: stdio | Medium | Spawn server as subprocess, speak JSON-RPC over pipes. |
| Transport: streamable-HTTP/SSE | Higher | HTTP server in-process or reverse-proxied; needs auth. |
| OAuth 2.1 / DCR (dynamic client registration) | High | Required for remote servers with user data; the spec is still hardening (MCP security best practices, 2026-07-28). |

**Integration point with ACE:** an MCP client would be a *new IIL tool
backend* — a fourth channel beside the router, native lane, and fallback.
The registry would gain an `mcp:<server>:<tool>` namespace, and the
egress-enforcement test (T6) would need to extend to MCP server hosts. This
is additive, not invasive, IF the client is gated behind a registry flag.

## 3. Sandbox interplay

ACE's Wave-3b sandbox (seccomp+namespaces container, no network, read-only
rootfs) is the execution boundary for intent code. MCP interplay:

| Concern | Analysis |
|---|---|
| **MCP server as subprocess** | A stdio MCP server would run *inside* the sandbox if spawned by sandboxed intent code — but the sandbox has **no network**, so HTTP-based MCP servers are unreachable from inside. A v1 MCP client must therefore run **outside** the sandbox (host-side), like the native lane. |
| **Tool-call routing** | MCP tool calls would route: model → IIL → MCP client (host) → MCP server. The MCP client becomes a new egress surface — every MCP server host is a network destination that must be allowlisted (extends invariant #4). |
| **Prompt injection via tool output** | MCP server responses are untrusted content that flows back into the model's context. This is the same threat as web_search/docs results (already handled by response caps), but MCP tool output can be *structured to manipulate the model* (tool poisoning). ACE's response-size caps mitigate volume, not semantics. |
| **No callback from sandbox** | Lane B+ has no callback (PREEPIC G3). An MCP client callable from sandboxed code would need the same queued-intent primitive as `llm_query()` — out of scope for v1. |

**Bottom line:** MCP client = host-side egress component, not a sandboxed
callable. It fits the "direct allowlisted HTTP call" pattern of T1-T3, but
each MCP server is a new allowlist entry.

## 4. Security concerns (load-bearing)

The 2026 MCP security landscape is active and concerning. Evidence:

1. **30+ CVEs filed Jan–Feb 2026** against MCP servers, clients, and SDKs
   (OWASP MCP Top 10, Cycode 2026-06-24). The attack surface is real and
   growing.
2. **Prompt injection + tool poisoning** are the dominant classes: a
   malicious or compromised MCP server embeds instructions in tool
   descriptions or results to hijack the model (Checkmarx 2026-07-30;
   CyCognito). ACE's threat model (adversary controls repo content →
   prompt-injects model) is *amplified* by MCP: the server is a new injection
   channel the repo doesn't even need to touch.
3. **Postmark MCP incident (Sep 2025):** a widely-installed email MCP server
   pushed a malicious update that BCC'd every agent-sent email to an
   attacker-controlled domain (Wiz 2026-06-23). Supply-chain risk is first-
   class: an MCP server update can silently escalate.
4. **Credential misuse:** MCP servers often hold API keys; a compromised
   server exfiltrates them. ACE's token-from-env pattern (T2) would need
   per-server credential isolation.
5. **Small-model tool-calling reliability is unvalidated on our stack:**
   local models sometimes answer instead of calling the MCP tool (Reddit
   r/LocalLLaMA 2026-02-08); the reliable tool-callers are ≥30B (Qwen3-Coder
   30B, Gemma 4 27B — PromptQuorum 2026). Our 9B subject is below the
   validated floor; our 35B judge is above it but is reserved for re-plan.

ACE-specific risk summary: MCP introduces a **supply-chain + prompt-injection
surface that the current allowlist pattern (T1-T3) deliberately avoids** by
pinning exact hosts and response shapes. MCP's flexibility is the opposite of
the egress-allowlist law.

## 5. Recommendation

### CONDITIONAL GO — gated on three preconditions.

The engine does not need MCP to ship the reach tier (T1-T3 cover P2-P4) or
the orchestrator core (AC1-AC6). MCP is a breadth play whose security cost
is only justified when tool demand exceeds what the hand-rolled allowlist
pattern can sustain. Recommend:

**GO when ALL of the following hold:**
1. **A concrete tool need arises that T1-T3 don't cover** AND the public MCP
   marketplace has a well-audited server for it (evidence: server source
   reviewed, no dynamic client registration required, permissive license).
2. **The 35B judge (or a validated ≥30B tool-caller) drives MCP tool
   selection** — the 9B subject must NOT be the primary MCP driver (its
   tool-calling reliability is unvalidated; the 35B is above the validated
   floor). This keeps MCP in the escalation path, not the hot path.
3. **A per-server egress allowlist + credential isolation + response
   validation layer is in place** before any MCP server is called — i.e.
   MCP servers are treated as untrusted content sources, same as search
   results.

**NO-GO otherwise.** Specifically, a NO-GO if:
- The motivation is "ecosystem" without a concrete tool need (YAGNI — T1-T3
  already cover the engine's actual needs).
- The 9B subject would drive MCP tool calls (reliability unvalidated).
- Remote MCP servers with OAuth/DCR are required (spec still hardening,
  security surface too large for v1).

### Evidence summary

| Factor | Evidence | Verdict |
|---|---|---|
| Tool breadth | 18k+ servers (LEDGER §5) | Strong — but breadth ≠ need |
| Small-model reliability | 9B unvalidated; ≥30B validated (PromptQuorum 2026; Reddit 2026-02) | Weak for our 9B |
| Security | 30+ CVEs in 2 months (Cycode); Postmark supply-chain (Wiz); tool poisoning (Checkmarx) | Active risk |
| Egress control | MCP server hosts are dynamic → hard to allowlist | Tension with invariant #4 |
| Sandbox fit | Must run host-side (sandbox has no network) | Fits T1-T3 pattern |
| ACE need | T1-T3 cover P2-P4; no 4th tool needed yet | Not load-bearing |

## 6. If GO — spike protocol (future wave, not this one)

1. Pick ONE concrete tool need not covered by T1-T3 (e.g. a CI status
   checker). Find the simplest MCP server for it.
2. Implement a **stdio MCP client** (subprocess, JSON-RPC) that speaks
   `initialize` + `tools/list` + `tools/call`. No OAuth, no HTTP transport.
3. Gate it behind a registry flag `mcp_enabled=False` by default. Each server
   allowlisted in a new `engine/intent/allowlists/mcp_servers.txt`.
4. Drive tool selection with the 35B judge only; measure intent accuracy
   ≥90% and structural validity ≥98% on a 20-intent canary set.
5. Extend the egress-enforcement test (T6) to assert MCP server hosts are
   allowlisted.
6. Decide GO/NO-GO from the canary metrics.

## 7. Open questions

1. Does the 35B judge's tool-calling reliability hold against real MCP
   servers (not just native tool-call schemas)? → spike measures this.
2. Can MCP tool responses be capped + sanitized to the same contract as
   web_search/docs results (size cap, no exec)? → design TBD at spike.
3. How does MCP's `resources/read` surface interact with ACE's context
   budget? → likely a size-capped read, same as docs.
4. Supply-chain vetting: what audit bar qualifies an MCP server for the
   allowlist? → owner decision; suggest "source-reviewed + pinned commit +
   no dynamic code fetch".
