# Skills & Personas — Lifecycle, Effectiveness Telemetry, Persona System

**Status: PLANNED** — PRD #34 published, tickets #35-#42 ready-for-agent.

Feature home for the skills/persona layer ABOVE wave-0.

## Document Map

| File | Purpose |
|------|---------|
| `EPIC.md` | Feature epic: 8 hard requirements (R1-R8), module map, dependencies |
| `PRD-v1.md` | Product requirements: 12 user stories, 6 implementation decisions |
| `TICKETS.md` | Ticket slice map: 8 SP tickets, dependency chain |

## Gitea

- PRD issue: [#34](http://<LAN_IP>:3000/<user>/ace-engine/issues/34)
- Tickets: [#35](http://<LAN_IP>:3000/<user>/ace-engine/issues/35) - [#42](http://<LAN_IP>:3000/<user>/ace-engine/issues/42)
- Label: `ready-for-agent`

## Quick Start (post-implementation)

```
python -c "from engine.intent.skills import SkillStore; s=SkillStore(); print(s.skill_ids)"
python -c "from engine.intent.skills import SkillStore; s=SkillStore(); print([d.skill_id for d in s.match_for_task('implement', 'TARGETING')])"
sqlite3 engine.db "SELECT * FROM skill_flags;"
python -c "from engine.intent.personas import PersonaStore; p=PersonaStore(); print(p.fragment_ids)"
```

## Architecture

```
engine/intent/skills.py -> SkillStore (load + validate + match)
  + engine/intent/skills/*.md
  + engine/intent/skill_effectiveness.py (injection->outcome recording)
  + engine/intent/personas.py (PersonaStore)

engine/generator.py -> match_for_task() -> inject skills -> record effectiveness
engine/orchestrator/replan_brain.py -> pass failure_class to compiler
prompt_compiler.py -> compose system prompt (base + task-type + failure-class + skill)
engine/intent/pattern_to_skill.py -> ralph_patterns (approved) -> skill docs
```
