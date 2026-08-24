"""LLM prompt and strict parser for sector calls."""

import json
import re

from sector_vote.sector_logic import SECTORS, normalize_sector

VALID_DIRECTIONS = {"up", "down", "neutral"}
VALID_HORIZONS = {"next_session", "short_term", "long_term", "unclear"}


def build_prompt(transcript: str) -> str:
    sectors = ", ".join(SECTORS)
    return f"""아래 경제·금융 유튜브 스크립트에서 섹터 전망만 추출하세요.

고정 섹터: {sectors}

판정 규칙:
- 다음 거래일 또는 아주 가까운 장세에 대한 명시적 방향만 horizon=next_session.
- 며칠~수주 전망은 short_term, 구조적·장기 전망은 long_term, 불명확하면 unclear.
- 장기 전망은 제외 대상이므로 next_session으로 바꾸지 마세요.
- 개별 종목 매수·매도, ETF·상품 추천은 추출하지 마세요.
- 화자가 가능성만 나열하거나 타인의 주장을 인용하면 neutral 또는 제외하세요.
- direction은 up, down, neutral 중 하나만 사용하세요.
- quote는 원문에서 120자 이내로 짧게 인용하고, reason은 80자 이내로 요약하세요.
- 전망이 없으면 calls를 빈 배열로 반환하세요.

JSON만 반환:
{{"calls":[{{"sector":"반도체","direction":"up","horizon":"next_session","confidence":0.8,"reason":"...","quote":"..."}}]}}

스크립트:
{transcript[:30000]}"""


def parse_calls(raw: str) -> list[dict]:
    text = (raw or "").strip()
    text = re.sub(
        r"^```(?:json)?\s*|\s*```$", "", text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    data = json.loads(text)
    rows = data.get("calls", []) if isinstance(data, dict) else []
    parsed = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        direction = row.get("direction")
        horizon = row.get("horizon")
        if direction not in VALID_DIRECTIONS or horizon not in VALID_HORIZONS:
            continue
        try:
            confidence = max(0.0, min(1.0, float(row.get("confidence", 0))))
        except (TypeError, ValueError):
            confidence = 0.0
        parsed.append({
            "sector": normalize_sector(str(row.get("sector", ""))),
            "direction": direction,
            "horizon": horizon,
            "confidence": confidence,
            "reason": str(row.get("reason", "")).strip()[:160],
            "quote": str(row.get("quote", "")).strip()[:120],
        })
    return parsed


def classify_transcript(transcript: str, api_key: str, model: str = "claude-sonnet-4-6") -> list[dict]:
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is required")
    from anthropic import Anthropic

    client = Anthropic(api_key=api_key)
    message = client.messages.create(
        model=model,
        max_tokens=1800,
        temperature=0,
        messages=[{"role": "user", "content": build_prompt(transcript)}],
    )
    raw = "".join(block.text for block in message.content if getattr(block, "type", "") == "text")
    return parse_calls(raw)
