"""M7 HTTP 接口端到端测试：路由、请求幂等头、错误码、页面与回读。"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.modules.m7_claims import repository as repo_mod
from tests.test_m7_domain import FULL_EVIDENCE, _itinerary_payload


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(repo_mod, "DEFAULT_DB_PATH", tmp_path / "claims.sqlite3")
    app.state.m7_service = None
    return TestClient(app)


def _payload(itinerary_id="ITN-WEB-1", batch_id="B-WEB", missed=True):
    payload = _itinerary_payload(itinerary_id, missed=missed)
    payload["batch_id"] = batch_id
    payload["evidence"] = FULL_EVIDENCE
    return payload


def test_claims_page_accessible(client):
    response = client.get("/claims")
    assert response.status_code == 200 and "panel" in response.text and "索赔" in response.text


def test_full_register_settle_readback_flow(client):
    created = client.post("/api/claims/register", json=_payload()).json()
    claim_id = created["claims"][0]["claim_id"]
    receipt = client.post("/api/claims/settle",
                          json={"claim_id": claim_id, "expected_version": 1},
                          headers={"X-Request-Key": "WEB-K-1"}).json()
    assert receipt["paid_fen"] == 15000 and receipt["receipt_no"].startswith("RCP-")

    # 回执丢失后同键重试：同一回执、同一张凭证
    retry = client.post("/api/claims/settle",
                        json={"claim_id": claim_id, "expected_version": 1},
                        headers={"X-Request-Key": "WEB-K-1"}).json()
    assert retry == receipt

    view = client.get(f"/api/claims/{claim_id}").json()
    assert view["due_fen"] == 15000 and view["net_posted_fen"] == 15000
    assert view["pending_difference_fen"] == 0
    assert view["voucher_chain"][0]["voucher_type"] == "SETTLEMENT"
    assert {e["direction"] for e in view["voucher_chain"][0]["entries"]} == {"DR", "CR"}


def test_same_key_different_payload_is_409(client):
    created = client.post("/api/claims/register", json=_payload()).json()
    claim_id = created["claims"][0]["claim_id"]
    client.post("/api/claims/settle", json={"claim_id": claim_id, "expected_version": 1},
                headers={"X-Request-Key": "KX-1"})
    response = client.post("/api/claims/settle", json={"claim_id": claim_id, "expected_version": 2},
                           headers={"X-Request-Key": "KX-1"})
    assert response.status_code == 409 and response.json()["error_code"] == "IDEMPOTENCY_KEY_CONFLICT"


def test_stale_version_returns_409(client):
    created = client.post("/api/claims/register", json=_payload()).json()
    claim_id = created["claims"][0]["claim_id"]
    body = _payload()
    body.update(claim_id=claim_id, expected_version=1)
    assert client.post("/api/claims/revise", json=body).status_code == 200
    stale = client.post("/api/claims/settle", json={"claim_id": claim_id, "expected_version": 1})
    assert stale.status_code == 409 and stale.json()["error_code"] == "STALE_VERSION"


def test_normal_connection_422(client):
    response = client.post("/api/claims/register", json=_payload(itinerary_id="ITN-OK", missed=False))
    assert response.status_code == 422 and response.json()["error_code"] == "CONNECTION_NORMAL"


def test_evidence_incomplete_denied_then_revised_and_settled(client):
    payload = _payload(itinerary_id="ITN-EV")
    payload["evidence"] = [FULL_EVIDENCE[0]]
    claim_id = client.post("/api/claims/register", json=payload).json()["claims"][0]["claim_id"]
    denied = client.post("/api/claims/settle", json={"claim_id": claim_id, "expected_version": 1})
    assert denied.status_code == 422 and denied.json()["error_code"] == "PAYMENT_DENIED"

    complete = _payload(itinerary_id="ITN-EV")
    complete.update(claim_id=claim_id, expected_version=1, reason="EVIDENCE_COMPLETED")
    revised = client.post("/api/claims/revise", json=complete).json()
    assert revised["due_fen"] == 15000 and revised["version"] == 2
    receipt = client.post("/api/claims/settle", json={"claim_id": claim_id, "expected_version": 2}).json()
    assert receipt["status"] == "SETTLED"


def test_settled_revision_creates_supplement_and_preserves_voucher(client):
    claim_id = client.post("/api/claims/register", json=_payload()).json()["claims"][0]["claim_id"]
    client.post("/api/claims/settle", json={"claim_id": claim_id, "expected_version": 1})
    body = _payload()
    body["segments"][1]["actual_arr"] = "2026-11-17T01:30:00+00:00"
    body.update(claim_id=claim_id, expected_version=2)
    result = client.post("/api/claims/revise", json=body).json()
    assert result["action"] == "SUPPLEMENT"
    view = client.get(f"/api/claims/{claim_id}").json()
    assert len(view["voucher_chain"]) == 2
    assert view["voucher_chain"][0]["voucher_type"] == "SETTLEMENT"      # 原凭证保留
    assert view["voucher_chain"][1]["linked_voucher_no"] == view["voucher_chain"][0]["voucher_no"]


def test_withdraw_uses_reverse_entry_and_listing(client):
    claim_id = client.post("/api/claims/register", json=_payload()).json()["claims"][0]["claim_id"]
    client.post("/api/claims/settle", json={"claim_id": claim_id, "expected_version": 1})
    response = client.post("/api/claims/withdraw", json={"claim_id": claim_id, "expected_version": 2})
    assert response.json()["action"] == "REVERSED"
    view = client.get(f"/api/claims/{claim_id}").json()
    assert view["status"] == "WITHDRAWN" and view["net_posted_fen"] == 0
    assert view["voucher_chain"][-1]["voucher_type"] == "WITHDRAWAL"
    listing = client.get("/api/claims").json()["claims"]
    assert any(item["claim_id"] == claim_id for item in listing)


def test_cross_batch_duplicate_registration_returns_same_claim(client):
    first = client.post("/api/claims/register", json=_payload(batch_id="B-A")).json()
    second = client.post("/api/claims/register", json=_payload(batch_id="B-B")).json()
    assert first["claims"][0]["claim_id"] == second["claims"][0]["claim_id"]
    assert second["claims"][0]["created"] is False


def test_batch_settle_endpoint(client):
    client.post("/api/claims/register", json=_payload(itinerary_id="ITN-B1", batch_id="BATCH-X"))
    result = client.post("/api/claims/settle-batch", json={"batch_id": "BATCH-X"},
                         headers={"X-Request-Key": "BATCH-K"}).json()
    assert result["settled_count"] == 1 and result["paid_total_fen"] == 15000
    retry = client.post("/api/claims/settle-batch", json={"batch_id": "BATCH-X"},
                        headers={"X-Request-Key": "BATCH-K"}).json()
    assert retry == result
