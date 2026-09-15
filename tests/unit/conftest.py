"""Collect guard: skip CCBS unit tests whose target scripts are not present locally.

These tests import dash-named scripts (e.g. ccbs-state-machine.py) that live in
the deployment checkout (../ccbs-deployment/ccbs/). The local tickets/ccbs/ tree
only carries underscore-named variants, so when the dashed source is absent the
test cannot collect. Skip instead of erroring.
"""
import os

_HERE = os.path.dirname(__file__)
_TARGETS = {
    "test_state_machine.py": "ccbs-state-machine.py",
    "test_judge.py": "ccbs-judge.py",
    "test_config_gen.py": "ccbs-config-gen.py",
}
_CCBS_DIR = os.path.normpath(os.path.join(_HERE, "..", "..", "tickets", "ccbs"))

collect_ignore = [
    test_file
    for test_file, src in _TARGETS.items()
    if not os.path.exists(os.path.join(_CCBS_DIR, src))
]
