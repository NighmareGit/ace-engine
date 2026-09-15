# Skill: Multi-Module Implementation

**Skill ID:** `multi-module-implementation`
**Purpose:** Scaffold every file declared in a task's files[] before wiring
imports between them; never inline or reimplement a sibling module — code
against its declared contract.

## When to Use

- A task declares multiple target files (files[] has more than one entry).
- Cross-task imports are expected (one task's output is another's dependency).
- The DAG declares sibling modules that must be imported, not duplicated.

## Procedure

1. **Scaffold all files[] first.** Before writing any import wiring, ensure
   every declared file exists. If a file is missing from the workspace,
   create it as a stub carrying the task's title + acceptance criteria (so
   downstream tasks can read the contract). The D2 stub pre-pass does this
   automatically — but if you are adding a new file, scaffold it explicitly.

2. **Code against declared contracts.** Sibling modules are declared in the
   project DAG. Import them; never inline their logic or reimplement their
   functions. If a sibling's API is unclear, read its stub docstring — it
   carries the contract.

3. **Wire imports after scaffolding.** Only after all declared files exist
   should you add the import statements that connect them. This order
   guarantees that `from sibling_module import X` resolves at test time.

4. **One module per file.** Each file is one module. Do not place
   implementation code in `__init__.py` — it is a package marker, not a
   home for logic.

5. **Verify before finishing.** Before declaring the task done, confirm
   every file in files[] exists and contains a real implementation (not a
   stub, not a `pass`).

## Anti-patterns

- Do NOT inline a sibling module's logic because reading its source is
  "faster" than importing it. That creates a duplicate the next task will
  silently diverge from.
- Do NOT place code in `__init__.py`. It is a package marker.
- Do NOT wire imports before all target files exist — the import will
  fail at test time and waste a retry.

## Output

- Every file in files[] exists with a real implementation.
- Cross-module imports use the module path from the DAG.
- No code in `__init__.py`.
