"""F39 -- the CI workflow and the eval overlay must agree on where the datastores are.

This is a drift guard, and it exists because the drift already happened.

`docker-compose.eval.yml` republishes Redis on 56379 and Postgres on 55432,
deliberately: on a developer machine a native service frequently already owns
the default port, compose publishes the container anyway, reports success, and
the runner then talks to the wrong database for an entire ninety-minute run. The
overlay moved the ports to stop that. `.github/workflows/eval.yml` was not
updated, and nothing noticed -- a CI runner has no native Redis, so there was no
bind conflict, nothing failed at boot, the whole sixteen-container stack came up
green, and the run died in preflight ten minutes later.

That is the shape of the failure worth guarding: not a crash, but two files that
each look correct on their own.

These assertions are static -- they parse two text files and compare numbers.
Nothing here boots, connects or waits, so this runs in the offline `eval-tests`
target alongside the case-file validation, and it fails in milliseconds rather
than after a stack boot.
"""

from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
OVERLAY = ROOT / "docker-compose.eval.yml"
WORKFLOW = ROOT / ".github" / "workflows" / "eval.yml"

# "${EVAL_REDIS_PORT:-56379}:6379" -- capture the variable name, the default
# the overlay falls back to, and the container port it maps onto.
PUBLISH_RE = re.compile(
    r'"\$\{(?P<var>EVAL_\w+_PORT):-(?P<host>\d+)\}:(?P<container>\d+)"'
)


@pytest.fixture(scope="module")
def overlay_text() -> str:
    assert OVERLAY.exists(), f"missing {OVERLAY}"
    return OVERLAY.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def workflow_text() -> str:
    assert WORKFLOW.exists(), f"missing {WORKFLOW}"
    return WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def published(overlay_text: str) -> dict[str, int]:
    """{EVAL_REDIS_PORT: 56379, EVAL_POSTGRES_PORT: 55432}."""
    found = {m.group("var"): int(m.group("host"))
             for m in PUBLISH_RE.finditer(overlay_text)}
    assert found, (
        "parsed no ${EVAL_*_PORT:-NNNNN} publications out of the overlay. "
        "Either the overlay changed shape or this guard is now asserting "
        "nothing, which is the more dangerous of the two."
    )
    return found


def _env_value(workflow: str, key: str) -> str:
    """Read one `key: value` from the workflow's job-level env block.

    Deliberately a regex over the text rather than a YAML parse: the workflow
    is full of `${{ }}` expressions, and this guard should not acquire a YAML
    dependency to compare two integers.
    """
    m = re.search(rf"^\s+{re.escape(key)}:\s*\"?([^\"\n]+)\"?\s*$",
                  workflow, re.MULTILINE)
    assert m, f"{key} is not set in {WORKFLOW.name}"
    return m.group(1).strip()


def test_the_overlay_still_moves_both_datastores_off_the_default_ports(published):
    """If this ever fails, the ports moved back and the whole guard is moot.

    Asserted rather than assumed, so that a revert to the defaults has to be a
    deliberate edit to this file and not a silent one.
    """
    assert published.get("EVAL_REDIS_PORT") == 56379
    assert published.get("EVAL_POSTGRES_PORT") == 55432


def test_workflow_redis_url_points_at_the_published_port(workflow_text, published):
    """The exact failure of run 33847248102.

    Everything booted. The analytics API answered, all four fault-injection
    endpoints answered, the graph API answered. Then preflight could not reach
    Redis, and without Redis the alert-suppression reset is a no-op, so cases
    003 and 006 -- which both fault order-svc->payment-svc inside one 600s dedup
    TTL -- would have false-negatived. The runner was right to refuse.
    """
    url = _env_value(workflow_text, "REDIS_URL")
    port = published["EVAL_REDIS_PORT"]
    assert f":{port}/" in url, (
        f"workflow REDIS_URL is {url!r} but the overlay publishes Redis on "
        f"{port}. This does not fail at boot on a CI runner -- it fails in "
        "preflight, after the stack is up."
    )


