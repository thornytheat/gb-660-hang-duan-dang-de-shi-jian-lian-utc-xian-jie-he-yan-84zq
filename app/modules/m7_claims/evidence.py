"""证据包：航司补发的误机证明与后续航段凭证。

证据不全（缺类、缺编号或缺时间）的索赔只能拒付，不允许结算。
"""
from dataclasses import dataclass

REQUIRED_DOC_TYPES = ("AIRLINE_DELAY_CERT", "ONWARD_BOARDING_DOC")


@dataclass(frozen=True)
class Document:
    doc_type: str
    doc_no: str
    issued_at: str          # 原始 ISO 字符串，原样留存
    note: str = ""

    @classmethod
    def from_payload(cls, row: dict) -> "Document":
        try:
            doc_type = str(row["doc_type"]).strip().upper()
            doc_no = str(row["doc_no"]).strip()
            issued_at = str(row["issued_at"]).strip()
        except (KeyError, AttributeError, TypeError) as exc:
            raise ValueError("MALFORMED_DOCUMENT") from exc
        if not doc_type or not doc_no or not issued_at:
            raise ValueError("MALFORMED_DOCUMENT")
        note = str(row.get("note", "")).strip()
        return cls(doc_type, doc_no, issued_at, note)

    def to_payload(self) -> dict:
        return {"doc_type": self.doc_type, "doc_no": self.doc_no, "issued_at": self.issued_at, "note": self.note}


@dataclass(frozen=True)
class EvidencePacket:
    documents: tuple[Document, ...]

    @classmethod
    def from_payload(cls, payload) -> "EvidencePacket":
        if not isinstance(payload, list):
            raise ValueError("EVIDENCE_MUST_BE_LIST")
        docs = tuple(Document.from_payload(row) for row in payload)
        return cls(docs)

    def to_payload(self) -> list[dict]:
        return [doc.to_payload() for doc in self.documents]

    def missing_types(self) -> list[str]:
        present = {doc.doc_type for doc in self.documents}
        return [doc_type for doc_type in REQUIRED_DOC_TYPES if doc_type not in present]

    def is_complete(self) -> bool:
        return not self.missing_types()
