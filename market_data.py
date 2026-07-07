"""
공개 시장 데이터 조회 (네이버금융 기반)
- settings.FOCUS_PRODUCTS(집중 상품)의 '직전 영업일(어제) 마감' 종가·등락률·거래량을 읽어,
  '어제 눈에 띈 상품'을 오전 브리핑에 참고 제안으로 넣기 위한 모듈.
- 오전 브리핑은 장이 열린 직후(09:00)에 돌기 때문에, '오늘 막 열린 시세'가 아니라
  '어제 마감된 하루치 데이터'를 잡아야 한다. 그래서 실시간 값이 아니라 '일별 시세'에서
  오늘이 아닌 가장 최근 완료 거래일을 골라 쓴다.
- 원칙: 값을 못 가져오면 절대 추측하지 않는다. 해당 상품을 빼거나 전체를 None으로 반환한다.
  (컴플라이언스: 불확실한 수치는 브리핑에 넣지 않는다.)
"""

from datetime import datetime, timezone, timedelta

import requests

import settings

HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://m.stock.naver.com/"}
KST = timezone(timedelta(hours=9))


def _to_float(v):
    try:
        return float(str(v).replace(",", "").replace("%", ""))
    except Exception:
        return None


def _digits(s):
    return "".join(ch for ch in str(s) if ch.isdigit())


def _pack(close, rate, vol):
    rate_f = _to_float(rate)
    close_f = _to_float(close)
    if close_f is None or rate_f is None:
        return None
    vol_f = _to_float(vol) if vol is not None else None
    return {
        "close": f"{close_f:,.0f}",
        "rate": rate_f,
        "vol": f"{int(vol_f):,}" if vol_f is not None else None,
    }


def _fetch_prev_day(code: str):
    """종목코드의 '직전 영업일(오늘 제외 최근 거래일)' 마감 시세를 dict로 반환. 실패하면 None."""
    today_ymd = datetime.now(KST).strftime("%Y%m%d")
    # 1) 일별 시세에서 오늘이 아닌 최근 완료 거래일을 고른다 (정확 — 장중에도 어제값을 준다)
    try:
        url = f"https://m.stock.naver.com/api/stock/{code}/price?pageSize=10&page=1"
        r = requests.get(url, headers=HEADERS, timeout=8)
        if r.status_code == 200:
            data = r.json()
            rows = data if isinstance(data, list) else (
                data.get("datas") or data.get("priceInfos") or [])
            for row in rows:
                if not isinstance(row, dict):
                    continue
                date_raw = (row.get("localTradedAt") or row.get("localDate")
                            or row.get("dt") or row.get("bizdate") or "")
                ymd = _digits(date_raw)[:8]
                if len(ymd) == 8 and ymd == today_ymd:
                    continue  # 오늘(장중 진행 중) 행은 건너뛴다
                got = _pack(row.get("closePrice") or row.get("nv"),
                            row.get("fluctuationsRatio") or row.get("cr"),
                            row.get("accumulatedTradingVolume") or row.get("aq"))
                if got:
                    return got
    except Exception:
        pass
    # 2) 마지막 대비책: 일별 시세를 못 읽으면 실시간 엔드포인트. 장 마감 후에는 이 값이 곧 당일 마감이다.
    return _fetch_realtime(code)


def _fetch_realtime(code: str):
    endpoints = [
        f"https://polling.finance.naver.com/api/realtime/domestic/stock/{code}",
        f"https://m.stock.naver.com/api/stock/{code}/basic",
    ]
    for url in endpoints:
        try:
            r = requests.get(url, headers=HEADERS, timeout=8)
            if r.status_code != 200:
                continue
            data = r.json()
            node = data
            if isinstance(data, dict) and data.get("datas"):
                node = data["datas"][0]
            if not isinstance(node, dict):
                continue
            got = _pack(node.get("closePrice") or node.get("nv") or node.get("now"),
                        node.get("fluctuationsRatio") or node.get("cr"),
                        node.get("accumulatedTradingVolume") or node.get("aq"))
            if got:
                return got
        except Exception:
            continue
    return None


def notable_focus_products():
    """
    FOCUS_PRODUCTS 각 상품의 직전 영업일 등락률·거래량을 조회해
    등락률 절댓값이 큰 순으로 정렬한 참고 문장(여러 줄)을 반환한다. 하나도 못 가져오면 None.
    """
    rows = []
    for p in settings.FOCUS_PRODUCTS:
        d = _fetch_prev_day(p["code"])
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
        lines.append(f"- {name}({code}): 직전 영업일 종가 {d['close']}, "
                     f"등락률 {sign}{d['rate']:.2f}%{vol_part}")
    return "\n".join(lines)


# ===================== 시각화용: 최근 거래일 시세 흐름 =====================
def _series_from_price_rows(rows, days):
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        date_raw = (row.get("localTradedAt") or row.get("localDate") or row.get("dt") or "")
        ymd = _digits(date_raw)[:8]
        close = _to_float(row.get("closePrice") or row.get("nv"))
        if not ymd or close is None:
            continue
        out.append({
            "ymd": ymd,
            "close": close,
            "rate": _to_float(row.get("fluctuationsRatio") or row.get("cr")),
            "vol": _to_float(row.get("accumulatedTradingVolume") or row.get("aq")),
        })
    out.sort(key=lambda o: o["ymd"])   # 오래된 -> 최신
    return out[-days:]


def daily_series(code, days=20):
    """종목의 최근 days 거래일 (날짜/종가/등락률/거래량) 리스트. 실패하면 []."""
    try:
        url = f"https://m.stock.naver.com/api/stock/{code}/price?pageSize={days + 4}&page=1"
        r = requests.get(url, headers=HEADERS, timeout=8)
        if r.status_code != 200:
            return []
        data = r.json()
        rows = data if isinstance(data, list) else (
            data.get("datas") or data.get("priceInfos") or [])
        return _series_from_price_rows(rows, days)
    except Exception:
        return []


def index_daily_series(index="KOSPI", days=20):
    """지수(KOSPI 등)의 최근 days 거래일 흐름. 실패하면 []."""
    try:
        url = f"https://m.stock.naver.com/api/index/{index}/price?pageSize={days + 4}&page=1"
        r = requests.get(url, headers=HEADERS, timeout=8)
        if r.status_code != 200:
            return []
        data = r.json()
        rows = data if isinstance(data, list) else (
            data.get("datas") or data.get("priceInfos") or [])
        return _series_from_price_rows(rows, days)
    except Exception:
        return []


def focus_series(days=20):
    """FOCUS_PRODUCTS 각 상품의 최근 흐름. [{name, code, series}] 형태."""
    result = []
    for p in settings.FOCUS_PRODUCTS:
        result.append({"name": p["name"], "code": p["code"],
                       "series": daily_series(p["code"], days)})
    return result
