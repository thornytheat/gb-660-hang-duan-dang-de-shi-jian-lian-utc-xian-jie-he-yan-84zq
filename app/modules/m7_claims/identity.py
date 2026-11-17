"""索赔身份：以行程标识 + 失接点（衔接机场与前后航班）识别同一索赔。

同一身份跨批次重复登记只返回原索赔，绝不产生第二笔赔款。
"""


def natural_key(itinerary_id: str, point: dict) -> str:
    return "|".join((
        str(itinerary_id).strip(),
            point["airport"],
            point["inbound_flight"],
            point["outbound_flight"],
    ))
