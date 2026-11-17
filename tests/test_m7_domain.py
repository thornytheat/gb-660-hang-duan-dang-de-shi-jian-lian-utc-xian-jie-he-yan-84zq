"""M7 误机索赔结算：领域规则单元测试（金额整数分、MCT、复式凭证、证据）。"""
import pytest

from app.modules.m7_claims.evidence import EvidencePacket
from app.modules.m7_claims.ledger import (
    DR, CR, recovery_voucher, signed_expense_fen, settlement_voucher,
    supplement_voucher, withdrawal_voucher, voucher_balanced,
)
from app.modules.m7_claims.models import Itinerary
from app.modules.m7_claims.money import compensation_for_delay, require_fen


def _seg(flight_no, origin, destination, sched_dep, sched_arr, act_dep=None, act_arr=None):
    return {
        "flight_no": flight_no, "origin": origin, "destination": destination,
        "scheduled_dep": sched_dep, "scheduled_arr": sched_arr,
        "actual_dep": act_dep, "actual_arr": act_arr,
    }


def _itinerary_payload(itinerary_id="ITN-1", missed=True):
    # JFK 中转，MCT=90 分钟；实际到达 12:00，下一段计划 13:00 起飞 -> 等待 60 分钟，失接
    inbound = _seg("MU5101", "PVG", "JFK",
                   "2026-11-16T01:00:00+00:00", "2026-11-16T10:00:00+00:00",
                   act_arr="2026-11-16T12:00:00+00:00")
    outbound_dep = "2026-11-16T13:00:00+00:00" if missed else "2026-11-16T14:30:00+00:00"
    outbound = _seg("UA857", "JFK", "SFO", outbound_dep, "2026-11-16T19:00:00+00:00",
                    act_dep=outbound_dep, act_arr="2026-11-16T22:30:00+00:00")
    return {"itinerary_id": itinerary_id, "segments": [inbound, outbound]}


FULL_EVIDENCE = [
    {"doc_type": "AIRLINE_DELAY_CERT", "doc_no": "ADC-1", "issued_at": "2026-11-16T18:00:00+00:00"},
    {"doc_type": "ONWARD_BOARDING_DOC", "doc_no": "OBD-1", "issued_at": "2026-11-16T13:30:00+00:00"},
]


# ---------- 金额 ----------

@pytest.mark.parametrize("delay,amount", [(0, 0), (60, 0), (119, 0), (120, 15000),
                                          (180, 15000), (239, 15000), (240, 30000),
                                          (300, 30000), (479, 30000), (480, 60000),
                                          (9999, 60000)])
def test_compensation_tiers_integer_fen(delay, amount):
    assert compensation_for_delay(delay) == amount
    assert isinstance(compensation_for_delay(delay), int)


def test_amounts_must_be_integer_fen():
    require_fen(100)
    with pytest.raises(ValueError, match="AMOUNT_NOT_INTEGER_FEN"):
        require_fen(100.5)
    with pytest.raises(ValueError, match="AMOUNT_NOT_INTEGER_FEN"):
        require_fen("100")
    with pytest.raises(ValueError, match="NEGATIVE_AMOUNT"):
        require_fen(-1)


# ---------- 航段与失接判定 ----------

def test_missed_connection_detected_with_mct():
    itinerary = Itinerary.from_payload(_itinerary_payload())
    points = itinerary.missed_points()
    assert len(points) == 1 and points[0]["status"] == "TOO_SHORT"
    assert points[0]["wait_minutes"] == 60 and points[0]["mct_minutes"] == 90


def test_normal_connection_has_no_missed_point():
    itinerary = Itinerary.from_payload(_itinerary_payload(missed=False))
    assert itinerary.missed_points() == []


def test_unknown_airport_rejected():
    payload = _itinerary_payload()
    payload["segments"][0]["destination"] = "XXX"
    payload["segments"][1]["origin"] = "XXX"
    with pytest.raises(ValueError, match="UNKNOWN_AIRPORT"):
        Itinerary.from_payload(payload).missed_points()


def test_naive_timestamp_rejected_no_timezone_rule_change():
    payload = _itinerary_payload()
    payload["segments"][0]["scheduled_dep"] = "2026-11-16T01:00:00"
    with pytest.raises(ValueError, match="NAIVE_TIME_REJECTED"):
        Itinerary.from_payload(payload)


def test_offset_preserved_but_utc_compared():
    # +08:00 的 09:00 等于 UTC 01:00，顺序比较在 UTC 上做，偏移原样保留
    payload = _itinerary_payload()
    payload["segments"][0]["scheduled_dep"] = "2026-11-16T09:00:00+08:00"
    itinerary = Itinerary.from_payload(payload)
    assert itinerary.segments[0].scheduled_dep.isoformat() == "2026-11-16T01:00:00+00:00"


def test_chain_mismatch_and_unordered_input():
    payload = _itinerary_payload()
    payload["segments"][0]["destination"] = "LHR"
    with pytest.raises(ValueError, match="CHAIN_AIRPORT_MISMATCH"):
        Itinerary.from_payload(payload)
    ordered = Itinerary.from_payload(_itinerary_payload())
    assert [s.flight_no for s in ordered.segments] == ["MU5101", "UA857"]


def test_final_delay_minutes():
    itinerary = Itinerary.from_payload(_itinerary_payload())
    # 计划到达 19:00，实际 22:30 -> 210 分钟
    assert itinerary.final_delay_minutes() == 210


# ---------- 证据 ----------

def test_evidence_completeness():
    assert EvidencePacket.from_payload(FULL_EVIDENCE).is_complete()
    partial = [FULL_EVIDENCE[0]]
    packet = EvidencePacket.from_payload(partial)
    assert not packet.is_complete()
    assert packet.missing_types() == ["ONWARD_BOARDING_DOC"]


def test_malformed_document_rejected():
    with pytest.raises(ValueError, match="MALFORMED_DOCUMENT"):
        EvidencePacket.from_payload([{"doc_type": "AIRLINE_DELAY_CERT", "doc_no": "", "issued_at": "x"}])


# ---------- 复式凭证 ----------

def test_vouchers_are_balanced_double_entry():
    for spec in (settlement_voucher(30000, "s"), supplement_voucher(15000, "p", "V-1"),
                 recovery_voucher(12000, "r", "V-1"), withdrawal_voucher(30000, "w", "V-1")):
        assert voucher_balanced(spec.entries)
        debit = sum(e.amount_fen for e in spec.entries if e.direction == DR)
        credit = sum(e.amount_fen for e in spec.entries if e.direction == CR)
        assert debit == credit


def test_recovery_and_withdrawal_are_reverse_entries():
    recovery = recovery_voucher(12000, "r", "V-1")
    assert recovery.linked_voucher_no == "V-1"
    assert recovery.entries[0].direction == DR and recovery.entries[0].account.endswith("PAYABLE")
    withdrawal = withdrawal_voucher(30000, "w", "V-1")
    assert withdrawal.entries[1].direction == CR
    # 结算 + 全额撤回后净入账为 0
    entries = settlement_voucher(30000, "s").entries + withdrawal.entries
    assert signed_expense_fen(entries) == 0


def test_signed_expense_nets_supplement_and_recovery():
    entries = (settlement_voucher(30000, "s").entries
               + supplement_voucher(15000, "p", "V-1").entries
               + recovery_voucher(12000, "r", "V-1").entries)
    assert signed_expense_fen(entries) == 33000
