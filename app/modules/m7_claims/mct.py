"""机场最短衔接时间（MCT，分钟）。

仅查表，不引入任何时区规则。未知机场返回 None，由上层拒绝登记。
"""
MINIMUM_CONNECT_MINUTES: dict[str, int] = {
    "PVG": 60,
    "JFK": 90,
    "LHR": 75,
    "SFO": 80,
    "PEK": 70,
    "CAN": 65,
}


def minimum_connect_minutes(airport: str) -> int | None:
    return MINIMUM_CONNECT_MINUTES.get(airport.upper())
