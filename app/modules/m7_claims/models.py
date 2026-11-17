"""航段与行程领域模型。

时间一律按带偏移的 ISO 8601 解析，仅做带偏移的 UTC 换算用于先后比较，
不修改任何时区规则、不解释 DST（沿用输入自带的偏移量）。
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .mct import minimum_connect_minutes


def parse_iso(value: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("INVALID_TIME")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("INVALID_TIME") from exc
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("NAIVE_TIME_REJECTED")
    return moment.astimezone(timezone.utc)


@dataclass(frozen=True)
class Segment:
    flight_no: str
    origin: str
    destination: str
    scheduled_dep: datetime          # UTC
    scheduled_arr: datetime          # UTC
    actual_dep: datetime | None = None
    actual_arr: datetime | None = None

    @classmethod
    def from_payload(cls, row: dict) -> "Segment":
        try:
            flight_no = str(row["flight_no"]).strip().upper()
            origin = str(row["origin"]).strip().upper()
            destination = str(row["destination"]).strip().upper()
        except (KeyError, AttributeError, TypeError) as exc:
            raise ValueError("MISSING_SEGMENT_FIELD") from exc
        if not flight_no or not origin or not destination:
            raise ValueError("MISSING_SEGMENT_FIELD")
        scheduled_dep = parse_iso(row["scheduled_dep"])
        scheduled_arr = parse_iso(row["scheduled_arr"])
        if scheduled_arr <= scheduled_dep:
            raise ValueError("INVALID_FLIGHT_DURATION")
        actual_dep = parse_iso(row["actual_dep"]) if row.get("actual_dep") else None
        actual_arr = parse_iso(row["actual_arr"]) if row.get("actual_arr") else None
        return cls(flight_no, origin, destination, scheduled_dep, scheduled_arr, actual_dep, actual_arr)

    def to_payload(self) -> dict:
        return {
            "flight_no": self.flight_no,
            "origin": self.origin,
            "destination": self.destination,
            "scheduled_dep": self.scheduled_dep.isoformat(),
            "scheduled_arr": self.scheduled_arr.isoformat(),
            "actual_dep": self.actual_dep.isoformat() if self.actual_dep else None,
            "actual_arr": self.actual_arr.isoformat() if self.actual_arr else None,
        }


@dataclass
class Itinerary:
    itinerary_id: str
    segments: list[Segment] = field(default_factory=list)

    @classmethod
    def from_payload(cls, payload: dict) -> "Itinerary":
        itinerary_id = str(payload.get("itinerary_id", "")).strip()
        if not itinerary_id:
            raise ValueError("MISSING_ITINERARY_ID")
        rows = payload.get("segments")
        if not isinstance(rows, list) or len(rows) < 2:
            raise ValueError("ITINERARY_NEEDS_TWO_SEGMENTS")
        segments = [Segment.from_payload(row) for row in rows]
        itinerary = cls(itinerary_id, segments)
        itinerary.validate_chain()
        return itinerary

    def validate_chain(self) -> None:
        """校验航链：按计划起飞 UTC 排序后，相邻航段必须首尾同机场且时间不倒挂。"""
        ordered = sorted(self.segments, key=lambda seg: (seg.scheduled_dep, seg.flight_no))
        for prev, nxt in zip(ordered, ordered[1:]):
            if prev.destination != nxt.origin:
                raise ValueError("CHAIN_AIRPORT_MISMATCH")
            if nxt.scheduled_dep < prev.scheduled_arr:
                raise ValueError("CHAIN_TIME_OVERLAP")
        self.segments = ordered

    def missed_points(self) -> list[dict]:
        """识别失接点：实际到达晚于下一段计划起飞，或实际衔接时间不足 MCT。

        衔接等待使用半开区间语义：等待分钟 >= MCT 即视为正常衔接。
        返回失接点描述列表，正常衔接不产生记录。
        """
        points: list[dict] = []
        for index, (prev, nxt) in enumerate(zip(self.segments, self.segments[1:]), start=1):
            airport = prev.destination
            prev_arr = prev.actual_arr or prev.scheduled_arr
            nxt_dep = nxt.actual_dep or nxt.scheduled_dep
            wait_minutes = int((nxt_dep - prev_arr).total_seconds() // 60)
            mct = minimum_connect_minutes(airport)
            if mct is None:
                raise ValueError("UNKNOWN_AIRPORT")
            if wait_minutes < 0:
                status = "OVERLAP"
            elif wait_minutes < mct:
                status = "TOO_SHORT"
            else:
                status = "VALID"
            if status != "VALID":
                points.append({
                    "point_index": index,
                    "airport": airport,
                    "inbound_flight": prev.flight_no,
                    "outbound_flight": nxt.flight_no,
                    "wait_minutes": wait_minutes,
                    "mct_minutes": mct,
                    "status": status,
                })
        return points

    def final_delay_minutes(self) -> int:
        """终到延误 = 末段实际到达 - 末段计划到达（分钟，负值归零）。"""
        last = self.segments[-1]
        actual_arr = last.actual_arr
        if actual_arr is None:
            return 0
        delta = int((actual_arr - last.scheduled_arr).total_seconds() // 60)
        return max(delta, 0)

    def to_payload(self) -> dict:
        return {"itinerary_id": self.itinerary_id, "segments": [seg.to_payload() for seg in self.segments]}
