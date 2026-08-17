# tests/test_config_api.py
# Task 4: config POST = validate (400 on bad schema) + deep-merge into the
# on-disk config (partial saves preserve siblings), instead of replacing the
# whole config with defaults+patch.
#
# Deviation from task-4-brief.md: the brief's `_app(load_admin)` mounts the
# router without auth stubs, but Task 2 made every admin route require
# `open_webui.utils.auth.get_verified_user` (401 otherwise). We use the
# `admin_client` fixture (tests/conftest.py) which stubs that import to a
# role=admin user before building the client. Controller-authorized.
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _app(admin_client):
    return admin_client()


def test_partial_post_preserves_siblings(admin_client):
    c, adm = _app(admin_client)
    adm.qk_atomic_write(
        adm.QK_CONFIG_PATH,
        {"pricing": {"url": "u", "refresh_hours": 12, "overrides": {"m": {"input": 1}}}},
    )
    r = c.post("/api/v1/quota-keeper/config", json={"pricing": {"url": "u2"}})
    assert r.status_code == 200
    cfg = adm.qk_load_json(adm.QK_CONFIG_PATH, {})
    assert cfg["pricing"]["refresh_hours"] == 12 and cfg["pricing"]["overrides"] == {"m": {"input": 1}}
    assert cfg["pricing"]["url"] == "u2"


def test_bad_schema_400(admin_client):
    c, adm = _app(admin_client)
    r = c.post("/api/v1/quota-keeper/config", json={"schedule": "x"})
    assert r.status_code == 400 and "schedule" in r.text


def test_numeric_bounds(admin_client):
    c, adm = _app(admin_client)
    r = c.post("/api/v1/quota-keeper/config", json={"schedule": {"night_start_hour": 99}})
    assert r.status_code == 400
