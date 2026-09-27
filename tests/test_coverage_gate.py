"""Coverage gate (TESTAPP.md "Test harness"): every scenario in the test app's
registry has at least one e2e test AND a real fixture, so a scenario can't be
added silently.

How a test names its scenario (the gate reads the e2e sources, no phone needed):
- the `scenario("name", ...)` fixture call, or `Scenario("name")` when a test
  launches the screen itself;
- `@pytest.mark.parametrize("name", ["a", "b"])` for tests shared by variants.
Fixtures are tests/fixtures/trees/testapp-<name>.json (or testapp-<name>-<variant>.json).
"""
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCENARIOS = [s["name"] for s in json.loads((ROOT / "android/testapp/scenarios.json").read_text())]

# scenarios with no fixture, and why (keep this short and justified)
NO_FIXTURE = {
    "peer_uid_probe": "a socket probe with no UI worth capturing; its e2e checks the DTA reply",
}


def e2e_covered():
    names = set()
    for f in (ROOT / "tests" / "e2e").glob("test_*.py"):
        src = f.read_text()
        names |= set(re.findall(r'\b[sS]cenario\(\s*"([a-z0-9_]+)"', src))
        for body in re.findall(r'@pytest\.mark\.parametrize\(\s*"name"\s*,\s*\[([^\]]*)\]', src):
            names |= set(re.findall(r'"([a-z0-9_]+)"', body))
    return names


def test_every_scenario_has_an_e2e_test():
    missing = sorted(set(SCENARIOS) - e2e_covered())
    assert not missing, f"scenarios without an e2e test: {missing}"


def test_every_scenario_has_a_real_fixture():
    trees = {p.name for p in (ROOT / "tests" / "fixtures" / "trees").glob("testapp-*.json")}
    missing = [n for n in SCENARIOS if n not in NO_FIXTURE
               and f"testapp-{n}.json" not in trees
               and not any(t.startswith(f"testapp-{n}-") for t in trees)]
    assert not missing, f"scenarios without a fixture (make fixtures): {missing}"


def test_the_gate_sees_the_registry():
    assert len(SCENARIOS) >= 80 and "cart" in SCENARIOS
    assert set(NO_FIXTURE) <= set(SCENARIOS)
