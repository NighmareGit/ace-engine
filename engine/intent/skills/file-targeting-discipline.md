# Skill: File Targeting Discipline

**Skill ID:** `file-targeting-discipline`
**Purpose:** Create EXACTLY the files declared in the task's files[] — no
more, no less. Never place implementation code in `__init__.py` or other
undeclared files. One module per file.

## When to Use

- Every implementation task. This is the default discipline — apply it unless
  the task explicitly declares additional target files.
- The generation prompt lists `files_to_create` — that list is the contract.

## Procedure

1. **Read the declared files[] list.** The task's `files_to_create` is the
   authoritative list of files you must create. It is pinned into the
   generation prompt by the targeting rail (D3).

2. **Create EXACTLY those files.** Do not create extra "helper" files,
   do not merge two declared files into one, do not split one declared file
   into several. The targeting validator (stage 5) checks that every
   declared file is present in your output and is not an untouched stub.

3. **No code in `__init__.py`.** `__init__.py` is a package marker. Do not
   place implementation logic there. If you need a shared constant, put it
   in a named module and import it.

4. **One module per file.** Each declared file is one module. Keep the
   one-module-per-file invariant — it keeps imports simple and the DAG
   predictable.

5. **Verify before finishing.** Before declaring the task done, confirm
   every file in files[] exists and contains your implementation (not a
   stub, not `pass`, not `TODO`).

## Anti-patterns

- Do NOT create extra files "for cleanliness." The targeting rail flags
  missing declared files — and extra files confuse the next task's context.
- Do NOT place code in `__init__.py`.
- Do NOT leave a declared file as an untouched D2 stub — the targeting
  rail treats stub-identical content as NOT-yet-implemented.

## Output

- Every file in files[] exists with a real implementation.
- No extra files created.
- No code in `__init__.py`.