def test_workflow_postgres_port_points_at_the_published_port(workflow_text, published):
    """Postgres is the quieter half of the same bug.

    eval/store.py defaults POSTGRES_PORT to 5432, so an unset value is not an
    error -- it is a connection to whatever is on 5432, which on a CI runner is
    nothing and on a developer machine is frequently the wrong database.
    """
    assert _env_value(workflow_text, "POSTGRES_PORT") == str(
        published["EVAL_POSTGRES_PORT"])


def test_the_eval_port_variables_are_exported_to_the_job(workflow_text, published):
    """The overlay reads EVAL_*_PORT at `docker compose up` time.

    Setting only REDIS_URL would make the runner right and the containers
    wrong -- the mirror image of the original bug, and harder to spot.
    """
    for var, port in published.items():
        assert _env_value(workflow_text, var) == str(port), (
            f"{var} must be set in the workflow env so `docker compose up` "
            "publishes the same port the runner then connects to"
        )


def test_the_makefile_agrees_too(published):
    """`make eval` and CI must measure the same stack.

    A scoreboard produced locally and one produced in CI are only comparable if
    they were talking to the same ports, and the Makefile has its own copy of
    these defaults in EVAL_ENV.
    """
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    for var, port in published.items():
        assert f"{var}:-{port}" in makefile, (
            f"Makefile EVAL_ENV does not fall back to {port} for {var}"
        )


# ---------------------------------------------------------------------------
# F47 -- the workflow must not name a baseline file that cannot exist.
# ---------------------------------------------------------------------------

def test_workflow_does_not_hardcode_a_baseline_path(workflow_text: str):
    """`--baseline eval/baselines/main.json` was in this workflow for weeks.

    That file has never existed. Baselines are tier-named -- `main-full.json`,
    `main-smoke.json` -- because smoke and full scores are computed over
    different case mixes, and check_regression derives the name from the run's
    own tier. Naming one here could only ever override that with the wrong one.

    It stayed invisible because the runner exits non-zero on a red gate, so the
    step never ran. It would have surfaced on the first GREEN run, as an
    exit 2 reading "no baseline" -- on the one run anybody would have wanted to
    record a baseline from.
    """
    # Comment lines are stripped first: the fix itself explains the flag by
    # name, and a guard that cannot tell an invocation from the note explaining
    # why the invocation was removed is not a guard.
    live = "\n".join(ln for ln in workflow_text.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "--baseline" not in live, (
        "the eval workflow passes --baseline; let check_regression derive "
        "main-<tier>.json from the run's own tier instead"
    )


def test_baseline_filenames_are_tier_scoped():
    """The naming the above depends on, asserted where it is documented."""
    readme = (ROOT / "eval" / "baselines" / "README.md").read_text(encoding="utf-8")
    assert "main-smoke.json" in readme
    assert "main-full.json" in readme


def test_the_drift_report_survives_a_missing_baseline(workflow_text: str):
    """F46. This job's only purpose is to report, and it crashed every night.

    It read the committed baseline unconditionally, and no baseline exists --
    which is the DESIGNED state, since one is recorded only from a green run.
    So the single condition it was guaranteed to meet was the one it could not
    survive, and its failure looked exactly like the eval having failed.

    A reporting job must not have a failure mode that hides its own report.
    """
    i = workflow_text.find("drift-report:")
    assert i > 0, "drift-report job not found"
    job = workflow_text[i:]
    assert "base_path.exists()" in job, (
        "the drift report reads the baseline unconditionally again")
    assert "No committed baseline" in job, (
        "the drift report no longer explains an absent baseline")


def test_the_drift_report_separates_exclusions_from_failures(workflow_text: str):
    """An excluded case is the harness's problem, not the detector's.

    Listing the two together under one heading mislabels a case whose premise
    was false as a case the detector got wrong -- which is the exact confusion
    F44 exists to remove.
    """
    i = workflow_text.find("drift-report:")
    job = workflow_text[i:]
    assert "invalid_precondition" in job
    assert "cases the detector got wrong" in job
    assert "premise false on this host" in job
