#!/usr/bin/env python3
"""M7 误机索赔结算端到端演示（不落真实支付，仅入账）。

运行：python3 -I scripts/demo_m7_claims.py
演示故事线：
  1. 登记误机索赔（证据不全 -> 拒付状态）
  2. 航司补发证据后修订 -> 未结算单按新事实重算应赔
  3. 结算入账（复式凭证 + 回执），回执丢失后同键重试查回原分录
  4. 航司再次修正事实：应赔上调 -> 已结算单生成关联补赔，原凭证不动
  5. 航司推翻误机判定：应赔归零 -> 关联全额追偿
  6. 撤回 -> 反向分录冲销，回读应赔/净入账/待处理差额/凭证链
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.modules.m7_claims.repository import ClaimRepository
from app.modules.m7_claims.service import ClaimService


def _leg(flight_no, origin, destination, sched_dep, sched_arr, act_dep=None, act_arr=None):
    return {"flight_no": flight_no, "origin": origin, "destination": destination,
            "scheduled_dep": sched_dep, "scheduled_arr": sched_arr,
            "actual_dep": act_dep, "actual_arr": act_arr}


def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="m7-demo-"))
    service = ClaimService(ClaimRepository(workdir / "claims.sqlite3"))

    base_itinerary = {
        "itinerary_id": "ITN-DEMO-1",
        "segments": [
            _leg("MU5101", "PVG", "JFK",
                 "2026-11-16T01:00:00+00:00", "2026-11-16T10:00:00+00:00",
                 act_arr="2026-11-16T12:00:00+00:00"),
            _leg("UA857", "JFK", "SFO",
                 "2026-11-16T13:00:00+00:00", "2026-11-16T19:00:00+00:00",
                 act_dep="2026-11-16T13:00:00+00:00",
                 act_arr="2026-11-16T22:30:00+00:00"),
        ],
    }

    print("== 1. 初次登记：只有航司延误证明，证据不全 ==")
    payload = {**base_itinerary, "batch_id": "B-DEMO",
               "evidence": [{"doc_type": "AIRLINE_DELAY_CERT", "doc_no": "ADC-9001",
                             "issued_at": "2026-11-16T18:00:00+00:00"}]}
    registered = service.register(payload, request_key="DEMO-REG")
    claim_id = registered["claims"][0]["claim_id"]
    view = service.readback(claim_id)
    print(f"   索赔号 {claim_id}  自然键 {view['natural_key']}")
    print(f"   应赔={view['due_fen']} 分  拒付原因={view['denial_reason']}")

    print("== 2. 航司补发登机凭证，证据修订（未结算 -> 重算）==")
    full_evidence = [
        {"doc_type": "AIRLINE_DELAY_CERT", "doc_no": "ADC-9001", "issued_at": "2026-11-16T18:00:00+00:00"},
        {"doc_type": "ONWARD_BOARDING_DOC", "doc_no": "OBD-77", "issued_at": "2026-11-16T13:30:00+00:00"},
    ]
    revised = service.revise({**base_itinerary, "claim_id": claim_id,
                              "expected_version": 1, "reason": "AIRLINE_REISSUE",
                              "evidence": full_evidence}, request_key="DEMO-REV-1")
    print(f"   动作={revised['action']}  新应赔={revised['due_fen']} 分  版本=v{revised['version']}")

    print("== 3. 结算入账；模拟回执丢失，同请求键重试 ==")
    receipt = service.settle({"claim_id": claim_id, "expected_version": 2}, request_key="DEMO-SETTLE")
    print(f"   回执 {receipt['receipt_no']}  凭证 {receipt['voucher_no']}  赔付 {receipt['paid_fen']} 分")
    replayed = service.settle({"claim_id": claim_id, "expected_version": 2}, request_key="DEMO-SETTLE")
    print(f"   重试回执一致={replayed == receipt}，凭证数={len(service.readback(claim_id)['voucher_chain'])}")

    print("== 4. 航司修正：终到延误扩大到 390 分钟，已结算 -> 关联补赔 ==")
    heavier = {**base_itinerary,
               "segments": [base_itinerary["segments"][0],
                            {**base_itinerary["segments"][1],
                             "actual_arr": "2026-11-17T01:30:00+00:00"}]}
    up = service.revise({**heavier, "claim_id": claim_id, "expected_version": 3,
                         "reason": "DELAY_EXTENDED", "evidence": full_evidence},
                        request_key="DEMO-REV-2")
    print(f"   动作={up['action']}  补赔凭证={up['vouchers'][0]['voucher_no']} "
          f"金额={up['vouchers'][0]['amount_fen']} 分（原凭证保留）")

    print("== 5. 航司复核：衔接其实正常，误机判定撤销 -> 全额追偿 ==")
    normal = {**base_itinerary,
              "segments": [base_itinerary["segments"][0],
                           {**base_itinerary["segments"][1],
                            "scheduled_dep": "2026-11-16T14:30:00+00:00",
                            "actual_dep": "2026-11-16T14:30:00+00:00"}]}
    down = service.revise({**normal, "claim_id": claim_id, "expected_version": 4,
                           "reason": "AIRLINE_OVERTURN", "evidence": full_evidence},
                          request_key="DEMO-REV-3")
    print(f"   动作={down['action']}  追偿 {down['vouchers'][0]['amount_fen']} 分  拒付原因={down['denial_reason']}")

    print("== 6. 撤回索赔 -> 反向分录；回读台账 ==")
    withdrawal = service.withdraw({"claim_id": claim_id, "expected_version": 5}, request_key="DEMO-WD")
    print(f"   动作={withdrawal['action']}  "
          f"反向凭证={withdrawal.get('voucher_no', '无（净额已为零）')}")
    view = service.readback(claim_id)
    print(f"   状态={view['status']}  应赔={view['due_fen']}  净入账={view['net_posted_fen']}  "
          f"待处理差额={view['pending_difference_fen']}（均为整数分）")
    print("   凭证链：")
    for voucher in view["voucher_chain"]:
        lines = "；".join(f"{e['direction']} {e['account']} {e['amount_fen']}" for e in voucher["entries"])
        print(f"     - {voucher['voucher_no']} {voucher['voucher_type']} "
              f"link={voucher['linked_voucher_no'] or '-'}  [{lines}]")
    print(f"\n演示数据库（仅演示用）：{workdir / 'claims.sqlite3'}")


if __name__ == "__main__":
    main()
