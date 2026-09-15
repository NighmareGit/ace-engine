# DESIGN-GA — Group A (rebase-and-pin) Operating Procedure

> **Scope:** GA-T01…GA-T04, TARGET-REPO `beellama-port`.
> **Sources:** TICKETS.md (Group A), PRD-GA.json, PORT-DESIGN-RECOMMENDATION.md §3a,
> R02-verdict.md, beellama-port repo state, upstream-drift-stat.txt.
> **Design type:** Procedural git work — the design **is** the operating procedure,
> made deterministic so a 9B-agent can execute it without judgment calls.

---

## 0. Situational snapshot (read first)

The campaign branch carries **4 local commits, all docs, zero code** (paths:
`campaign/`, `campaign/issues/`, `campaign/orchestration/`). `git diff main..HEAD
--stat -- src/ llama-graph.cpp` is empty. Consequence: GA-T01's rebase is
**mechanically clean** — the real conflict surface (llama-graph.cpp layout
refactors) arrives in Group B. `quilt/`, `pinnings/` do not exist yet. `ci/run.sh`
is the build harness; `.github/workflows/` holds only `release*.yml`.

---

## 1. GA-T01 — Rebase onto `preview-v0.4.7` (`53a68d3c3`)

**Command sequence:**

```bash
cd /home/<user>/projects/beellama-port
git checkout feature/three-tier-expert-cache
git rev-parse --verify preview-v0.4.7   # expect 53a68d3c3f36c3467fdb7431cd72473a45c03e86

# Trial merge-tree BEFORE mutating history (expect clean — docs-only, disjoint paths).
git merge-tree --write-tree --no-messages \
  $(git merge-base feature/three-tier-expert-cache preview-v0.4.7) \
  feature/three-tier-expert-cache preview-v0.4.7

# The rebase.
git rebase --onto preview-v0.4.7 \
  $(git merge-base feature/three-tier-expert-cache preview-v0.4.7) \
  feature/three-tier-expert-cache
```

**Conflict policy:** port patches yield to upstream layout, then re-apply intent.
For the actual 4 docs-only commits, no conflicts are expected.

**Abort/rollback:** `git rebase --abort`; original tip recoverable via reflog
(`git reflog feature/three-tier-expert-cache | head -5`).

**Verification gate (build must pass):**

```bash
rm -rf build-ga-t01 && mkdir build-ga-t01 && cd build-ga-t01
cmake -G Ninja -DCMAKE_BUILD_TYPE=Release -DGGML_CUDA=ON ..
cmake --build . --config Release -j$(nproc)   # gate: exit 0
# MoE smoke (structural — no port code yet):
grep -q "build_moe_ffn" ../src/llama-graph.cpp && echo "MoE path present"
./bin/llama-cli --help >/dev/null 2>&1 && echo "binary runs"
```

**Evidence:** `git merge-base --is-ancestor 53a68d3c3 HEAD` true; patch count == 4;
merge-tree output saved to `campaign/results/ga-t01-mergetree.txt`; build log exit 0.

---

## 2. GA-T02 — Quilt mechanism

**Choice: `git format-patches` + `series` file (NOT quilt(1)).** Justification:
quilt is absent from the CI container, adds a second patch format the agent must
reason about; `git am` is native and self-documenting. Directory keeps the
`quilt/` name per the ticket.

**Layout:**

```
beellama-port/
├── quilt/
│   ├── series                  # ordered list of *.patch filenames
│   ├── 0001-….patch … 0004-….patch
└── quilt/rebase-and-reapply.sh
```

**Extraction (from the rebased branch):**

```bash
git format-patch main..feature/three-tier-expert-cache -o quilt --numbered
printf '%s\n' quilt/*.patch > quilt/series
```

**`rebase-and-reapply.sh` interface:** input = new upstream ref (default HEAD);
output = clean tree with all series patches reapplied; non-zero exit on conflict;
idempotent. It loops `git am --3way` over `quilt/series`; on failure it **stops
and names the patch** (never silently takes ours/theirs). The `llama-graph.cpp`
conflict step is explicit — operator resolves per §1.2, then `git am --continue`.

