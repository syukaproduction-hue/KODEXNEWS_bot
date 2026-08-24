"""Sector taxonomy, vote records, and public aggregation."""

from dataclasses import dataclass

SECTORS = (
    "반도체",
    "2차전지",
    "자동차",
    "바이오·헬스케어",
    "금융",
    "조선·방산",
    "에너지·화학",
    "인터넷·게임",
    "소비재",
    "건설·리츠",
    "AI·로봇",
    "기타",
)

_ALIAS_RULES = (
    (("hbm", "반도체", "메모리", "파운드리", "디램", "dram", "낸드"), "반도체"),
    (("2차전지", "이차전지", "배터리", "양극재", "음극재"), "2차전지"),
    (("방산", "방위산업", "조선"), "조선·방산"),
    (("자동차", "완성차", "전기차"), "자동차"),
    (("바이오", "헬스케어", "제약"), "바이오·헬스케어"),
    (("은행", "보험", "증권", "금융"), "금융"),
    (("에너지", "정유", "화학", "유가"), "에너지·화학"),
    (("인터넷", "플랫폼", "게임"), "인터넷·게임"),
    (("소비재", "유통", "화장품", "식품"), "소비재"),
    (("건설", "리츠", "부동산"), "건설·리츠"),
    (("인공지능", "ai", "로봇"), "AI·로봇"),
)


def normalize_sector(value: str) -> str:
    """Map free-form sector text to the fixed public taxonomy."""
    text = (value or "").strip().lower()
    for aliases, sector in _ALIAS_RULES:
        if any(alias in text for alias in aliases):
            return sector
    return "기타"


@dataclass(frozen=True)
class SectorCall:
    channel: str
    sector: str
    direction: str
    horizon: str
    published_at: str


def aggregate_calls(calls: list[SectorCall]) -> list[dict]:
    """Create sector-only consensus with one vote per channel and sector."""
    latest: dict[tuple[str, str], SectorCall] = {}
    for call in calls:
        if call.horizon != "next_session":
            continue
        sector = normalize_sector(call.sector)
        direction = call.direction if call.direction in {"up", "down", "neutral"} else "neutral"
        normalized = SectorCall(call.channel, sector, direction, call.horizon, call.published_at)
        key = (call.channel, sector)
        if key not in latest or normalized.published_at >= latest[key].published_at:
            latest[key] = normalized

    counts: dict[str, dict[str, int]] = {}
    for call in latest.values():
        bucket = counts.setdefault(call.sector, {"up": 0, "down": 0, "neutral": 0})
        bucket[call.direction] += 1

    result = []
    for sector, bucket in counts.items():
        if bucket["up"] > bucket["down"] and bucket["up"] > bucket["neutral"]:
            consensus = "up"
        elif bucket["down"] > bucket["up"] and bucket["down"] > bucket["neutral"]:
            consensus = "down"
        else:
            consensus = "neutral"
        result.append({
            "sector": sector,
            **bucket,
            "total": sum(bucket.values()),
            "consensus": consensus,
        })
    return sorted(result, key=lambda row: (-row["total"], SECTORS.index(row["sector"])))
