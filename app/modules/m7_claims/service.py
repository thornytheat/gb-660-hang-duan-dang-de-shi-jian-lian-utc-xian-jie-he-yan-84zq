"""索赔域应用服务：登记、证据修订、结算、撤回、回读。

业务红线：
- 已入账凭证永不修改：已结算单的事实变化只生成关联补赔/追偿凭证。
- 未结算单按新事实重算应赔；正常衔接或证据不全一律拒付。
- 所有写接口走请求幂等键，重试返回原回执，不重复赔款。
- 修订与结算带期望版本号，并发下旧版本拒绝。
- 不发生真实支付，只生成会计凭证与结算回执。
"""
import hashlib
import json

from .evidence import EvidencePacket
from .identity import natural_key
from .ledger import (
    recovery_voucher,
    signed_expense_fen,
    settlement_voucher,
    supplement_voucher,
    withdrawal_voucher,
)
from .models import Itinerary
from .money import compensation_for_delay
from .repository import ClaimRepository

REASON_NORMAL_CONNECTION = "CONNECTION_NORMAL"
REASON_EVIDENCE_INCOMPLETE = "EVIDENCE_INCOMPLETE"


class ClaimError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _claim_id(key: str) -> str:
    return "CLM-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12].upper()


def _request_hash(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _evaluate(itinerary: Itinerary, point: dict, evidence: EvidencePacket) -> dict:
    """按当前事实与证据计算应赔与拒付原因。金额为整数分。"""
    delay = itinerary.final_delay_minutes()
    complete = evidence.is_complete()
    if point["status"] == "VALID":
        return {"delay_minutes": delay, "due_fen": 0, "evidence_complete": complete,
                "denial_reason": REASON_NORMAL_CONNECTION}
    if not complete:
        return {"delay_minutes": delay, "due_fen": 0, "evidence_complete": False,
                "denial_reason": REASON_EVIDENCE_INCOMPLETE}
    return {"delay_minutes": delay, "due_fen": compensation_for_delay(delay),
            "evidence_complete": True, "denial_reason": None}


def _replay_or_start(repo: ClaimRepository, request_key: str | None, kind: str, payload: dict):
    """幂等入口：返回 (db, tx, prior_response)。

    同键同载荷 -> 复用原回执（db/tx 为 None）；同键异载荷 -> 拒绝；无键 -> 直接开新事务。
    """
    if not request_key:
        tx = repo.transaction()
        return tx.__enter__(), tx, None
    prior = repo.load_request(request_key)
    if prior is not None:
        if prior["request_hash"] != _request_hash(payload):
            raise ClaimError("IDEMPOTENCY_KEY_CONFLICT",
                            "同号请求的载荷与首次不一致，拒绝执行", status=409)
        return None, None, json.loads(prior["response_json"])
    tx = repo.transaction()
    return tx.__enter__(), tx, None


def _commit_idempotent(tx, db, request_key: str, kind: str, payload: dict,
                       claim_id: str | None, response: dict) -> None:
    if request_key:
        tx.repo.save_request(db, request_key, kind, claim_id, _request_hash(payload), response)
    db.execute("COMMIT")
    db.close()
    tx.repo._lock.release()


def _rollback(tx, db) -> None:
    if tx is None:
        return
    try:
        db.execute("ROLLBACK")
    finally:
        db.close()
        tx.repo._lock.release()


# 仓储/模型层技术错误 -> 对外稳定领域错误码
_VALUE_ERROR_STATUS = {
    "STALE_VERSION": 409,
    "STALE_VERSION_OR_NOT_OPEN": 409,
    "CLAIM_NOT_FOUND": 404,
}


def _as_claim_error(exc: Exception) -> ClaimError:
    if isinstance(exc, ClaimError):
        return exc
    code = str(exc)
    return ClaimError(code, code, status=_VALUE_ERROR_STATUS.get(code, 422))


class ClaimService:
    def __init__(self, repo: ClaimRepository | None = None):
        self.repo = repo or ClaimRepository()

    # ---------- 索赔登记 ----------

    def register(self, payload: dict, request_key: str | None = None) -> dict:
        batch_id = str(payload.get("batch_id") or "DEFAULT").strip()
        itinerary = Itinerary.from_payload(payload)
        evidence = EvidencePacket.from_payload(payload.get("evidence", []))
        points = itinerary.missed_points()
        if not points:
            raise ClaimError("CONNECTION_NORMAL", "行程衔接正常，不予登记误机索赔")

        request_payload = {"batch_id": batch_id, "itinerary": itinerary.to_payload(),
                           "evidence": evidence.to_payload()}
        tx = None
        try:
            db, tx, prior = _replay_or_start(self.repo, request_key, "REGISTER", request_payload)
            if prior is not None:
                return prior
            results = []
            for point in points:
                key = natural_key(itinerary.itinerary_id, point)
                existing = self.repo.get_claim_by_key(db, key)
                if existing is not None:
                    self.repo.mark_batch_seen(db, batch_id, existing["claim_id"])
                    results.append({"claim_id": existing["claim_id"], "natural_key": key,
                                    "created": False, "status": existing["status"],
                                    "due_fen": existing["due_fen"]})
                    continue
                verdict = _evaluate(itinerary, point, evidence)
                claim = {
                    "claim_id": _claim_id(key),
                    "natural_key": key,
                    "itinerary_id": itinerary.itinerary_id,
                    "itinerary": itinerary.to_payload(),
                    "point": point,
                    "evidence": evidence.to_payload(),
                    **verdict,
                    "status": "OPEN",
                    "version": 1,
                }
                self.repo.insert_claim(db, claim, batch_id)
                results.append({"claim_id": claim["claim_id"], "natural_key": key,
                                "created": True, "status": "OPEN",
                                "due_fen": claim["due_fen"],
                                "denial_reason": claim["denial_reason"]})
            response = {"batch_id": batch_id, "itinerary_id": itinerary.itinerary_id,
                        "claims": results, "created_count": sum(1 for r in results if r["created"])}
            _commit_idempotent(tx, db, request_key, "REGISTER", request_payload, None, response)
            return response
        except Exception as exc:
            _rollback(tx, db if tx is not None else None)
            if isinstance(exc, (ValueError, LookupError)):
                raise _as_claim_error(exc) from exc
            raise

    # ---------- 证据/事实修订 ----------

    def revise(self, payload: dict, request_key: str | None = None) -> dict:
        claim_id = str(payload.get("claim_id", "")).strip()
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or isinstance(expected_version, bool):
            raise ClaimError("VERSION_REQUIRED", "修订必须携带整数 expected_version")
        reason = str(payload.get("reason", "EVIDENCE_REVISE")).strip() or "EVIDENCE_REVISE"
        itinerary = Itinerary.from_payload(payload)
        evidence = EvidencePacket.from_payload(payload.get("evidence", []))

        request_payload = {"claim_id": claim_id, "expected_version": expected_version,
                           "reason": reason, "itinerary": itinerary.to_payload(),
                           "evidence": evidence.to_payload()}
        tx = None
        try:
            db, tx, prior = _replay_or_start(self.repo, request_key, "REVISE", request_payload)
            if prior is not None:
                return prior
            claim = self._load(db, claim_id)
            self._check_version(claim, expected_version)
            self._guard_identity(claim, itinerary)
            points = itinerary.missed_points()
            point = self._match_point(claim, points)
            verdict = _evaluate(itinerary, point, evidence)

            if claim["status"] == "OPEN":
                updated = self.repo.update_claim_revision(
                    db, claim_id, expected_version, itinerary.to_payload(), evidence.to_payload(),
                    verdict["evidence_complete"], verdict["delay_minutes"], verdict["due_fen"],
                    reason, verdict["denial_reason"])
                response = {"claim_id": claim_id, "settled": False, "action": "RECALCULATED",
                            "version": updated["version"], "due_fen": updated["due_fen"],
                            "denial_reason": updated["denial_reason"], "vouchers": []}
            elif claim["status"] == "WITHDRAWN":
                raise ClaimError("CLAIM_WITHDRAWN", "索赔已撤回，不接受修订", status=409)
            else:
                updated = self.repo.update_claim_revision(
                    db, claim_id, expected_version, itinerary.to_payload(), evidence.to_payload(),
                    verdict["evidence_complete"], verdict["delay_minutes"], verdict["due_fen"],
                    reason, verdict["denial_reason"])
                vouchers = self._post_settlement_adjustment(db, claim, updated, reason)
                response = {"claim_id": claim_id, "settled": True,
                            "action": vouchers[0]["voucher_type"] if vouchers else "NO_CHANGE",
                            "version": updated["version"], "due_fen": updated["due_fen"],
                            "denial_reason": updated["denial_reason"], "vouchers": vouchers}
            _commit_idempotent(tx, db, request_key, "REVISE", request_payload, claim_id, response)
            return response
        except Exception as exc:
            _rollback(tx, db if tx is not None else None)
            if isinstance(exc, (ValueError, LookupError)):
                raise _as_claim_error(exc) from exc
            raise

    # ---------- 结算 ----------

    def settle(self, payload: dict, request_key: str | None = None) -> dict:
        claim_id = str(payload.get("claim_id", "")).strip()
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or isinstance(expected_version, bool):
            raise ClaimError("VERSION_REQUIRED", "结算必须携带整数 expected_version")
        request_payload = {"claim_id": claim_id, "expected_version": expected_version}
        tx = None
        try:
            db, tx, prior = _replay_or_start(self.repo, request_key, "SETTLE", request_payload)
            if prior is not None:
                return prior
            claim = self._load(db, claim_id)
            self._check_version(claim, expected_version)
            if claim["status"] == "SETTLED":
                raise ClaimError("ALREADY_SETTLED", "索赔已结算，重复结算拒绝", status=409)
            if claim["status"] == "WITHDRAWN":
                raise ClaimError("CLAIM_WITHDRAWN", "索赔已撤回，不能结算", status=409)
            if claim["denial_reason"] is not None or claim["due_fen"] <= 0:
                raise ClaimError("PAYMENT_DENIED",
                                f"拒付：{claim['denial_reason'] or 'NO_DUE_AMOUNT'}")
            updated = self.repo.mark_settled(db, claim_id, expected_version)
            voucher_no = self.repo.next_voucher_no(db, claim_id)
            spec = settlement_voucher(
                claim["due_fen"],
                f"{claim['natural_key']} 误机赔付结算 v{updated['version']}")
            self.repo.insert_voucher(db, claim_id, voucher_no, spec)
            response = {"claim_id": claim_id, "receipt_no": f"RCP-{voucher_no}",
                        "voucher_no": voucher_no, "paid_fen": claim["due_fen"],
                        "version": updated["version"], "status": "SETTLED"}
            _commit_idempotent(tx, db, request_key, "SETTLE", request_payload, claim_id, response)
            return response
        except Exception as exc:
            _rollback(tx, db if tx is not None else None)
            if isinstance(exc, (ValueError, LookupError)):
                raise _as_claim_error(exc) from exc
            raise

    def settle_batch(self, payload: dict, request_key: str | None = None) -> dict:
        """批量结算：逐条跑结算，已结算/拒付跳过；整批评分可安全重试。"""
        batch_id = str(payload.get("batch_id", "")).strip()
        if not batch_id:
            raise ClaimError("BATCH_ID_REQUIRED", "批量结算必须指定 batch_id")
        request_payload = {"batch_id": batch_id}
        tx = None
        try:
            db, tx, prior = _replay_or_start(self.repo, request_key, "SETTLE_BATCH", request_payload)
            if prior is not None:
                return prior
            settled, skipped = [], []
            for claim in self.repo.batch_claims(batch_id):
                if claim["status"] != "OPEN":
                    skipped.append({"claim_id": claim["claim_id"], "reason": f"STATUS_{claim['status']}"})
                    continue
                if claim["denial_reason"] is not None or claim["due_fen"] <= 0:
                    skipped.append({"claim_id": claim["claim_id"],
                                    "reason": claim["denial_reason"] or "NO_DUE_AMOUNT"})
                    continue
                updated = self.repo.mark_settled(db, claim["claim_id"], claim["version"])
                voucher_no = self.repo.next_voucher_no(db, claim["claim_id"])
                spec = settlement_voucher(
                    claim["due_fen"],
                    f"{claim['natural_key']} 误机赔付批次结算 v{updated['version']}")
                self.repo.insert_voucher(db, claim["claim_id"], voucher_no, spec)
                settled.append({"claim_id": claim["claim_id"], "receipt_no": f"RCP-{voucher_no}",
                                "voucher_no": voucher_no, "paid_fen": claim["due_fen"]})
            response = {"batch_id": batch_id, "settled": settled, "skipped": skipped,
                        "settled_count": len(settled),
                        "paid_total_fen": sum(item["paid_fen"] for item in settled)}
            _commit_idempotent(tx, db, request_key, "SETTLE_BATCH", request_payload, None, response)
            return response
        except Exception as exc:
            _rollback(tx, db if tx is not None else None)
            if isinstance(exc, (ValueError, LookupError)):
                raise _as_claim_error(exc) from exc
            raise

    # ---------- 撤回（反向分录） ----------

    def withdraw(self, payload: dict, request_key: str | None = None) -> dict:
        claim_id = str(payload.get("claim_id", "")).strip()
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or isinstance(expected_version, bool):
            raise ClaimError("VERSION_REQUIRED", "撤回必须携带整数 expected_version")
        reason = str(payload.get("reason", "WITHDRAW")).strip() or "WITHDRAW"
        request_payload = {"claim_id": claim_id, "expected_version": expected_version, "reason": reason}
        tx = None
        try:
            db, tx, prior = _replay_or_start(self.repo, request_key, "WITHDRAW", request_payload)
            if prior is not None:
                return prior
            claim = self._load(db, claim_id)
            self._check_version(claim, expected_version)
            if claim["status"] == "WITHDRAWN":
                raise ClaimError("CLAIM_WITHDRAWN", "索赔已撤回，重复撤回拒绝", status=409)
            if claim["status"] == "SETTLED":
                # 已结算撤回：原凭证保留；若仍有净入账则追加反向分录，已全额追偿则直接改状态。
                net_before = self._net_posted(db, claim_id)
                updated = self.repo.mark_withdrawn(db, claim_id, expected_version)
                if net_before <= 0:
                    response = {"claim_id": claim_id, "action": "CANCELLED_NO_NET",
                                "version": updated["version"], "status": "WITHDRAWN",
                                "vouchers": []}
                else:
                    first_voucher = db.execute(
                        "SELECT voucher_no FROM vouchers WHERE claim_id = ? ORDER BY created_at, voucher_no LIMIT 1",
                        (claim_id,)).fetchone()
                    voucher_no = self.repo.next_voucher_no(db, claim_id)
                    spec = withdrawal_voucher(
                        net_before, f"{claim['natural_key']} 撤回反向分录 v{updated['version']}",
                        first_voucher["voucher_no"])
                    self.repo.insert_voucher(db, claim_id, voucher_no, spec)
                    response = {"claim_id": claim_id, "action": "REVERSED",
                                "receipt_no": f"RCP-{voucher_no}", "voucher_no": voucher_no,
                                "reversed_fen": net_before, "version": updated["version"],
                                "status": "WITHDRAWN"}
            else:
                updated = self.repo.mark_withdrawn(db, claim_id, expected_version)
                response = {"claim_id": claim_id, "action": "CANCELLED_OPEN",
                            "version": updated["version"], "status": "WITHDRAWN", "vouchers": []}
            _commit_idempotent(tx, db, request_key, "WITHDRAW", request_payload, claim_id, response)
            return response
        except Exception as exc:
            _rollback(tx, db if tx is not None else None)
            if isinstance(exc, (ValueError, LookupError)):
                raise _as_claim_error(exc) from exc
            raise

    # ---------- 回读 ----------

    def readback(self, claim_id: str) -> dict:
        try:
            claim = self.repo.get_claim(claim_id)
        except LookupError as exc:
            raise _as_claim_error(exc) from exc
        chain = self.repo.list_voucher_chain(claim_id)
        net_posted = signed_expense_fen(
            [entry for voucher in chain for entry in voucher["entries"]])
        pending = claim["due_fen"] - net_posted if claim["status"] != "WITHDRAWN" else 0
        return {
            "claim_id": claim["claim_id"],
            "natural_key": claim["natural_key"],
            "itinerary_id": claim["itinerary_id"],
            "status": claim["status"],
            "version": claim["version"],
            "delay_minutes": claim["delay_minutes"],
            "due_fen": claim["due_fen"],                       # 应赔
            "net_posted_fen": net_posted,                     # 净入账
            "pending_difference_fen": pending,                # 待处理差额
            "evidence_complete": claim["evidence_complete"],
            "denial_reason": claim["denial_reason"],
            "point": claim["point"],
            "itinerary": claim["itinerary"],
            "evidence": claim["evidence"],
            "revisions": self.repo.list_revisions(claim_id),
            "voucher_chain": chain,
        }

    def list_claims(self) -> list[dict]:
        return self.repo.list_claims()

    # ---------- 内部辅助 ----------

    def _load(self, db, claim_id: str) -> dict:
        row = db.execute("SELECT * FROM claims WHERE claim_id = ?", (claim_id,)).fetchone()
        if row is None:
            raise ClaimError("CLAIM_NOT_FOUND", f"索赔不存在: {claim_id}", status=404)
        return self.repo._claim_dict(row)

    def _check_version(self, claim: dict, expected_version: int) -> None:
        if claim["version"] != expected_version:
            raise ClaimError("STALE_VERSION",
                            f"版本过期：当前 v{claim['version']}，提交基于 v{expected_version}",
                            status=409)

    def _guard_identity(self, claim: dict, itinerary: Itinerary) -> None:
        """修订不得改变身份（行程与失接点的机场/前后航班），只允许事实/证据内容变化。

        失接状态本身允许变化（修订后可能恢复正常衔接），但相邻航段身份必须保留。
        """
        if claim["itinerary_id"] != itinerary.itinerary_id:
            raise ClaimError("IDENTITY_MISMATCH", "修订不得变更行程标识", status=409)
        target = claim["point"]
        for prev, nxt in zip(itinerary.segments, itinerary.segments[1:]):
            if (prev.destination == target["airport"]
                    and prev.flight_no == target["inbound_flight"]
                    and nxt.flight_no == target["outbound_flight"]):
                return
        raise ClaimError("IDENTITY_MISMATCH", "修订后的失接点与原索赔不一致", status=409)

    def _match_point(self, claim: dict, points: list[dict]) -> dict:
        """从失接点列表中定位原索赔对应点；若已恢复正常衔接则合成 VALID 点。"""
        target = claim["point"]
        for point in points:
            if (point["airport"] == target["airport"]
                    and point["inbound_flight"] == target["inbound_flight"]
                    and point["outbound_flight"] == target["outbound_flight"]):
                return point
        return {**target, "status": "VALID"}

    def _net_posted(self, db, claim_id: str) -> int:
        rows = db.execute(
            """SELECT e.account, e.direction, e.amount_fen FROM voucher_entries e
               JOIN vouchers v ON v.voucher_no = e.voucher_no WHERE v.claim_id = ?""",
            (claim_id,)).fetchall()
        return signed_expense_fen(rows)

    def _post_settlement_adjustment(self, db, before: dict, after: dict, reason: str) -> list[dict]:
        """已结算单按新事实差额生成关联补赔/追偿，原凭证保持原样。"""
        old_due, new_due = before["due_fen"], after["due_fen"]
        if old_due == new_due:
            return []
        first = db.execute(
            "SELECT voucher_no FROM vouchers WHERE claim_id = ? ORDER BY created_at, voucher_no LIMIT 1",
            (before["claim_id"],)).fetchone()
        linked = first["voucher_no"] if first else None
        voucher_no = self.repo.next_voucher_no(db, before["claim_id"])
        if new_due > old_due:
            spec = supplement_voucher(
                new_due - old_due,
                f"{before['natural_key']} 证据修订补赔 v{after['version']} ({reason})", linked)
        else:
            spec = recovery_voucher(
                old_due - new_due,
                f"{before['natural_key']} 证据修订追偿 v{after['version']} ({reason})", linked)
        self.repo.insert_voucher(db, before["claim_id"], voucher_no, spec)
        return [{"voucher_no": voucher_no, "voucher_type": spec.voucher_type,
                 "amount_fen": spec.entries[0].amount_fen, "linked_voucher_no": linked}]
