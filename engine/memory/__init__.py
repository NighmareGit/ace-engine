"""engine/memory — native cross-run contradiction memory + poisoning gate.

Top-level package owning all memory logic (DESIGN-G3 §1). One interface,
two backends (pattern store via ``engine.workflows.ralph.pattern_gate`` +
verdict store via ``engine/memory/gate.py``), one implementation.

Sub-modules:
- ``comparator`` — ``CrossRunComparator`` (TF-IDF + negation-pair + verdict-position).
- ``gate``      — ``submit_claims`` / ``approve`` / ``reject`` (ADR-0002 gate contract).
- ``recall``    — ``recall_for_objective`` (one-way gated read of approved claims).
"""
