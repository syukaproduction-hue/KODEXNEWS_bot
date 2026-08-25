"""
소파 이기기 — 실제 과거 시세 수집 스크립트

이 파일을 실행하면 네이버금융 공개 데이터에서 일별 시세를 받아
sofa_game.html 과 같은 폴더에 market_history.json 을 만듭니다.
그 파일이 생기면 게임이 자동으로 실제 데이터로 바뀌고,
'연습용 가상 데이터' 경고 띠도 사라집니다.

실행 방법 (Railway Shell 또는 파이썬이 깔린 PC)
    python fetch_history.py

주의
  · 이 샌드박스에서는 네이버 접속이 막혀 있어 실행할 수 없습니다.
    Railway 서버나 사무실 PC에서 돌려 주세요.
  · 다섯 자산의 '거래일'이 모두 겹치는 날짜만 남깁니다.
    (게임이 다섯 자산을 같은 날짜 축으로 비교하기 때문입니다)
"""

import json
import time
from pathlib import Path

import requests

HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://m.stock.naver.com/"}
YEARS = 17          # 2010년부터 받도록 넉넉히
PAGE_SIZE = 100     # 한 번에 받을 일수
OUT = Path(__file__).parent / "market_history.json"

# ---------------------------------------------------------------------
# 채권 설정
#   "yield:3.2"  → 연 3.2% 로 완만히 오르는 안전자산으로 합성합니다.
#                  상품명이 전혀 등장하지 않아 준법 부담이 없습니다. (기본값)
#   "code:XXXXXX" → 채권 ETF 종목코드를 넣으면 그 실제 가격을 씁니다.
#                  이 경우 특정 상품이 화면에 반영되므로,
#                  반드시 준법 확인을 먼저 받으세요.
# ---------------------------------------------------------------------
BOND_SOURCE = "yield:3.2"

ASSETS = [
    {"id": "kospi",   "kind": "index", "key": "KOSPI"},
    {"id": "kosdaq",  "kind": "index", "key": "KOSDAQ"},
    {"id": "samsung", "kind": "stock", "key": "005930"},   # 삼성전자
    {"id": "hynix",   "kind": "stock", "key": "000660"},   # SK하이닉스
]


def _digits(v):
    return "".join(c for c in str(v) if c.isdigit())


def _rows_from(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return payload.get("datas") or payload.get("priceInfos") or []
    return []


def fetch_series(kind, key, days):
    """{'YYYYMMDD': 종가} 딕셔너리로 반환."""
    base = ("https://m.stock.naver.com/api/index/%s/price" % key) if kind == "index" \
        else ("https://m.stock.naver.com/api/stock/%s/price" % key)
    out, page = {}, 1
    while len(out) < days and page <= (days // PAGE_SIZE) + 4:
        url = "%s?pageSize=%d&page=%d" % (base, PAGE_SIZE, page)
        try:
            r = requests.get(url, headers=HEADERS, timeout=12)
            if r.status_code != 200:
                print("   ! HTTP %s (page %d) — 중단" % (r.status_code, page))
                break
            rows = _rows_from(r.json())
        except Exception as e:
            print("   ! 조회 실패 (page %d): %s" % (page, e))
            break
        if not rows:
            break
        added = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            ymd = _digits(row.get("localTradedAt") or row.get("localDate") or row.get("dt") or "")[:8]
            raw = row.get("closePrice") or row.get("nv")
            if len(ymd) != 8 or raw in (None, ""):
                continue
            try:
                out[ymd] = float(str(raw).replace(",", ""))
                added += 1
            except ValueError:
                continue
        if added == 0:
            break
        page += 1
        time.sleep(0.25)   # 과도한 요청 방지
    return out


def main():
    days = YEARS * 245
    series = {}

    for a in ASSETS:
        print("· %s (%s) 수집 중…" % (a["id"], a["key"]))
        s = fetch_series(a["kind"], a["key"], days)
        print("   %d 거래일" % len(s))
        if len(s) < 300:
            print("   ! 데이터가 너무 적습니다. 종목코드와 네트워크를 확인하세요.")
            return
        series[a["id"]] = s

    # 채권
    if BOND_SOURCE.startswith("code:"):
        code = BOND_SOURCE.split(":", 1)[1].strip()
        print("· bond (%s) 수집 중… (상품 반영 — 준법 확인 필요)" % code)
        series["bond"] = fetch_series("stock", code, days)
        print("   %d 거래일" % len(series["bond"]))
    else:
        apr = float(BOND_SOURCE.split(":", 1)[1])
        print("· bond 합성 (연 %.2f%%, 상품명 없음)" % apr)
        series["bond"] = None   # 날짜 축이 정해진 뒤에 채운다

    # 다섯 자산이 모두 겹치는 날짜만 남긴다
    keyed = [set(v.keys()) for v in series.values() if v]
    dates = sorted(set.intersection(*keyed))
    if len(dates) < 300:
        print("공통 거래일이 %d일뿐입니다. 수집 범위를 확인하세요." % len(dates))
        return

    if series["bond"] is None:
        apr = float(BOND_SOURCE.split(":", 1)[1])
        step = (1.0 + apr / 100.0) ** (1.0 / 245.0)
        series["bond"] = {d: 100.0 * (step ** i) for i, d in enumerate(dates)}

    out = {
        "updated": dates[-1][:4] + "-" + dates[-1][4:6] + "-" + dates[-1][6:],
        "source": "네이버금융 공개 데이터"
                  + ("" if BOND_SOURCE.startswith("code:") else " (채권은 연 %s%% 합성)"
                     % BOND_SOURCE.split(":", 1)[1]),
        "dates": dates,
        "series": {k: [round(series[k][d], 4) for d in dates] for k in series},
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    print("\n완료: %s" % OUT)
    print("  공통 거래일 %d일 (%s ~ %s)" % (len(dates), dates[0], dates[-1]))
    for k, v in out["series"].items():
        chg = (v[-1] / v[0] - 1) * 100
        print("  %-8s %8.2f → %8.2f  (%+.1f%%)" % (k, v[0], v[-1], chg))
    print("\n이 파일을 sofa_game.html 과 같은 폴더에 두면 게임이 실제 데이터로 바뀝니다.")


if __name__ == "__main__":
    main()
