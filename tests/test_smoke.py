# tests/test_smoke.py
from tests.conftest import write_json


def test_modules_load(qk, load_admin):
    adm = load_admin()
    assert callable(qk.qk_record_usage)
    assert callable(adm.qk_fetch_pricing)
