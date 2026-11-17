"""M7 服务层测试：登记去重、修订重算、结算凭证、幂等、并发版本、撤回冲销。"""
import threading

import pytest

from app.modules.m7_claims import repository as repo_mod
from app.modules.m7_claims.identity import natural_key
from app.modules.m7_claims.service import ClaimError, ClaimService
from tests.test_m7_domain import FULL_EVIDENCE, _itinerary_payload


@pytest.fixture()
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(repo_mod, "DEFAULT_DB_PATH", tmp_path / "claims.sqlite3")
    return ClaimService(repo_mod.ClaimRepository(tmp_path / "claims.sqlite3"))


def _register_payload(itinerary_id="ITN-1", batch_id="B-1", evidence=None, missed=True):
    payload = _itinerary_payload(itinerary_id, missed=missed)
    payload["batch_id"] = batch_id
    payload["evidence"] = FULL_EVIDENCE if evidence is None else evidence
    return payload


def _first_claim_id(result: dict) -> str:
    return result["claims"][0]["claim_id"]


# ---------- 登记与跨批次去重 ----------

def test_register_creates_claim_with_integer_due(service):
    result = service.register(_register_payload())
    assert result["created_count"] == 1
    claim_id = _first_claim_id(result)
    view = service.readback(claim_id)
    assert view["status"] == "OPEN" and view["version"] == 1
    assert view["due_fen"] == 15000          # 延误 210 分钟落在 120-239 档
    assert view["pending_difference_fen"] == 15000 and view["net_posted_fen"] == 0


def test_same_identity_cross_batch_never_duplicates(service):
    first = service.register(_register_payload(batch_id="B-1"))
    second = service.register(_register_payload(batch_id="B-2"))
    assert first["claims"][0]["claim_id"] == second["claims"][0]["claim_id"]
    assert second["created_count"] == 0 and second["claims"][0]["created"] is False
    assert len(service.list_claims()) == 1
    # 两个批次都能查到同一索赔
    assert {c["claim_id"] for c in service.repo.batch_claims("B-1")} == \
           {c["claim_id"] for c in service.repo.batch_claims("B-2")}


def test_natural_key_identifies_claim_by_itinerary_and_missed_point(service):
    result = service.register(_register_payload())
    key = result["claims"][0]["natural_key"]
    assert key == "ITN-1|JFK|MU5101|UA857"
    assert natural_key("ITN-1", {"airport": "JFK", "inbound_flight": "MU5101",
                                 "outbound_flight": "UA857"}) == key


def test_normal_connection_rejected_at_registration(service):
    with pytest.raises(ClaimError) as exc:
        service.register(_register_payload(missed=False))
    assert exc.value.code == "CONNECTION_NORMAL"


def test_incomplete_evidence_registers_but_denies(service):
    result = service.register(_register_payload(evidence=[FULL_EVIDENCE[0]]))
    view = service.readback(_first_claim_id(result))
    assert view["due_fen"] == 0 and view["denial_reason"] == "EVIDENCE_INCOMPLETE"
    with pytest.raises(ClaimError) as exc:
        service.settle({"claim_id": view["claim_id"], "expected_version": 1})
    assert exc.value.code == "PAYMENT_DENIED"


# ---------- 未结算单修订：按新事实重算，不动凭证 ----------

def test_open_claim_revision_recalculates_due(service):
    claim_id = _first_claim_id(service.register(_register_payload()))
    payload = _register_payload()
    # 实际终到再拖到 23:30 -> 延误 270 分钟，升至 30000 档
    payload["segments"][1]["actual_arr"] = "2026-11-16T23:30:00+00:00"
    payload.update(claim_id=claim_id, expected_version=1, reason="AIRLINE_SUPPLEMENT")
    result = service.revise(payload)
    assert result["action"] == "RECALCULATED" and result["version"] == 2
    view = service.readback(claim_id)
    assert view["due_fen"] == 30000 and view["net_posted_fen"] == 0
    assert len(view["voucher_chain"]) == 0          # 未结算不产生凭证
    assert [r["reason"] for r in view["revisions"]] == ["REGISTER", "AIRLINE_SUPPLEMENT"]