**Evidence:** one patch per local commit + `series`; script produces clean tree on
a fresh checkout; each reapplied patch preserves intent vs. original.

---

## 3. GA-T03 — Snapshot-pin + reapplication

**Where pinned diffs live:** `quilt/pr27861.diff`, `quilt/pr25294.diff`
(byte-identical to `.scratch/beellama-dogfood/sources/`), plus
`quilt/sources-manifest.json` (`{"pr27861.diff":"<sha256>","pr25294.diff":"<sha256>"}`)
and `quilt/REAPPLICATION-TRIGGERS.md`.

**`quilt/pin-refresh.sh` interface:** input = `refresh` (re-snapshot from
`.scratch`) or a new upstream ref; outputs = refreshed diffs + updated manifest +
conflict report in `campaign/results/ga-t03-refresh-<timestamp>.txt`.

**Triggers (per R08, made executable):** (1) force-push to upstream PR branch
(detected by tip change); (2) design-altering review (human signal → operator runs
`pin-refresh.sh`); (3) final upstream merge (snapshot archived, canary stops
tracking).

**Evidence:** `sha256sum` of both diffs matches `.scratch`; manifest records both
hashes; triggers doc lists all three triggers.

---

## 4. GA-T04 — CI canary

**Where:** `ci/quilt-canary.sh` (sibling to existing `ci/run.sh`) +
`.github/workflows/quilt-canary.yml` (triggers on push).

**Canary check = dry-run `git apply --check` of each pinned diff against the
tracked base + `sha256sum -c` against the manifest.** Exit 0 = green (build
proceeds to §1.4 command); non-zero = red (build blocked, human reviews).

```bash
# ci/quilt-canary.sh core logic
for diff in pr27861.diff pr25294.diff; do
  git apply --check "$diff" || exit 1
done
sha256sum -c <(jq -r 'to_entries[] | "\(.value)  \(.key)"' sources-manifest.json)
```

**Local execution (engine gate):** `bash ci/quilt-canary.sh`.

**Negative test (one-time):** a deliberately altered diff (flip one byte) must
fail; result logged; diff then restored from manifest.

**Evidence:** canary exits 0 on real diffs; tampered diff fails (logged); workflow
file triggers on push.

---

## 5. Failure semantics per step

| Step | Failure | Detection | Recovery |
|------|---------|-----------|----------|
| GA-T01 | Rebase conflict | `git rebase` stops | `--abort`, re-apply intent per §1.2 |
| GA-T01 | Build fails | cmake exit ≠ 0 | Inspect logs; base is upstream-clean |
| GA-T02 | `git am` conflict | script exits 1, names patch | Resolve per §1.2, `--continue`, re-run |
| GA-T03 | Hash mismatch | `sha256sum -c` fails | Re-snapshot from `.scratch`; escalate |
| GA-T04 | Canary red | exit ≠ 0 | Block build; human reviews; `pin-refresh.sh` |

---

## 6. Ticket review — confirm / amend

- **GA-T01 — CONFIRM, amend:** ticket says "retarget local patches to new
  llama-graph.cpp layout" but the 4 local commits are docs-only — no code to
  retarget yet. The rebase is mechanically clean. **Amendment:** real deliverable
  is "branch rebased + build green"; `llama-graph.cpp` retargeting is deferred to
  Group B. Ticket should note the conflict surface is deferred.
- **GA-T02 — CONFIRM, amend:** mechanism renamed from "quilt-style" to
  "`git format-patches` + `series`" (justified §2.1); dir keeps `quilt/` name.
- **GA-T03 — CONFIRM.** Add `pin-refresh.sh` as an explicit deliverable.
- **GA-T04 — CONFIRM.** Canary = `git apply --check` + `sha256sum -c`; lives in
  `ci/` + `.github/workflows/`; negative test required once.
