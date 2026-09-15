"""Bad-verdict corpus + mutation-sensitivity test (T09).

10 corpus cases as JSON fixtures under tests/research/fixtures/bad_verdicts/.
Each structural case must be REJECTED by EvidenceValidator with the correct
failing check; the valid case must PASS (no false positive); mutation
sensitivity >= 90%.
"""

import json
import os
import pytest

from engine.research.evidence import EvidenceValidator


FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "bad_verdicts")


def _load_fixtures():
    """Load all bad-verdict corpus fixtures, sorted by name."""
    fixtures = []
    for fname in sorted(os.listdir(FIXTURES_DIR)):
        if not fname.endswith(".json"):
            continue
        with open(os.path.join(FIXTURES_DIR, fname), "r", encoding="utf-8") as f:
            fixtures.append(json.load(f))
    return fixtures


FIXTURES = _load_fixtures()
BAD_FIXTURES = [f for f in FIXTURES if not f["expected_pass"]]
VALID_FIXTURES = [f for f in FIXTURES if f["expected_pass"]]


@pytest.mark.parametrize("fixture", FIXTURES, ids=[f["name"] for f in FIXTURES])
def test_corpus_case(fixture):
    """Each fixture: bad cases rejected (naming the failing check), valid passes."""
    v = EvidenceValidator()
    result = v.validate(fixture["verdict_md"], question=fixture["question"])
    if fixture["expected_pass"]:
        assert result.passed, (
            f"{fixture['name']}: expected PASS but failed at stage "
            f"{result.checks[-1]['name']}: {result.checks[-1].get('error')}"
        )
    else:
        assert not result.passed, (
            f"{fixture['name']}: expected REJECT but passed all stages"
        )
        # The failing check must match the expected one.
        failing = [c for c in result.checks if not c.get("passed", True)]
        assert failing, f"{fixture['name']}: no failing check recorded"
        fail_names = [c["name"] for c in failing]
        expected = fixture["expected_failing_check"]
        assert expected in fail_names, (
            f"{fixture['name']}: expected failing check '{expected}' "
            f"but got {fail_names}"
        )


def test_mutation_sensitivity():
    """Mutation sensitivity >= 90%: each single-element mutation of the valid
    verdict that breaks a structural invariant must be rejected.

    We generate mutations of the valid fixture and assert that at least 90%
    of the mutations that SHOULD fail are indeed rejected.
    """
    valid = next(f for f in FIXTURES if f["name"] == "valid")
    base_md = valid["verdict_md"]
    question = valid["question"]
    v = EvidenceValidator()

    mutations = _generate_mutations(base_md)
    rejected = 0
    total = len(mutations)
    for mut_md in mutations:
        result = v.validate(mut_md, question=question)
        if not result.passed:
            rejected += 1
    sensitivity = rejected / total if total else 0.0
    assert sensitivity >= 0.9, (
        f"mutation sensitivity {sensitivity:.2f} < 0.9 "
        f"({rejected}/{total} mutations rejected)"
    )


def _generate_mutations(md: str):
    """Generate structural mutations of a valid verdict markdown.

    Each mutation breaks one invariant. All should be rejected by the
    deterministic validator.
    """
    mutations = []

    # M1: empty verdict.
    mutations.append("")

    # M2: remove position from Verdict section.
    mutations.append(md.replace("Position: supported", "Position: maybe"))

    # M3: remove confidence.
    mutations.append(md.replace("Confidence: 0.85", ""))

    # M4: remove Sources section.
    mutations.append(md.replace("## Sources\n\n- [src-1] type: file uri: /x/a.txt title: Source A\n- [src-2] type: file uri: /x/b.txt title: Source B\n\n", ""))

    # M5: remove Claims section.
    mutations.append(md.replace("## Claims\n\n- [claim-1] Three-tier caching reduces latency [src-1]\n- [claim-2] Expert cache improves throughput [src-2]\n\n", ""))

    # M6: remove Contradictions section.
    mutations.append(md.replace("## Contradictions\n\n- between [1, 2] resolution: explained by context X\n\n", ""))

    # M7: hallucinated citation (unknown source).
    mutations.append(md.replace("[claim:1, src:1", "[claim:1, src:99"))

    # M8: orphan claim (claim with no source edge) — added inside Claims.
    mutations.append(md.replace(
        "- [claim-2] Expert cache improves throughput [src-2]",
        "- [claim-2] Expert cache improves throughput [src-2]\n"
        "- [claim-99] orphan claim with no source"
    ))

    # M9: self-support (claim cites itself with own text).
    self_support = md.replace(
        '[claim:1, src:1, "three tier caching reduces latency significantly", 0.9]',
        '[claim:1, src:1, "Three-tier caching reduces latency", 0.9]'
    )
    mutations.append(self_support)

    # M10: unresolved contradiction.
    mutations.append(md.replace(
        "resolution: explained by context X",
        "no resolution provided"
    ))

    # M11: malformed marker.
    mutations.append(md.replace("[claim-1]", "[claim-]"))

    # M12: remove all citations AND source markers from claims (zero edges).
    m12 = md.replace(
        "## Citations\n\n[claim:1, src:1, \"three tier caching reduces latency significantly\", 0.9]\n[claim:2, src:2, \"expert cache improves throughput measurably\", 0.8]",
        "## Citations\n\n"
    )
    m12 = m12.replace("[src-1]", "").replace("[src-2]", "")
    mutations.append(m12)

    return mutations
