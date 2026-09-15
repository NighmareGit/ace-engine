# Triage labels — <user>/ace-engine

Canonical state machine for the `triage` skill (label strings as
configured on Gitea; created 2026-09-09):

| Role | Label string | Meaning |
|------|--------------|---------|
| needs evaluation | `needs-triage` | maintainer needs to evaluate |
| waiting on reporter | `needs-info` | blocked on reporter input |
| AFK-ready | `ready-for-agent` | fully specified; an agent can pick it up with no human context |
| human-ready | `ready-for-human` | needs human implementation or decision |
| won't fix | `wontfix` | will not be actioned |

Convenience labels: `bug`, `enhancement`; priorities `P1`, `P2`, `P3`.

Rules:
- Exactly one state label per issue at any time.
- `ready-for-agent` issues must be self-contained: acceptance criteria +
  evidence links in the body (no tribal knowledge required).
- State transitions are recorded by Gitea comments; do not silently
  relabel without a one-line comment saying why.
