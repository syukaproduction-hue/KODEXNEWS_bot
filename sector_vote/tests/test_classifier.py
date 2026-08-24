from sector_vote.classifier import build_prompt, parse_calls


def test_parse_calls_normalizes_and_filters_invalid_rows():
    raw = '''```json
    {"calls":[
      {"sector":"HBM", "direction":"up", "horizon":"next_session", "confidence":0.82,
       "reason":"메모리 가격 기대", "quote":"내일 반도체가 상대적으로 강할 수 있습니다"},
      {"sector":"은행", "direction":"buy", "horizon":"next_session", "confidence":0.9,
       "reason":"금리", "quote":"은행주를 사세요"}
    ]}
    ```'''

    calls = parse_calls(raw)

    assert calls == [{
        "sector": "반도체",
        "direction": "up",
        "horizon": "next_session",
        "confidence": 0.82,
        "reason": "메모리 가격 기대",
        "quote": "내일 반도체가 상대적으로 강할 수 있습니다",
    }]


def test_prompt_excludes_long_term_and_requires_explicit_sector_direction():
    prompt = build_prompt("오늘 시장 스크립트")

    assert "next_session" in prompt
    assert "장기 전망은 제외" in prompt
    assert "개별 종목" in prompt
    assert "오늘 시장 스크립트" in prompt