def test_revision_to_normal_connection_denies_open_claim(service):
    claim_id = _first_claim_id(service.register(_register_payload()))
    payload = _register_payload(missed=False)
    payload.update(claim_id=claim_id, expected_version=1)
    result = service.revise(payload)
    assert result["due_fen"] == 0 and result["denial_reason"] == "CONNECTION_NORMAL"


def test_stale_version_revision_rejected(service):
    claim_id = _first_claim_id(service.register(_register_payload()))
    payload = _register_payload()
    payload.update(claim_id=claim_id, expected_version=1)
    service.revise(payload)
    payload["expected_version"] = 1                   # 旧版本重试
    with pytest.raises(ClaimError) as exc:
        service.revise(payload)
    assert exc.value.code == "STALE_VERSION" and exc.value.status == 409

def test_concurrent_revisions_only_one_wins(service):
    claim_id = _first_claim_id(service.register(_register_payload()))
    outcomes = []

    def revise(actual_arr):
        payload = _register_payload()
        payload["segments"][1]["actual_arr"] = actual_arr
        payload.update(claim_id=claim_id, expected_version=1)
        try:
            outcomes.append(("ok", service.revise(payload)["version"]))
        except ClaimError as exc:
            outcomes.append(("stale", exc.code))

    threads = [threading.Thread(target=revise, args=(t,)) for t in
               ("2026-11-16T23:30:00+00:00", "2026-11-17T01:30:00+00:00")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == [("ok", 2), ("stale", "STALE_VERSION")]
    assert service.readback(claim_id)["version"] == 2


# ---------- 结算：凭证、回执、重试不重复 ----------

def test_settle_posts_balanced_voucher_and_receipt(service):
    claim_id = _first_claim_id(service.register(_register_payload()))
    receipt = service.settle({"claim_id": claim_id, "expected_version": 1}, request_key="K-SETTLE-1")
    assert receipt["receipt_no"] == f"RCP-{receipt['voucher_no']}" and receipt["paid_fen"] == 15000
    view = service.readback(claim_id)
    assert view["status"] == "SETTLED" and view["net_posted_fen"] == 15000
    assert view["pending_difference_fen"] == 0
    voucher = view["voucher_chain"][0]
    assert voucher["voucher_type"] == "SETTLEMENT"
    debit = sum(e["amount_fen"] for e in voucher["entries"] if e["direction"] == "DR")
    credit = sum(e["amount_fen"] for e in voucher["entries"] if e["direction"] == "CR")
    assert debit == credit == 15000


def test_lost_receipt_retry_returns_same_voucher(service):
    claim_id = _first_claim_id(service.register(_register_payload()))
    first = service.settle({"claim_id": claim_id, "expected_version": 1}, request_key="K-SETTLE-1")
    # 入账成功但回执丢失：同键重试，查回原分录，不产生第二张凭证/第二笔赔款
    retry = service.settle({"claim_id": claim_id, "expected_version": 1}, request_key="K-SETTLE-1")
    assert retry == first
    assert len(service.readback(claim_id)["voucher_chain"]) == 1


def test_same_key_different_payload_rejected(service):
    claim_id = _first_claim_id(service.register(_register_payload()))
    service.settle({"claim_id": claim_id, "expected_version": 1}, request_key="K-1")
    with pytest.raises(ClaimError) as exc:
        service.settle({"claim_id": claim_id, "expected_version": 2}, request_key="K-1")
    assert exc.value.code == "IDEMPOTENCY_KEY_CONFLICT" and exc.value.status == 409


def test_settle_after_someone_else_revised_rejects_old_version(service):
    claim_id = _first_claim_id(service.register(_register_payload()))
    payload = _register_payload()
    payload.update(claim_id=claim_id, expected_version=1)
    service.revise(payload)                      # 版本推进到 2
    with pytest.raises(ClaimError) as exc:
        service.settle({"claim_id": claim_id, "expected_version": 1})
    assert exc.value.code == "STALE_VERSION"


def test_double_settle_without_key_denied(service):
    claim_id = _first_claim_id(service.register(_register_payload()))
    service.settle({"claim_id": claim_id, "expected_version": 1})
    with pytest.raises(ClaimError) as exc:
        service.settle({"claim_id": claim_id, "expected_version": 2})
    assert exc.value.code == "ALREADY_SETTLED"


# ---------- 已结算修订：补赔/追偿，原凭证保持原样 ----------

def test_settled_revision_up_uses_supplement_voucher(service):
    claim_id = _first_claim_id(service.register(_register_payload()))
    service.settle({"claim_id": claim_id, "expected_version": 1})
    original = service.readback(claim_id)["voucher_chain"][0]

    payload = _register_payload()
    payload["segments"][1]["actual_arr"] = "2026-11-17T01:30:00+00:00"   # 390 分钟 -> 30000
    payload.update(claim_id=claim_id, expected_version=2, reason="NEW_EVIDENCE")
    result = service.revise(payload)
    assert result["action"] == "SUPPLEMENT"
    assert result["vouchers"][0]["amount_fen"] == 30000 - 15000  # 原档 15000 -> 新档 30000
    view = service.readback(claim_id)
    assert view["due_fen"] == 30000 and view["net_posted_fen"] == 30000
    assert view["pending_difference_fen"] == 0
    chain = view["voucher_chain"]
    assert len(chain) == 2 and chain[0]["voucher_no"] == original["voucher_no"]
    # 原凭证金额与内容原样保留
    assert chain[0]["amount_fen"] == 15000 and chain[0]["entries"] == original["entries"]
    assert chain[1]["voucher_type"] == "SUPPLEMENT"
    assert chain[1]["linked_voucher_no"] == original["voucher_no"]


def test_settled_revision_down_uses_recovery_voucher(service):
    # 先按高档 30000 结算
    payload = _register_payload()
    payload["segments"][1]["actual_arr"] = "2026-11-17T01:30:00+00:00"
    claim_id = _first_claim_id(service.register(payload))
    service.settle({"claim_id": claim_id, "expected_version": 1})
    # 航司修正：终到延误只有 150 分钟 -> 应赔 15000，多赔 15000 走追偿
    revised = _register_payload()
    revised.update(claim_id=claim_id, expected_version=2, reason="AIRLINE_CORRECTION")
    result = service.revise(revised)
    assert result["action"] == "RECOVERY" and result["vouchers"][0]["amount_fen"] == 15000
    view = service.readback(claim_id)
    assert view["due_fen"] == 15000 and view["net_posted_fen"] == 15000
    assert view["pending_difference_fen"] == 0
    assert view["voucher_chain"][1]["voucher_type"] == "RECOVERY"


def test_settled_revision_to_evidence_incomplete_recovers_full_amount(service):
    claim_id = _first_claim_id(service.register(_register_payload()))
    service.settle({"claim_id": claim_id, "expected_version": 1})
    payload = _register_payload(evidence=[FULL_EVIDENCE[0]])
    payload.update(claim_id=claim_id, expected_version=2)
    result = service.revise(payload)
    assert result["action"] == "RECOVERY" and result["denial_reason"] == "EVIDENCE_INCOMPLETE"
    view = service.readback(claim_id)
    assert view["due_fen"] == 0 and view["net_posted_fen"] == 0 and view["pending_difference_fen"] == 0


def test_settled_revision_normal_connection_recovers_full(service):
    claim_id = _first_claim_id(service.register(_register_payload()))
    service.settle({"claim_id": claim_id, "expected_version": 1})
    payload = _register_payload(missed=False)
    payload.update(claim_id=claim_id, expected_version=2)
    result = service.revise(payload)
    assert result["action"] == "RECOVERY" and result["denial_reason"] == "CONNECTION_NORMAL"
    view = service.readback(claim_id)
    assert view["due_fen"] == 0 and view["net_posted_fen"] == 0


# ---------- 撤回：反向分录 ----------

def test_withdraw_open_claim_needs_no_voucher(service):
    claim_id = _first_claim_id(service.register(_register_payload()))
    result = service.withdraw({"claim_id": claim_id, "expected_version": 1})
    assert result["action"] == "CANCELLED_OPEN"
    view = service.readback(claim_id)
    assert view["status"] == "WITHDRAWN" and view["due_fen"] == 0
    assert view["voucher_chain"] == [] and view["pending_difference_fen"] == 0


def test_withdraw_settled_claim_posts_reverse_voucher(service):
    claim_id = _first_claim_id(service.register(_register_payload()))
    service.settle({"claim_id": claim_id, "expected_version": 1})
    result = service.withdraw({"claim_id": claim_id, "expected_version": 2}, request_key="K-W-1")
    assert result["action"] == "REVERSED" and result["reversed_fen"] == 15000
    view = service.readback(claim_id)
    assert view["status"] == "WITHDRAWN" and view["net_posted_fen"] == 0
    chain = view["voucher_chain"]
    assert len(chain) == 2 and chain[1]["voucher_type"] == "WITHDRAWAL"
    assert chain[1]["linked_voucher_no"] == chain[0]["voucher_no"]
    # 撤回重试返回同一回执，不重复冲销
    again = service.withdraw({"claim_id": claim_id, "expected_version": 2}, request_key="K-W-1")
    assert again == result and len(service.readback(claim_id)["voucher_chain"]) == 2


def test_withdraw_after_full_recovery_has_no_net_to_reverse(service):
    claim_id = _first_claim_id(service.register(_register_payload()))
    service.settle({"claim_id": claim_id, "expected_version": 1})
    payload = _register_payload(missed=False)
    payload.update(claim_id= claim_id, expected_version=2)
    service.revise(payload)                                # 全额追偿，净入账归零
    result = service.withdraw({"claim_id": claim_id, "expected_version": 3})
    assert result["action"] == "CANCELLED_NO_NET"
    view = service.readback(claim_id)
    assert view["status"] == "WITHDRAWN" and view["net_posted_fen"] == 0
    # 不新增凭证，原结算/追偿凭证链保留
    assert [v["voucher_type"] for v in view["voucher_chain"]] == ["SETTLEMENT", "RECOVERY"]


# ---------- 批次结算 ----------

def test_batch_settle_skips_denied_and_is_retryable(service):
    ok_id = _first_claim_id(service.register(_register_payload(itinerary_id="ITN-1", batch_id="B-9")))
    denied = service.register(_register_payload(itinerary_id="ITN-2", batch_id="B-9",
                                                evidence=[FULL_EVIDENCE[0]]))
    denied_id = _first_claim_id(denied)
    first = service.settle_batch({"batch_id": "B-9"}, request_key="K-BATCH")
    assert first["settled_count"] == 1 and first["paid_total_fen"] == 15000
    assert first["settled"][0]["claim_id"] == ok_id
    assert any(item["claim_id"] == denied_id for item in first["skipped"])
    # 整批重试：原结算单查回，不重复赔款
    retry = service.settle_batch({"batch_id": "B-9"}, request_key="K-BATCH")
    assert retry == first
    assert len(service.readback(ok_id)["voucher_chain"]) == 1


# ---------- 多失接点、身份与异常路径 ----------

def _three_leg_payload(itinerary_id="ITN-MULTI"):
    return {
        "itinerary_id": itinerary_id,
        "batch_id": "B-M",
        "evidence": FULL_EVIDENCE,
        "segments": [
            # 计划 02:00 到 PVG，实际 03:00 到；PVG MCT=60，下一段 03:30 起飞 -> 等待 30 分钟，失接
            {"flight_no": "CA1858", "origin": "PEK", "destination": "PVG",
             "scheduled_dep": "2026-11-16T00:00:00+00:00", "scheduled_arr": "2026-11-16T02:00:00+00:00",
             "actual_dep": "2026-11-16T00:00:00+00:00", "actual_arr": "2026-11-16T03:00:00+00:00"},
            {"flight_no": "MU5101", "origin": "PVG", "destination": "JFK",
             "scheduled_dep": "2026-11-16T03:30:00+00:00", "scheduled_arr": "2026-11-16T10:00:00+00:00",
             "actual_dep": "2026-11-16T03:30:00+00:00", "actual_arr": "2026-11-16T12:00:00+00:00"},
            # 该航段实际到达 JFK 12:00（计划 10:00）；JFK MCT=90，下一段 13:00 起飞 -> 等待 60 分钟，失接
            {"flight_no": "UA857", "origin": "JFK", "destination": "SFO",
             "scheduled_dep": "2026-11-16T13:00:00+00:00", "scheduled_arr": "2026-11-16T19:00:00+00:00",
             "actual_dep": "2026-11-16T13:00:00+00:00",
             "actual_arr": "2026-11-16T22:30:00+00:00"},
        ],
    }


def test_itinerary_with_two_missed_points_registers_two_claims(service):
    result = service.register(_three_leg_payload())
    assert result["created_count"] == 2
    keys = {item["natural_key"] for item in result["claims"]}
    assert keys == {"ITN-MULTI|PVG|CA1858|MU5101", "ITN-MULTI|JFK|MU5101|UA857"}
    # 重复登记不新增
    again = service.register(_three_leg_payload())
    assert again["created_count"] == 0 and len(service.list_claims()) == 2


def test_concurrent_registration_creates_claim_once(service):
    outcomes = []

    def register():
        try:
            outcomes.append(service.register(_register_payload(batch_id="B-C")))
        except ClaimError as exc:
            outcomes.append(exc.code)

    threads = [threading.Thread(target=register) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(service.list_claims()) == 1
    assert all(outcome["created_count"] == 1 or outcome["claims"][0]["created"] is False
               for outcome in outcomes)


def test_revision_changing_identity_rejected(service):
    claim_id = _first_claim_id(service.register(_register_payload()))
    payload = _register_payload(itinerary_id="ITN-CHANGED")
    payload.update(claim_id=claim_id, expected_version=1)
    with pytest.raises(ClaimError) as exc:
        service.revise(payload)
    assert exc.value.code == "IDENTITY_MISMATCH"
    # 被拒后原索赔版本不变
    assert service.readback(claim_id)["version"] == 1


def test_readback_unknown_claim_404(service):
    with pytest.raises(ClaimError) as exc:
        service.readback("CLM-NOPE")
    assert exc.value.code == "CLAIM_NOT_FOUND" and exc.value.status == 404


def test_withdrawn_claim_cannot_be_revised_or_settled(service):
    claim_id = _first_claim_id(service.register(_register_payload()))
    service.withdraw({"claim_id": claim_id, "expected_version": 1})
    payload = _register_payload()
    payload.update(claim_id=claim_id, expected_version=2)
    with pytest.raises(ClaimError) as exc:
        service.revise(payload)
    assert exc.value.code == "CLAIM_WITHDRAWN"
    with pytest.raises(ClaimError) as exc:
        service.settle({"claim_id": claim_id, "expected_version": 2})
    assert exc.value.code == "CLAIM_WITHDRAWN"


def test_revision_without_version_rejected(service):
    claim_id = _first_claim_id(service.register(_register_payload()))
    payload = _register_payload()
    payload["claim_id"] = claim_id
    with pytest.raises(ClaimError) as exc:
        service.revise(payload)
    assert exc.value.code == "VERSION_REQUIRED"


def test_register_idempotent_key_replays_same_receipt(service):
    first = service.register(_register_payload(), request_key="K-REG-1")
    second = service.register(_register_payload(), request_key="K-REG-1")
    assert first == second and len(service.list_claims()) == 1
    with pytest.raises(ClaimError) as exc:
        service.register(_register_payload(batch_id="B-DIFFERENT"), request_key="K-REG-1")
    assert exc.value.code == "IDEMPOTENCY_KEY_CONFLICT"
