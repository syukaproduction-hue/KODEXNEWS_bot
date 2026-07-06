"""
공개 시장 데이터 조회 (네이버금융 기반)
- settings.FOCUS_PRODUCTS(집중 상품)의 전일 종가·등락률·거래량을 읽어,
  '어제 눈에 띈 상품'을 오전 브리핑에 참고 제안으로 넣기 위한 모듈.
- 네이버금융의 (미공개) 엔드포인트를 사용한다. bot.py의 fetch_kospi_close와 같은 방식이다.
- 원칙: 값을 못 가져오면 절대 추측하지 않는다. 해당 상품을 그냥 빼거나 전체를 None으로 반환한다.
  (컴플라이언스: 불확실한 수치는 브리핑에 넣지 않는다.)
"""

import requests

import settings

HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://m.stock.naver.com/"}


def _to_float(v):
    try:
        return float(str(v).replace(",", "").replace("%", ""))
    except Exception:
        return None


def _fetch_one(code: str):
    """종목코드 하나의 종가·등락률·거래량을 dict로 반환. 실패하면 None."""
    endpoints = [
        f"https://polling.finance.naver.com/api/realtime/domestic/stock/{code}",
        f"https://m.stock.naver.com/api/stock/{code}/basic",
        f"https://m.stock.naver.com/api/stock/{code}/integration",
    ]
    for url in endpoints:
        try:
            r = requests.get(url, headers=HEADERS, timeout=8)
            if r.status_code != 200:
                continue
            data = r.json()
            # 엔드포인트마다 구조가 달라 모두 대응 (index 조회와 동일한 방식)
            node = data
            if isinstance(data, dict) and "datas" in data and data["datas"]:
                node = data["datas"][0]
            if not isinstance(node, dict):
                continue
            close = node.get("closePrice") or node.get("nv") or node.get("now")
            rate = node.get("fluctuationsRatio") or node.get("cr")
            vol = (node.get("accumulatedTradingVolume")
                   or node.get("aq") or node.get("acml_vol"))
            rate_f = _to_float(rate)
            if close is None or rate_f is None:
                continue
            close_f = _to_float(close)
            close_s = f"{close_f:,.0f}" if close_f is not None else str(close)
            vol_f = _to_float(vol) if vol is not None else None
            vol_s = f"{int(vol_f):,}" if vol_f is not None else None
            return {"rate": rate_f, "close": close_s, "vol": vol_s}
        except Exception:
            continue
    return None


def notable_focus_products():
    """
    FOCUS_PRODUCTS 각 상품의 전일 등락률·거래량을 조회해
    등락률 절댓값이 큰 순으로 정렬한 참고 문장(여러 줄)을 반환한다.
    하나도 못 가져오면 None.
    """
    rows = []
    for p in settings.FOCUS_PRODUCTS:
        d = _fetch_one(p["code"])
        if not d:
            continue
        rows.append((p["name"], p["code"], d))
    if not rows:
        return None
    rows.sort(key=lambda x: abs(x[2]["rate"]), reverse=True)
    lines = []
    for name, code, d in rows:
        sign = "+" if d["rate"] > 0 else ""
        vol_part = f", 거래량 {d['vol']}주" if d.get("vol") else ""
        lines.append(f"- {name}({code}): 전일 종가 {d['close']}, "
                     f"등락률 {sign}{d['rate']:.2f}%{vol_part}")
    return "\n".join(lines)
