"""
app/api.py's contract: the endpoints the console's frontend depends on. This
does not re-test classify()/score()/evaluate() themselves - those already
have their own extensive coverage - it tests that the API wires them
together correctly and returns what app/static/app.js expects.

TestClient calls the app in-process (no network, no server to start), so
these run in the always-on suite - no live credentials needed. execute_live
defaults False (FakeExecutor), so nothing here touches Razorpay.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import app

client = TestClient(app)


def test_list_cases_returns_the_full_batch():
    resp = client.get("/api/cases")
    assert resp.status_code == 200
    cases = resp.json()
    assert len(cases) == 120
    assert {"case_id", "bucket", "kind", "amount_inr", "issuer", "error"} <= set(cases[0])


def test_list_cases_never_leaks_ground_truth():
    """Ground truth is for a human reading the console, not for the pipeline -
    it must not be present in the list endpoint a case picker actually
    renders from, the same discipline sim/render_trace.py already follows."""
    resp = client.get("/api/cases")
    assert all("ground_truth" not in c for c in resp.json())


def test_get_case_includes_ground_truth_and_customer():
    case_id = client.get("/api/cases").json()[0]["case_id"]
    resp = client.get(f"/api/cases/{case_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert "ground_truth" in body
    assert "customer" in body


def test_get_case_404_for_unknown_id():
    resp = client.get("/api/cases/not_a_real_case")
    assert resp.status_code == 404


def test_decide_404_for_unknown_case_id():
    resp = client.post("/api/decide", json={"case_id": "not_a_real_case"})
    assert resp.status_code == 404


def test_decide_400_for_an_unrecognised_classifier():
    case_id = client.get("/api/cases").json()[0]["case_id"]
    resp = client.post("/api/decide", json={"case_id": case_id, "classifier": "gpt5"})
    assert resp.status_code == 400


def test_decide_returns_all_pipeline_stages():
    case_id = client.get("/api/cases").json()[0]["case_id"]
    resp = client.post("/api/decide", json={"case_id": case_id})
    assert resp.status_code == 200
    body = resp.json()
    for key in ("case", "l1", "l2b", "l2a"):
        assert key in body and body[key] is not None
    assert "l3" in body and "l3_note" in body


def test_decide_defaults_to_fake_execution():
    """execute_live defaults False - clicking around the console must never
    spend real Razorpay quota or create a real order/link by accident."""
    cases = client.get("/api/cases").json()
    # RETRY_ACTIONS/CONTACT_ACTIONS both reach L3 for a case the table reads
    # as SOFT_FUNDS or similar - pick the first case whose decision actually
    # reaches L3, since a HARD_RISK case stops before L3 ever runs.
    for c in cases:
        resp = client.post("/api/decide", json={"case_id": c["case_id"]})
        body = resp.json()
        if body["l3"] is not None:
            assert body["l3"]["live"] is False
            assert body["l3"]["razorpay_order_id"].startswith(("order_fake_", "pay_fake_"))
            return
    pytest.fail("no case in this batch reached L3 - fixture assumption broke")


def test_decide_stop_permanent_never_reaches_l3():
    """A HARD_RISK case (e.g. reason=payment_declined_risk) resolves to
    STOP_PERMANENT or ESCALATE_HUMAN, neither of which calls Razorpay -
    l3 must be null with an explanatory note, not silently absent."""
    cases = client.get("/api/cases").json()
    risk_case = next((c for c in cases if c["error"].get("reason") == "payment_declined_risk"), None)
    if risk_case is None:
        pytest.skip("no HARD_RISK/payment_declined_risk case in this seeded batch")

    resp = client.post("/api/decide", json={"case_id": risk_case["case_id"]})
    body = resp.json()
    assert body["l3"] is None
    assert body["l3_note"] is not None
    assert "never reaches L3" in body["l3_note"]


def test_decide_table_vs_model_can_disagree_on_the_same_case():
    """Not a behavioural guarantee - just proves both classifier paths are
    actually wired to different code (LookupClassifier vs
    CachedLLMClassifier), not the same object under two names."""
    cases = client.get("/api/cases").json()
    case_id = cases[0]["case_id"]
    table_resp = client.post("/api/decide", json={"case_id": case_id, "classifier": "table"}).json()
    model_resp = client.post("/api/decide", json={"case_id": case_id, "classifier": "model"}).json()
    assert table_resp["l1"]["classifier"] == "table"
    assert model_resp["l1"]["classifier"] == "model"
