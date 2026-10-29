"""The served API matches the committed openapi.yaml contract."""

from pathlib import Path

import yaml
from openapi_spec_validator import validate

from app.schemas import SCENARIO_DEFAULTS, task_out

SPEC = yaml.safe_load((Path(__file__).parent.parent / "openapi.yaml").read_text())


def test_contract_valid():
    validate(SPEC)


def test_every_contract_path_is_served(env):
    served = {(path, m) for path, ops in env.app.openapi()["paths"].items() for m in ops}
    for path, ops in SPEC["paths"].items():
        for method in ops:
            if method in ("get", "post", "patch", "put", "delete"):
                assert (path, method) in served, (path, method)


def test_enums_match():
    s = SPEC["components"]["schemas"]
    assert set(SCENARIO_DEFAULTS) == set(s["Scenario"]["enum"])
    from app.engine.check import ACTIVE, TERMINAL

    assert set(ACTIVE) | set(TERMINAL) | {"draft_pending"} == set(s["TaskStatus"]["enum"])


def test_task_response_fields_in_contract(env):
    t = env.create_task()
    props = set(SPEC["components"]["schemas"]["Task"]["properties"])
    detail_props = props | set(SPEC["components"]["schemas"]["TaskDetail"]["allOf"][1]["properties"])
    assert set(t) <= detail_props, set(t) - detail_props
    assert set(SPEC["components"]["schemas"]["Task"]["required"]) <= set(t)


def test_draft_and_user_fields_in_contract(env):
    env.seed_thread()
    t = env.create_task()
    env.c.post(f"/v1/tasks/{t['id']}/run", headers=env.h("r"))
    d = env.drafts()[0]
    assert set(d) <= set(SPEC["components"]["schemas"]["Draft"]["properties"])
    u = env.c.get("/v1/users", headers=env.h()).json()["data"][0]
    assert set(u) <= set(SPEC["components"]["schemas"]["User"]["properties"])


def test_task_out_helper_has_no_secrets():
    assert "google_refresh_token" not in task_out.__code__.co_names
