"""
KODEX 시황 브리핑 — 통합 웹 (홈 / 텔레그램 봇 안내 / 아카이브 / 제작 브리프 / 완성 스크립트 / 데이터)
- bot.py와 같은 SQLite DB를 공유한다. DB 경로와 생성 함수(brief/script)를 configure()로 주입받는다.
- 공개 페이지지만 noindex/robots로 검색엔진 노출은 막는다.
"""

import re
import os
import html
import json
import time
import uuid
import hashlib
import sqlite3
import threading
from pathlib import Path
from urllib.parse import quote
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, Request
from fastapi.responses import (
    HTMLResponse, PlainTextResponse, JSONResponse, RedirectResponse, Response,
)

import market_data
import settings

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

DB_PATH = None
PLAN_FN = None
SCRIPT_FN = None
CHECK_FN = None
CAPTION_FN = None
BOT_LINK = "https://t.me/kodex_economy"
MAKER = "주식회사 슈카친구들"
KST = timezone(timedelta(hours=9))
WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]

# 접근 비밀번호(게이트). Railway에 WEB_PASSWORD를 넣으면 그 값이 우선, 없으면 기본 'KODEX'.
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "KODEX")
AUTH_TOKEN = hashlib.sha256(("kdx:" + WEB_PASSWORD).encode()).hexdigest()
AUTH_COOKIE = "kdx_auth"
GATE_EXEMPT = {"/login", "/login/auth", "/robots.txt", "/logo.svg",
               "/dividend", "/learn", "/survey", "/survey/vote"}

_BASE = Path(__file__).parent
LOGO_PATH = _BASE / "logo_kodex_ko.svg"
_LOGO_CACHE = None

JOBS = {}
LOCK = threading.Lock()


def configure(db_path, plan_fn=None, script_fn=None, check_fn=None, caption_fn=None):
    global DB_PATH, PLAN_FN, SCRIPT_FN, CHECK_FN, CAPTION_FN
    DB_PATH = str(db_path)
    PLAN_FN = plan_fn
    SCRIPT_FN = script_fn
    CHECK_FN = check_fn
    CAPTION_FN = caption_fn


# ================= DB =================
def _con():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS briefings(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, ymd TEXT,
        kind TEXT, source TEXT, title TEXT, body TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS plans(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, request TEXT, body TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS scripts(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, request TEXT, body TEXT, plan_id INTEGER,
        check_verdict TEXT, check_at TEXT, check_body TEXT, check_tags TEXT)""")
    for col, typ in (("plan_id", "INTEGER"), ("check_verdict", "TEXT"), ("check_at", "TEXT"),
                     ("check_body", "TEXT"), ("check_tags", "TEXT")):
        try:
            con.execute(f"ALTER TABLE scripts ADD COLUMN {col} {typ}")  # 기존 테이블 대비
        except Exception:
            pass
    con.execute("""CREATE TABLE IF NOT EXISTS product_news(
        code TEXT PRIMARY KEY, title TEXT, url TEXT, comp_name TEXT, comp_code TEXT, updated_at TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS survey_votes(
        qid TEXT, choice TEXT, ts TEXT)""")
    for col in ("comp_name", "comp_code"):
        try:
            con.execute(f"ALTER TABLE product_news ADD COLUMN {col} TEXT")  # 기존 테이블 대비
        except Exception:
            pass
    return con


def _rows(sql, args=()):
    if not DB_PATH:
        return []
    try:
        con = _con()
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()
    except Exception:
        return []


def list_briefings(limit=300):
    return _rows("SELECT id, ts, ymd, kind, source, title FROM briefings ORDER BY id DESC LIMIT ?", (limit,))


def get_briefing(bid):
    r = _rows("SELECT id, ts, ymd, kind, source, title, body FROM briefings WHERE id=?", (bid,))
    return r[0] if r else None


def list_plans(limit=50):
    return _rows("SELECT id, ts, request FROM plans ORDER BY id DESC LIMIT ?", (limit,))


def get_plan(pid):
    r = _rows("SELECT id, ts, request, body FROM plans WHERE id=?", (pid,))
    return r[0] if r else None


def get_script(sid):
    r = _rows("SELECT id, ts, request, body, plan_id, check_verdict, check_body, check_tags "
              "FROM scripts WHERE id=?", (sid,))
    return r[0] if r else None


def list_scripts(limit=50):
    return _rows("SELECT id, ts, request, check_verdict FROM scripts ORDER BY id DESC LIMIT ?", (limit,))


def scripts_for_plan(pid):
    return _rows("SELECT id, ts, request, check_verdict FROM scripts WHERE plan_id=? ORDER BY id DESC", (pid,))


def _week_cutoff_iso():
    return (datetime.now(KST) - timedelta(days=7)).isoformat()


def report_counts():
    cut = _week_cutoff_iso()

    def n(sql, args=()):
        r = _rows(sql, args)
        return (r[0][0] if r and r[0] and r[0][0] is not None else 0)

    verd = _rows("SELECT check_verdict, COUNT(*) FROM scripts "
                 "WHERE ts>=? AND check_verdict IS NOT NULL AND check_verdict<>'' GROUP BY check_verdict", (cut,))
    return {
        "am": n("SELECT COUNT(*) FROM briefings WHERE kind='am' AND ts>=?", (cut,)),
        "pm": n("SELECT COUNT(*) FROM briefings WHERE kind='pm' AND ts>=?", (cut,)),
        "plans": n("SELECT COUNT(*) FROM plans WHERE ts>=?", (cut,)),
        "scripts": n("SELECT COUNT(*) FROM scripts WHERE ts>=?", (cut,)),
        "checks": n("SELECT COUNT(*) FROM usage_log WHERE kind='check' AND ts>=?", (cut,)),
        "verdicts": {r[0]: r[1] for r in verd},
    }


def _extract_verdict(text):
    for line in (text or "").split("\n"):
        s = line.strip()
        if s.startswith("판정"):
            v = (s.split(":", 1)[1] if ":" in s else s.replace("판정", "")).strip()
            if "통과" in v:
                return "통과"
            if "수정" in v:
                return "수정 필요"
            if "주의" in v:
                return "주의"
    return ""


# 컴플라이언스에서 자주 문제되는 표현 유형 (최성락 팀장: 통과 사례를 유형별로 찾게)
COMP_TAGS = [
    ("수익률", ["수익률", "누적수익", "연평균"]),
    ("배당·월배당", ["배당", "월 배당", "월배당"]),
    ("분배율", ["분배", "분배율", "분배금"]),
    ("커버드콜", ["커버드콜", "커버드 콜"]),
    ("기준일", ["기준일", "상장 이후", "상장이후"]),
    ("원금·손실", ["원금", "손실"]),
    ("세금", ["세금", "세전", "세후", "비과세"]),
]


def _detect_tags(text):
    t = text or ""
    found = [name for name, kws in COMP_TAGS if any(k in t for k in kws)]
    return ",".join(found)


def save_check_result(sid, verdict, body, tags):
    if not sid:
        return
    con = _con()
    try:
        con.execute(
            "UPDATE scripts SET check_verdict=?, check_at=?, check_body=?, check_tags=? WHERE id=?",
            (verdict, datetime.now(KST).isoformat(), body, tags, int(sid)))
        con.commit()
    except Exception:
        pass
    finally:
        con.close()


def list_checked(verdict="all", tag="all", limit=100):
    sql = ("SELECT id, ts, request, check_verdict, check_tags FROM scripts "
           "WHERE check_verdict IS NOT NULL AND check_verdict<>''")
    args = []
    if verdict in ("통과", "주의", "수정 필요"):
        sql += " AND check_verdict=?"
        args.append(verdict)
    if tag and tag != "all":
        sql += " AND check_tags LIKE ?"
        args.append(f"%{tag}%")
    sql += " ORDER BY check_at DESC LIMIT ?"
    args.append(limit)
    return _rows(sql, tuple(args))


def _save_plan(request, body):
    con = _con()
    try:
        cur = con.execute("INSERT INTO plans(ts,request,body) VALUES(?,?,?)",
                          (datetime.now(KST).isoformat(), request, body))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def _save_script(request, body, plan_id=None):
    con = _con()
    try:
        cur = con.execute("INSERT INTO scripts(ts,request,body,plan_id) VALUES(?,?,?,?)",
                          (datetime.now(KST).isoformat(), request, body, plan_id))
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


# ================= 표시용 포맷 =================
def fmt_time(ts):
    try:
        return datetime.fromisoformat(ts).strftime("%H:%M")
    except Exception:
        return ""


def fmt_date(ymd, ts=""):
    try:
        dt = datetime.fromisoformat(ts) if ts else datetime.strptime(ymd, "%Y-%m-%d")
        return f"{dt.year}년 {dt.month}월 {dt.day}일 ({WEEKDAY_KR[dt.weekday()]})"
    except Exception:
        return ymd or ""


def kind_label(kind):
    return "오전" if kind == "am" else ("오후" if kind == "pm" else (kind or ""))


def source_label(source):
    return "자동" if source == "auto" else ("수동" if source == "manual" else (source or ""))


# ================= 시황 데이터 (캐시 + 차트) =================
UP_RED = "#D31A2B"      # 한국식: 상승 빨강
DOWN_BLUE = "#0B4EA2"   # 한국식: 하락 파랑
_MKT = {"focus": None, "kospi": None, "comp": None, "ts": 0}
_MKT_TTL = 3600  # 1시간 (일별 데이터라 자주 안 바뀜)
_MKT_LOCK = threading.Lock()
CHART_DAYS = 20


def _competitors():
    # 우선순위: settings.COMPETITORS(수동, 최우선) > 오전 브리핑 자동 등록(product_news)
    # 형식: { "집중상품코드": {"name": "TIGER OOO", "code": "종목코드"} }
    merged = {}
    for code, n in get_all_news().items():
        if n.get("comp_code") or n.get("comp_name"):
            merged[code] = {"name": n.get("comp_name") or "", "code": n.get("comp_code") or ""}
    manual = getattr(settings, "COMPETITORS", {}) or {}
    merged.update(manual)
    return merged


def refresh_market_cache():
    try:
        focus = market_data.focus_series(CHART_DAYS)
        kospi = market_data.index_daily_series("KOSPI", CHART_DAYS)
        comp = {}
        for fcode, c in _competitors().items():
            name = c.get("name", "")
            ccode = c.get("code", "")
            series = []
            if ccode:
                try:
                    series = market_data.daily_series(ccode, CHART_DAYS)
                except Exception:
                    series = []
            comp[fcode] = {"name": name, "code": ccode, "series": series}
        with _MKT_LOCK:
            _MKT["focus"], _MKT["kospi"], _MKT["comp"], _MKT["ts"] = focus, kospi, comp, time.time()
    except Exception:
        pass


def get_market():
    with _MKT_LOCK:
        fresh = _MKT["focus"] is not None and (time.time() - _MKT["ts"]) < _MKT_TTL
    if not fresh:
        refresh_market_cache()
    with _MKT_LOCK:
        return _MKT["focus"] or [], _MKT["kospi"] or [], _MKT["comp"] or {}


def start_refresher():
    # 시작 시 한 번 채우고, 이후 주기적으로 갱신. bot.py가 호출한다.
    def loop():
        while True:
            refresh_market_cache()
            time.sleep(_MKT_TTL)
    threading.Thread(target=loop, daemon=True).start()


def get_all_news():
    rows = _rows("SELECT code, title, url, comp_name, comp_code, updated_at FROM product_news")
    return {r[0]: {"title": r[1], "url": r[2], "comp_name": r[3], "comp_code": r[4], "updated_at": r[5]}
            for r in rows}


def _fmt_num(v, dec=0):
    try:
        return f"{v:,.{dec}f}"
    except Exception:
        return "-"


def _short_dt(iso):
    try:
        dt = datetime.fromisoformat(iso)
        return f"{dt.month}/{dt.day} {dt.strftime('%H:%M')}"
    except Exception:
        return iso or ""


def _trend_color(series):
    closes = [o["close"] for o in series if o.get("close") is not None]
    if len(closes) < 2:
        return "#334155"
    if closes[-1] > closes[0]:
        return UP_RED
    if closes[-1] < closes[0]:
        return DOWN_BLUE
    return "#334155"


def _sparkline(series, color, gid, w=320, h=70):
    closes = [o["close"] for o in series if o.get("close") is not None]
    if len(closes) < 2:
        return ""
    lo, hi = min(closes), max(closes)
    rng = (hi - lo) or 1
    n = len(closes)
    pts = [(i / (n - 1) * w, h - (c - lo) / rng * (h - 10) - 5) for i, c in enumerate(closes)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f"0,{h} " + line + f" {w},{h}"
    return (f"<svg class='chart' viewBox='0 0 {w} {h}' preserveAspectRatio='none' role='img'>"
            f"<polyline points='{area}' fill='url(#{gid})' stroke='none'/>"
            f"<polyline points='{line}' fill='none' stroke='{color}' stroke-width='2' "
            f"stroke-linejoin='round' stroke-linecap='round' vector-effect='non-scaling-stroke'/>"
            f"<defs><linearGradient id='{gid}' x1='0' y1='0' x2='0' y2='1'>"
            f"<stop offset='0' stop-color='{color}' stop-opacity='0.16'/>"
            f"<stop offset='1' stop-color='{color}' stop-opacity='0'/></linearGradient></defs></svg>")


def _volbars(series, w=320, h=26):
    if not any((o.get("vol") or 0) for o in series):
        return ""
    mx = max((o.get("vol") or 0) for o in series) or 1
    n = len(series)
    bw = w / n * 0.62
    gap = w / n
    bars = []
    for i, o in enumerate(series):
        v = o.get("vol") or 0
        r = o.get("rate") or 0
        col = "rgba(211,26,43,0.55)" if r > 0 else ("rgba(11,78,162,0.5)" if r < 0 else "#CBD3DD")
        bh = (v / mx) * h
        x = i * gap + (gap - bw) / 2
        bars.append(f"<rect x='{x:.1f}' y='{h - bh:.1f}' width='{bw:.1f}' height='{bh:.1f}' rx='1' fill='{col}'/>")
    return f"<svg class='vol' viewBox='0 0 {w} {h}' preserveAspectRatio='none'>{''.join(bars)}</svg>"


def _product_block(name, code, series, gid):
    head = (f"<div class='dtop'><span class='dname'>{html.escape(name)}</span>"
            f"<span class='dcode'>{html.escape(code)}</span></div>")
    if len(series) < 2:
        return head + "<div class='dmeta'>데이터를 가져오지 못했습니다.</div>"
    closes = [o["close"] for o in series]
    last = series[-1]
    rate = last.get("rate")
    up = (rate or 0) > 0
    down = (rate or 0) < 0
    cls = "up" if up else ("down" if down else "")
    arrow = "▲" if up else ("▼" if down else "-")
    rate_s = f"{arrow} {abs(rate):.2f}%" if rate is not None else ""
    hi, lo = max(closes), min(closes)
    color = UP_RED if up else (DOWN_BLUE if down else "#334155")
    chart = (f"<div class='pricebox'>"
             f"<span class='plabel top'>고 {_fmt_num(hi)}</span>"
             f"<span class='plabel bot'>저 {_fmt_num(lo)}</span>"
             f"{_sparkline(series, color, gid)}</div>{_volbars(series)}")
    return (head +
            f"<div class='dprice'><span class='dclose'>{_fmt_num(last['close'])}</span>"
            f"<span class='drate {cls}'>{html.escape(rate_s)}</span></div>" + chart +
            f"<div class='dmeta'>최근 {len(series)}거래일 · 참고: 네이버금융 시세</div>")


def _news_mood_request(name, code, news):
    title = (news.get("title") or "").strip()
    url = (news.get("url") or "").strip()
    return (f"[시황 분위기 중심 숏폼] {name} ({code}) 관련 오늘 시황 소재로 숏폼 스크립트를 써줘. "
            f"정보 전달·인과 단정보다 오늘 시장의 분위기와 온도를 전하는 톤으로. "
            f"특정 재료가 주가를 올렸다/내렸다는 미확인 인과 단정은 넣지 마라. "
            f"소재 기사: {title}" + (f" ({url})" if url else ""))


def _data_card(name, code, series, gid, competitor=None, news=None, news_slot=True, detail_url=None):
    comp_ok = competitor and len(competitor.get("series") or []) >= 2
    comp_note = ""
    if comp_ok:
        body = ("<div class='compare'>"
                "<div class='compare-col'><div class='rolelab our'>우리 · KODEX</div>"
                + _product_block(name, code, series, gid) + "</div>"
                "<div class='compare-col'><div class='rolelab comp'>경쟁사 · TIGER</div>"
                + _product_block(competitor.get("name") or "경쟁사",
                                 competitor.get("code") or "", competitor.get("series"), gid + "c") + "</div>"
                "</div>")
    else:
        body = _product_block(name, code, series, gid)
        if competitor and competitor.get("name"):
            comp_note = (f"<div class='compnote'>경쟁사 유사 ETF(참고): "
                         f"{html.escape(competitor['name'])}"
                         "<span class='compnote-sub'> · 종목코드가 확인되면 그래프가 나란히 표시됩니다</span></div>")
    newsblock = ""
    if news_slot:
        if news and news.get("title"):
            req = _news_mood_request(name, code, news)
            btn = ""
            if SCRIPT_FN:
                btn = (f"<button class='go newsgen' data-req=\"{html.escape(req)}\">이 기사로 시황 숏폼 만들기</button>"
                       "<div class='newsstatus statusline'></div>")
            meta = (f"<div class='newsmeta'>업데이트 {html.escape(_short_dt(news.get('updated_at')))}</div>"
                    if news.get("updated_at") else "")
            newsblock = (
                "<div class='newsbox'><div class='newslabel'>🎬 오늘의 시황 숏폼 소재</div>"
                f"<a class='newslink' href='{html.escape(news.get('url') or '#')}' target='_blank' rel='noopener'>"
                f"{html.escape(news['title'])}</a>{meta}{btn}</div>")
        else:
            newsblock = ("<div class='newsbox nb-empty'>오늘의 시황 숏폼 소재가 아직 없습니다. "
                         "평일 오전 9시 브리핑이 나오면 자동으로 채워집니다. "
                         "(텔레그램 봇 <code>/news</code> 명령으로 직접 등록·수정도 가능)</div>")
    detail = (f"<a class='detaillink' href='{detail_url}'>이 상품 상세·관련 콘텐츠 →</a>"
              if detail_url else "")
    return f"<div class='dcard'>{body}{comp_note}{newsblock}{detail}</div>"


# ================= 제작 브리프 텍스트 -> HTML =================
_HEADERS = ["🎬", "📋", "📌", "🎯", "🧩", "⚠️", "⚠", "🕐", "▶"]


def _match_header(line):
    s = line.strip()
    for h in _HEADERS:
        if s.startswith(h):
            return h
    return None


def _maybe_label(text):
    if ":" in text:
        label, rest = text.split(":", 1)
        lb = label.strip()
        if 0 < len(lb) <= 16 and not any(p in lb for p in ".!?"):
            return f"<strong>{html.escape(lb)}</strong> {html.escape(rest.strip())}"
    return html.escape(text)


def _section_body(lines):
    out, bullets, para, angle = [], [], [], None

    def flush_bullets():
        if bullets:
            out.append("<ul class='blist'>" + "".join(bullets) + "</ul>")
            bullets.clear()

    def flush_para():
        if para:
            out.append("<p class='sec-p'>" + "<br>".join(html.escape(x) for x in para) + "</p>")
            para.clear()

    def flush_angle():
        nonlocal angle
        if angle:
            num, atitle, bs = angle
            inner = ("<ul class='blist'>" + "".join(bs) + "</ul>") if bs else ""
            out.append(f"<div class='angle'><div class='angle-h'>"
                       f"<span class='anum'>{html.escape(num)}</span>{html.escape(atitle)}</div>{inner}</div>")
            angle = None

    for line in lines:
        s = line.strip()
        if s == "":
            flush_para()
            continue
        m = re.match(r"^(\d+)\.\s*(.*)$", s)
        if m:
            flush_para(); flush_bullets(); flush_angle()
            angle = [m.group(1) + ".", m.group(2), []]
            continue
        if s[:1] in "·-•":
            li = "<li>" + _maybe_label(s[1:].strip()) + "</li>"
            if angle is not None:
                angle[2].append(li)
            else:
                flush_para(); bullets.append(li)
            continue
        if angle is not None:
            angle[2].append("<li class='plain'>" + _maybe_label(s) + "</li>")
        else:
            flush_bullets(); para.append(s)

    flush_para(); flush_bullets(); flush_angle()
    return "".join(out)


def render_brief(body):
    lines = (body or "").splitlines()
    title, sections, cur = None, [], None
    for raw in lines:
        line = raw.rstrip()
        h = _match_header(line)
        if h == "🎬":
            title = line.strip()[len("🎬"):].strip()
            cur = None
            continue
        if h:
            cur = [h, line.strip()[len(h):].strip(), []]
            sections.append(cur)
            continue
        if cur is None:
            if not line.strip():
                continue
            cur = ["📋", "개요", []]
            sections.append(cur)
        cur[2].append(line)

    if not sections and not title:
        return f"<div class='body'>{html.escape(body or '')}</div>"

    out = []
    if title:
        out.append(f"<h1 class='dtitle'>{html.escape(title)}</h1>")
    for h, htext, content in sections:
        icon = "⚠️" if h == "⚠" else (h or "•")
        out.append("<section class='brief-sec'>")
        out.append(f"<h2 class='sec-h'><span class='sec-ic'>{html.escape(icon)}</span>{html.escape(htext)}</h2>")
        out.append(_section_body(content))
        out.append("</section>")
    return "".join(out)


# ================= 완성 스크립트 텍스트 -> HTML =================
def render_script(body):
    lines = (body or "").splitlines()
    sections, cur, pre = [], None, []
    for raw in lines:
        line = raw.rstrip()
        m = re.match(r"^\[(.+?)\]\s*$", line.strip())
        if m:
            cur = [m.group(1).strip(), []]
            sections.append(cur)
            continue
        if cur is None:
            if line.strip():
                pre.append(line)
            continue
        cur[1].append(line)

    if not sections:
        return f"<div class='body'>{html.escape(body or '')}</div>"

    out = []
    if pre:
        out.append("<div class='body'>" + "<br>".join(html.escape(p) for p in pre) + "</div>")
    for header, content in sections:
        disc = "유의문구" in header
        out.append("<section class='brief-sec" + (" disc" if disc else "") + "'>")
        out.append(f"<h2 class='sec-h'><span class='sec-ic'>▪</span>{html.escape(header)}</h2>")
        out.append(_script_body(header, content, disc))
        out.append("</section>")
    return "".join(out)


def _script_body(header, lines, disc):
    out, para = [], []
    is_body = "본문" in header

    def flush():
        if para:
            out.append("<p class='sec-p'>" + "<br>".join(html.escape(x) for x in para) + "</p>")
            para.clear()

    for line in lines:
        s = line.strip()
        if s == "":
            flush()
            continue
        if is_body and s.startswith("(") and s.endswith(")"):
            flush()
            out.append("<div class='scene'>🎬 " + html.escape(s[1:-1].strip()) + "</div>")
            continue
        para.append(s)
    flush()
    return "".join(out)


def render_check(text):
    lines = [l.rstrip() for l in (text or "").split("\n")]
    verdict, body = "", []
    for l in lines:
        s = l.strip()
        if not verdict and s.startswith("판정"):
            verdict = (s.split(":", 1)[1].strip() if ":" in s else s.replace("판정", "").strip())
            continue
        body.append(l)
    cls = "ok" if "통과" in verdict else ("warn" if "주의" in verdict else ("bad" if verdict else ""))
    badge = f"<div class='verdict {cls}'>판정 · {html.escape(verdict or '—')}</div>" if verdict else ""
    parts, ul = [], []

    def flush():
        if ul:
            parts.append("<ul class='clist'>" + "".join(f"<li>{x}</li>" for x in ul) + "</ul>")
            ul.clear()

    for l in body:
        s = l.strip()
        if not s:
            continue
        if s.startswith(("🚩", "⚠", "✅")):
            flush()
            parts.append(f"<div class='chead'>{html.escape(s)}</div>")
        elif s.startswith(("·", "-", "•")):
            ul.append(html.escape(s.lstrip("·-• ").strip()))
        else:
            flush()
            parts.append(f"<p class='cp'>{html.escape(s)}</p>")
    flush()
    return f"<div class='checkbox'>{badge}{''.join(parts)}</div>"


def verdict_badge(v):
    if not v:
        return ""
    cls = "ok" if "통과" in v else ("bad" if "수정" in v else "warn")
    return f"<span class='vbadge {cls}'>{html.escape(v)}</span>"


def _tag_chips(tags):
    items = [t for t in (tags or "").split(",") if t]
    if not items:
        return ""
    chips = "".join(f"<span class='ctag'>{html.escape(t)}</span>" for t in items)
    return f"<div class='ctags'>{chips}</div>"


def render_caption(text):
    t = (text or "").strip()
    return ("<div class='capbox'>"
            f"<pre class='captext'>{html.escape(t)}</pre>"
            f"<div class='caprow'><button class='go copybtn' data-copy=\"{html.escape(t, quote=True)}\">캡션 복사</button>"
            "<span class='copymsg'></span></div></div>")


# ================= HTML 뼈대 =================
CSS = """
:root{
  --ink:#101418; --bg:#F5F6F8; --surface:#FFFFFF; --line:#E4E7EC;
  --muted:#697586; --accent:#0B4EA2; --am:#B45309; --am-bg:#FBEEDD;
  --pm:#0B4EA2; --pm-bg:#E7EEF8;
  --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
  --sans:"Pretendard Variable",Pretendard,-apple-system,"Apple SD Gothic Neo","Noto Sans KR","Malgun Gothic",sans-serif;
}
*{box-sizing:border-box} html,body{margin:0}
body{background:var(--bg);color:var(--ink);font-family:var(--sans);line-height:1.62;-webkit-font-smoothing:antialiased}
.topbar{position:sticky;top:0;z-index:5;background:rgba(255,255,255,.92);backdrop-filter:saturate(180%) blur(8px);border-bottom:1px solid var(--line)}
.topbar .in{max-width:760px;margin:0 auto;display:flex;align-items:center;gap:14px;padding:11px 18px}
.brand{font-weight:800;font-size:15px;letter-spacing:-.01em;text-decoration:none;color:var(--ink)}
.nav{display:flex;gap:4px;margin-left:auto;flex-wrap:wrap}
.nav a{font-family:var(--mono);font-size:12px;color:var(--muted);text-decoration:none;padding:5px 9px;border-radius:8px}
.nav a:hover{color:var(--ink);background:var(--bg)} .nav a.on{color:var(--accent);background:var(--pm-bg)}
.wrap{max-width:760px;margin:0 auto;padding:0 18px 72px}
header.mast{padding:30px 0 16px;border-bottom:2px solid var(--ink);margin-bottom:8px}
.eyebrow{font-family:var(--mono);font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);margin:0 0 6px}
h1.title{font-size:30px;font-weight:800;letter-spacing:-.01em;margin:0}
.sub{color:var(--muted);font-size:14px;margin:6px 0 0}
.maker{font-family:var(--mono);font-size:12px;color:var(--muted);margin:12px 0 0}
.cards{display:grid;gap:12px;margin-top:22px}
a.tile{display:block;text-decoration:none;color:inherit;background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:18px;transition:transform .12s ease,box-shadow .12s ease}
a.tile:hover{transform:translateY(-2px);box-shadow:0 8px 22px rgba(16,20,24,.07)} a.tile.soon{opacity:.62}
.tile .tic{font-size:22px} .tile h3{margin:8px 0 4px;font-size:17px;font-weight:700} .tile p{margin:0;color:var(--muted);font-size:13.5px}
.tile .badge{float:right;font-family:var(--mono);font-size:10px;color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:2px 8px}
.datehead{font-family:var(--mono);font-size:13px;color:var(--muted);letter-spacing:.02em;margin:26px 0 10px;display:flex;align-items:center;gap:10px}
.datehead::after{content:"";flex:1;height:1px;background:var(--line)}
a.card{display:block;text-decoration:none;color:inherit;background:var(--surface);border:1px solid var(--line);border-left:3px solid var(--line);border-radius:12px;padding:14px 16px;margin:0 0 10px;transition:transform .12s ease,box-shadow .12s ease}
a.card:hover{transform:translateY(-1px);box-shadow:0 6px 18px rgba(16,20,24,.06)}
a.card.am{border-left-color:var(--am)} a.card.pm{border-left-color:var(--pm)}
.meta{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.pill{font-family:var(--mono);font-size:11px;font-weight:600;padding:2px 8px;border-radius:999px}
.pill.am{color:var(--am);background:var(--am-bg)} .pill.pm{color:var(--pm);background:var(--pm-bg)}
.tag{font-family:var(--mono);font-size:11px;color:var(--muted)} .time{font-family:var(--mono);font-size:11px;color:var(--muted);margin-left:auto}
.ctitle{font-size:15px;font-weight:600;line-height:1.45;margin:0}
.empty{background:var(--surface);border:1px dashed var(--line);border-radius:12px;padding:26px 18px;color:var(--muted);text-align:center;margin-top:20px}
.empty code{font-family:var(--mono);background:var(--bg);padding:2px 6px;border-radius:6px;color:var(--ink)}
a.back{display:inline-block;font-family:var(--mono);font-size:13px;color:var(--accent);text-decoration:none;margin:18px 0 4px} a.back:hover{text-decoration:underline}
.dtitle{font-size:22px;font-weight:800;line-height:1.4;margin:14px 0 6px}
.dmeta{font-family:var(--mono);font-size:12px;color:var(--muted);margin-bottom:16px}
.body{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:18px;white-space:pre-wrap;word-break:break-word;font-size:15px}
.brief-sec{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin:0 0 14px}
.brief-sec.disc{background:#FBFBFC} .brief-sec.disc .sec-p{font-size:12px;color:var(--muted);line-height:1.7}
.sec-h{display:flex;align-items:center;gap:9px;font-size:15px;font-weight:700;margin:0 0 10px;padding-bottom:9px;border-bottom:1px solid var(--line)}
.sec-ic{font-size:16px}
.sec-p{margin:0 0 8px;font-size:14.5px;color:#28303a;overflow-wrap:anywhere}
ul.blist{margin:6px 0 0;padding-left:18px} ul.blist li{margin:4px 0;font-size:14px;overflow-wrap:anywhere} li.plain{list-style:none;margin-left:-18px}
.body{overflow-wrap:anywhere}
.angle{background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:10px 0}
.angle-h{font-weight:700;font-size:14.5px;display:flex;gap:8px;align-items:baseline} .anum{font-family:var(--mono);color:var(--accent);font-weight:700}
.scene{background:var(--pm-bg);border-radius:8px;padding:8px 12px;margin:8px 0;font-size:13.5px;color:var(--pm);font-weight:500}
.field{margin-top:8px}
textarea.inp{width:100%;min-height:96px;resize:vertical;padding:13px 14px;font-size:15px;font-family:var(--sans);border:1px solid var(--line);border-radius:12px;background:var(--surface);color:var(--ink)}
textarea.inp:focus{outline:none;border-color:var(--accent)}
textarea.inp-tall{min-height:160px}
.checkwrap{margin-top:14px}
.checkresult{margin-top:8px}
.checkbox{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:16px 16px 8px}
.verdict{display:inline-block;font-weight:800;font-size:14px;padding:6px 14px;border-radius:999px;margin-bottom:10px;color:var(--muted);background:var(--bg)}
.verdict.ok{color:#0F7A3D;background:#E5F5EC} .verdict.warn{color:var(--am);background:var(--am-bg)} .verdict.bad{color:#D31A2B;background:#FBE7E9}
.chead{font-family:var(--mono);font-size:13px;font-weight:700;color:var(--ink);margin:12px 0 6px}
.checkbox .cp{margin:6px 0;font-size:14px;color:#28303a;overflow-wrap:anywhere}
ul.clist{margin:4px 0 8px;padding-left:18px} ul.clist li{margin:5px 0;font-size:14px;overflow-wrap:anywhere}
.vbadge{display:inline-block;font-family:var(--mono);font-size:11px;font-weight:700;padding:2px 8px;border-radius:999px;margin-left:6px;vertical-align:middle}
.vbadge.ok{color:#0F7A3D;background:#E5F5EC} .vbadge.warn{color:var(--am);background:var(--am-bg)} .vbadge.bad{color:#D31A2B;background:#FBE7E9}
.statgrid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px;margin:6px 0 20px}
.statcard{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:16px}
.stbig{font-size:28px;font-weight:800;letter-spacing:-.02em;color:var(--accent)}
.stlab{font-size:13px;font-weight:600;margin-top:2px}
.stsub{font-family:var(--mono);font-size:11px;color:var(--muted);margin-top:3px}
.rsec{margin:22px 0}
.rsec h3{font-size:14px;margin:0 0 10px}
.vdist{font-size:14px}
.wprow{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:10px 0;border-bottom:1px solid var(--line)}
.wpname{font-size:14px;font-weight:600}
.wchg{font-family:var(--mono);font-size:13px;font-weight:700;white-space:nowrap}
.wchg.up{color:#D31A2B} .wchg.down{color:#0B4EA2}
.wchg .muted{color:var(--muted);font-weight:400}
.capwrap{margin-top:14px}
.capbox{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:14px 16px;margin-top:8px}
pre.captext{white-space:pre-wrap;word-break:break-word;font-family:var(--sans);font-size:14.5px;line-height:1.6;color:var(--ink);margin:0 0 12px}
.caprow{display:flex;align-items:center;gap:8px}
.copybtn{font-size:13px;padding:8px 14px}
.copymsg{font-size:12px;color:#0F7A3D}
.searchbar{display:flex;gap:8px;flex-wrap:wrap;margin:4px 0 18px}
.searchbar input[type=text]{flex:1 1 200px;min-width:0;padding:11px 13px;font-size:15px;border:1px solid var(--line);border-radius:11px;background:var(--surface);color:var(--ink)}
.searchbar input[type=text]:focus{outline:none;border-color:var(--accent)}
.searchbar select{padding:11px 12px;font-size:14px;border:1px solid var(--line);border-radius:11px;background:var(--surface);color:var(--ink)}
.searchbar .go{padding:11px 18px}
.reslabel{font-size:13px;color:var(--muted);margin-bottom:10px}
.arow{align-items:center;gap:10px}
.tchip{flex:0 0 auto;font-family:var(--mono);font-size:10.5px;font-weight:700;padding:3px 8px;border-radius:999px;white-space:nowrap}
.tchip.am{color:var(--am);background:var(--am-bg)} .tchip.pm{color:var(--accent);background:var(--pm-bg)}
.tchip.brief{color:#7A3DBF;background:#F1E9FB} .tchip.script{color:#0F7A3D;background:#E5F5EC}
.atitle{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:14px;font-weight:600}
.detaillink{display:inline-block;margin-top:12px;font-size:13px;font-weight:600;color:var(--accent);text-decoration:none}
.detaillink:hover{text-decoration:underline}
.ctags{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}
.ctag{font-family:var(--mono);font-size:11px;font-weight:700;color:#5B6472;background:#EEF1F4;padding:3px 9px;border-radius:999px}
.rowtags{margin:-4px 0 10px 2px}
.savedcheck{font-family:var(--mono);font-size:11px;color:var(--muted);margin:4px 0 6px}
.filterline{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin:6px 0}
.flab{font-size:12px;color:var(--muted);margin-right:4px;min-width:52px}
.fchip{font-size:12.5px;font-weight:600;color:var(--ink);background:var(--surface);border:1px solid var(--line);border-radius:999px;padding:5px 12px;text-decoration:none}
.fchip.on{background:var(--accent);color:#fff;border-color:transparent}
button.go{margin-top:10px;background:var(--accent);color:#fff;border:0;border-radius:10px;font-size:15px;font-weight:600;padding:12px 20px;cursor:pointer}
button.go:disabled{opacity:.55;cursor:default}
.statusline{margin-top:14px;font-size:14px;color:var(--muted);display:flex;align-items:center;gap:10px;min-height:22px}
.spin{width:16px;height:16px;border:2px solid var(--line);border-top-color:var(--accent);border-radius:50%;animation:sp .8s linear infinite;display:inline-block}
@keyframes sp{to{transform:rotate(360deg)}}
.recent{margin-top:30px} .recent h3{font-family:var(--mono);font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin:0 0 8px}
a.rrow{display:flex;gap:10px;text-decoration:none;color:inherit;padding:11px 14px;background:var(--surface);border:1px solid var(--line);border-radius:10px;margin-bottom:8px;font-size:14px}
a.rrow:hover{border-color:var(--accent)} a.rrow .rt{font-family:var(--mono);font-size:11px;color:var(--muted);white-space:nowrap;margin-left:auto}
a.openbot{display:inline-block;background:#229ED9;color:#fff;text-decoration:none;font-weight:600;font-size:15px;padding:12px 20px;border-radius:10px;margin:6px 0 4px}
.cmd{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:8px 0}
.cmd code{font-family:var(--mono);font-weight:700;color:var(--accent);background:var(--pm-bg);padding:2px 8px;border-radius:6px;font-size:13px}
.cmd p{margin:6px 0 0;font-size:14px;color:#28303a}
.divider{margin:16px 0;font-weight:700;font-size:15px}
.dsectitle{font-family:var(--mono);font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin:24px 0 10px}
.dcard{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin:0 0 12px}
.dtop{display:flex;align-items:baseline;gap:8px} .dname{font-weight:700;font-size:15px} .dcode{font-family:var(--mono);font-size:11px;color:var(--muted)}
.dprice{display:flex;align-items:baseline;gap:10px;margin:4px 0 10px}
.dclose{font-size:20px;font-weight:800;letter-spacing:-.01em}
.drate{font-family:var(--mono);font-size:13px;font-weight:700} .drate.up{color:#D31A2B} .drate.down{color:#0B4EA2}
svg.chart{display:block;width:100%;height:70px} svg.vol{display:block;width:100%;height:26px;margin-top:2px;opacity:.9}
.pricebox{position:relative}
.plabel{position:absolute;right:2px;font-family:var(--mono);font-size:10px;color:var(--muted);background:rgba(255,255,255,.7);padding:0 3px;border-radius:4px;pointer-events:none}
.plabel.top{top:0} .plabel.bot{bottom:2px}
.compare{display:flex;flex-wrap:wrap;gap:14px} .compare-col{flex:1 1 250px;min-width:0}
.rolelab{font-family:var(--mono);font-size:11px;letter-spacing:.04em;margin-bottom:6px;display:inline-block;padding:2px 8px;border-radius:999px}
.rolelab.our{color:var(--accent);background:var(--pm-bg)} .rolelab.comp{color:#5B6472;background:#EEF1F4}
.newsbox{margin-top:14px;padding-top:14px;border-top:1px dashed var(--line)}
.newslabel{font-family:var(--mono);font-size:11px;letter-spacing:.04em;color:var(--am);margin-bottom:6px}
a.newslink{display:block;font-size:15px;font-weight:600;color:var(--ink);text-decoration:none;line-height:1.45;overflow-wrap:anywhere}
a.newslink:hover{color:var(--accent);text-decoration:underline}
.newsmeta{font-family:var(--mono);font-size:11px;color:var(--muted);margin-top:4px}
.newsgen{margin-top:12px;font-size:14px;padding:10px 16px}
.newsstatus{margin-top:8px}
.newsbox.nb-empty{color:var(--muted);font-size:13px}
.newsbox.nb-empty code{font-family:var(--mono);background:var(--bg);padding:2px 6px;border-radius:6px;color:var(--ink)}
.compnote{margin-top:10px;font-size:12.5px;color:#5B6472;background:#EEF1F4;border-radius:8px;padding:8px 10px;overflow-wrap:anywhere}
.compnote-sub{color:var(--muted)}
.footer{}
footer{margin-top:34px;padding-top:14px;border-top:1px solid var(--line);font-size:12px;color:var(--muted)}
footer div{margin:2px 0}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:6px}
.brand{display:flex;align-items:center;gap:8px}
.brandlogo{height:19px;width:auto;display:block}
.brandsub{font-weight:800;font-size:14px;color:var(--muted);letter-spacing:-.01em}
.login-wrap{max-width:400px;margin:0 auto;padding:64px 22px;min-height:100vh;display:flex;flex-direction:column;justify-content:center}
.login-card{background:var(--surface);border:1px solid var(--line);border-radius:18px;padding:32px 26px;box-shadow:0 8px 30px rgba(16,20,24,.06)}
.login-logo{height:30px;width:auto;display:block;margin:0 auto 8px}
.login-card h1{font-size:17px;text-align:center;margin:6px 0 4px}
.login-card p.sub{text-align:center;font-size:13px;margin:0 0 20px}
.login-card input{width:100%;padding:13px 14px;font-size:16px;border:1px solid var(--line);border-radius:12px;background:var(--bg);color:var(--ink);margin-bottom:12px}
.login-card input:focus{outline:none;border-color:var(--accent);background:var(--surface)}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
"""

FONT = ('<link rel="stylesheet" '
        'href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable.min.css">')


def _nav(active):
    items = [("/", "홈"), ("/bot", "텔레그램 봇"), ("/archive", "아카이브"),
             ("/plan", "제작 브리프"), ("/check", "컴플 체크"), ("/report", "주간 리포트"),
             ("/data", "데이터")]
    links = "".join(
        f"<a href='{href}' class='{'on' if active == href else ''}'>{html.escape(label)}</a>"
        for href, label in items)
    return ("<div class='topbar'><div class='in'>"
            "<a class='brand' href='/'><img class='brandlogo' src='/logo.svg' alt='KODEX'>"
            "<span class='brandsub'>시황</span></a>"
            f"<nav class='nav'>{links}</nav></div></div>")


def page(title, inner, active="", extra_head=""):
    return HTMLResponse(
        "<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<meta name='robots' content='noindex, nofollow'>"
        "<meta name='theme-color' content='#0B4EA2'>"
        f"<title>{html.escape(title)}</title>{FONT}<style>{CSS}</style>{extra_head}</head>"
        f"<body>{_nav(active)}<div class='wrap'>{inner}</div></body></html>")


FOOT = (f"<footer><div>제작자: {html.escape(MAKER)}</div>"
        "<div>본 페이지는 콘텐츠 기획 참고용입니다. 모든 수치·주가·뉴스는 사용 전 원문 및 준법 확인이 필요합니다.</div>"
        "<div><a href='/logout' style='color:var(--muted)'>로그아웃</a></div></footer>")


# ================= 공용 JS (job 폴링) =================
def _poll_js(btn, inp, status, endpoint, view_prefix, id_field, extra_payload=""):
    return ("""
document.addEventListener('DOMContentLoaded', function(){
  var go=document.getElementById('%s'), inp=document.getElementById('%s'), st=document.getElementById('%s');
  if(!go) return;
  go.addEventListener('click', function(){
    var v=(inp.value||'').trim();
    if(!v){ st.textContent='내용을 입력해 주세요.'; return; }
    go.disabled=true;
    st.innerHTML="<span class='spin'></span><span>생성 중입니다… 웹 검색 포함 30초~1분 걸립니다. 이 화면을 닫지 마세요.</span>";
    fetch('%s',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request:v%s})})
      .then(function(r){return r.json();})
      .then(function(j){ if(!j.job_id){throw new Error(j.error||'요청 실패');} poll(j.job_id); })
      .catch(function(e){ go.disabled=false; st.textContent='오류: '+e.message; });
  });
  function poll(id){
    var t=setInterval(function(){
      fetch('/job/'+id).then(function(r){return r.json();}).then(function(j){
        if(j.status==='done'){ clearInterval(t); window.location='%s'+j.%s; }
        else if(j.status==='error'){ clearInterval(t); go.disabled=false; st.textContent='생성 중 오류: '+(j.error||'알 수 없음'); }
      }).catch(function(){});
    }, 3000);
  }
});
""" % (btn, inp, status, endpoint, extra_payload, view_prefix, id_field))


DATA_JS = """
document.addEventListener('DOMContentLoaded', function(){
  document.addEventListener('click', function(e){
    var b = e.target.closest ? e.target.closest('.newsgen') : null;
    if(!b) return;
    var req = b.getAttribute('data-req') || '';
    var st = b.parentNode.querySelector('.newsstatus');
    b.disabled = true;
    if(st) st.innerHTML = "<span class='spin'></span><span>시황 숏폼 스크립트를 만드는 중… 30초~1분. 이 화면을 닫지 마세요.</span>";
    fetch('/script/new',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request:req})})
      .then(function(r){return r.json();})
      .then(function(j){ if(!j.job_id){throw new Error(j.error||'요청 실패');} poll(j.job_id, b, st); })
      .catch(function(err){ b.disabled=false; if(st) st.textContent='오류: '+err.message; });
  });
  function poll(id, b, st){
    var t=setInterval(function(){
      fetch('/job/'+id).then(function(r){return r.json();}).then(function(j){
        if(j.status==='done'){ clearInterval(t); window.location='/script/view/'+j.script_id; }
        else if(j.status==='error'){ clearInterval(t); b.disabled=false; if(st) st.textContent='생성 중 오류: '+(j.error||'알 수 없음'); }
      }).catch(function(){});
    }, 3000);
  }
});
"""


CHECK_JS = """
document.addEventListener('DOMContentLoaded', function(){
  document.addEventListener('click', function(e){
    var b = e.target.closest ? e.target.closest('.checkbtn') : null;
    if(!b) return;
    var wrap = b.parentNode;
    var st = wrap.querySelector('.checkstatus');
    var res = wrap.querySelector('.checkresult');
    var payload;
    var sid = b.getAttribute('data-sid');
    if(sid){ payload = {script_id: sid}; }
    else {
      var text = b.getAttribute('data-text');
      if(text===null){
        var inp = document.getElementById(b.getAttribute('data-input'));
        text = inp ? (inp.value||'').trim() : '';
      }
      if(!text){ if(st) st.textContent='점검할 내용을 입력해 주세요.'; return; }
      payload = {request: text};
    }
    b.disabled=true; if(res) res.innerHTML='';
    if(st) st.innerHTML="<span class='spin'></span><span>컴플 체크 중… 10~30초. 이 화면을 닫지 마세요.</span>";
    fetch('/check/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})
      .then(function(r){return r.json();})
      .then(function(j){ if(!j.job_id){throw new Error(j.error||'요청 실패');} poll(j.job_id, b, st, res); })
      .catch(function(err){ b.disabled=false; if(st) st.textContent='오류: '+err.message; });
  });
  function poll(id, b, st, res){
    var t=setInterval(function(){
      fetch('/job/'+id).then(function(r){return r.json();}).then(function(j){
        if(j.status==='done'){ clearInterval(t); b.disabled=false; if(st) st.textContent=''; if(res) res.innerHTML=j.html||''; }
        else if(j.status==='error'){ clearInterval(t); b.disabled=false; if(st) st.textContent='점검 중 오류: '+(j.error||'알 수 없음'); }
      }).catch(function(){});
    }, 2500);
  }
});
"""


CAPTION_JS = """
document.addEventListener('DOMContentLoaded', function(){
  document.addEventListener('click', function(e){
    var cp = e.target.closest ? e.target.closest('.copybtn') : null;
    if(cp){
      var txt = cp.getAttribute('data-copy')||'';
      if(navigator.clipboard){ navigator.clipboard.writeText(txt).then(function(){
        var m=cp.parentNode.querySelector('.copymsg'); if(m){m.textContent=' 복사됐어요';}
      }); }
      return;
    }
    var b = e.target.closest ? e.target.closest('.capbtn') : null;
    if(!b) return;
    var wrap = b.parentNode;
    var st = wrap.querySelector('.capstatus');
    var res = wrap.querySelector('.capresult');
    var payload;
    var sid = b.getAttribute('data-sid');
    if(sid){ payload = {script_id: sid}; }
    else {
      var inp = document.getElementById(b.getAttribute('data-input'));
      var text = inp ? (inp.value||'').trim() : '';
      if(!text){ if(st) st.textContent='내용을 입력해 주세요.'; return; }
      payload = {request: text};
    }
    b.disabled=true; if(res) res.innerHTML='';
    if(st) st.innerHTML="<span class='spin'></span><span>캡션·해시태그 생성 중… 10~30초.</span>";
    fetch('/caption/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)})
      .then(function(r){return r.json();})
      .then(function(j){ if(!j.job_id){throw new Error(j.error||'요청 실패');} poll(j.job_id, b, st, res); })
      .catch(function(err){ b.disabled=false; if(st) st.textContent='오류: '+err.message; });
  });
  function poll(id, b, st, res){
    var t=setInterval(function(){
      fetch('/job/'+id).then(function(r){return r.json();}).then(function(j){
        if(j.status==='done'){ clearInterval(t); b.disabled=false; if(st) st.textContent=''; if(res) res.innerHTML=j.html||''; }
        else if(j.status==='error'){ clearInterval(t); b.disabled=false; if(st) st.textContent='생성 중 오류: '+(j.error||'알 수 없음'); }
      }).catch(function(){});
    }, 2500);
  }
});
"""


# ================= 라우트 =================
@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return "User-agent: *\nDisallow: /\n"


# ---------- 접근 게이트 (비밀번호) ----------
def _authed(request):
    return request.cookies.get(AUTH_COOKIE) == AUTH_TOKEN


@app.middleware("http")
async def _gate(request: Request, call_next):
    path = request.url.path
    if path in GATE_EXEMPT or _authed(request):
        return await call_next(request)
    nxt = path + (("?" + request.url.query) if request.url.query else "")
    return RedirectResponse("/login?next=" + quote(nxt, safe=""), status_code=302)


@app.get("/logo.svg")
def logo_svg():
    global _LOGO_CACHE
    if _LOGO_CACHE is None:
        try:
            _LOGO_CACHE = LOGO_PATH.read_text(encoding="utf-8")
        except Exception:
            _LOGO_CACHE = ""
    if not _LOGO_CACHE:
        return Response(status_code=404)
    return Response(_LOGO_CACHE, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if _authed(request):
        return RedirectResponse("/", status_code=302)
    nxt = request.query_params.get("next", "/")
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/"
    js = ("<script>var NEXT=" + json.dumps(nxt) + ";"
          "document.addEventListener('DOMContentLoaded',function(){"
          "var go=document.getElementById('lgo'),inp=document.getElementById('lpw'),st=document.getElementById('lst');"
          "function submit(){var v=inp.value||'';if(!v){st.textContent='비밀번호를 입력하세요.';return;}"
          "go.disabled=true;st.textContent='확인 중…';"
          "fetch('/login/auth',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:v,next:NEXT})})"
          ".then(function(r){return r.json();}).then(function(j){if(j.ok){location.href=j.next||'/';}else{go.disabled=false;st.textContent=j.error||'실패';}})"
          ".catch(function(){go.disabled=false;st.textContent='오류가 발생했습니다.';});}"
          "go.addEventListener('click',submit);inp.addEventListener('keydown',function(e){if(e.key==='Enter')submit();});"
          "inp.focus();});</script>")
    body = (
        "<div class='login-wrap'><div class='login-card'>"
        "<img class='login-logo' src='/logo.svg' alt='KODEX'>"
        "<h1>시황 콘텐츠 허브</h1>"
        "<p class='sub'>접근하려면 비밀번호를 입력하세요.</p>"
        "<input id='lpw' type='password' placeholder='비밀번호' autocomplete='current-password'>"
        "<button id='lgo' class='go' style='width:100%'>들어가기</button>"
        "<div id='lst' class='statusline' style='justify-content:center'></div>"
        "</div></div>")
    return HTMLResponse(
        "<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<meta name='robots' content='noindex, nofollow'><meta name='theme-color' content='#0B4EA2'>"
        f"<title>로그인 · KODEX 시황</title>{FONT}<style>{CSS}</style>{js}</head>"
        f"<body>{body}</body></html>")


@app.post("/login/auth")
async def login_auth(request: Request):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "잘못된 요청입니다."}, status_code=400)
    pw = data.get("password") or ""
    nxt = data.get("next") or "/"
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/"
    if pw != WEB_PASSWORD:
        return JSONResponse({"ok": False, "error": "비밀번호가 올바르지 않습니다."})
    resp = JSONResponse({"ok": True, "next": nxt})
    resp.set_cookie(AUTH_COOKIE, AUTH_TOKEN, max_age=60 * 60 * 24 * 30,
                    httponly=True, samesite="lax")
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(AUTH_COOKIE)
    return resp


# ---------- 대중용 공개 도구: 월 배당 계산기 (비밀번호 없음, 브랜드·상품명 없음) ----------
DIVIDEND_CSS = """
*{box-sizing:border-box} :root{--bg:#0E1730;--card:#17213C;--line:#2A375A;--ink:#EAF0FF;--sub:#9FB0D6;--accent:#4C8DFF;--accent2:#8AB4FF;--good:#37D39B}
html,body{margin:0} body{font-family:'Pretendard Variable',Pretendard,-apple-system,system-ui,sans-serif;background:radial-gradient(1200px 600px at 50% -10%,#1b2a52 0,#0E1730 60%),#0E1730;color:var(--ink);min-height:100vh}
.wrap{max-width:520px;margin:0 auto;padding:28px 18px 60px}
.hero{text-align:center;padding:14px 0 8px}
.hero h1{font-size:26px;font-weight:800;letter-spacing:-.02em;margin:0 0 8px}
.hero p{color:var(--sub);font-size:14.5px;line-height:1.6;margin:0}
.tabs{display:flex;gap:8px;margin:22px 0 16px}
.tab{flex:1;text-align:center;padding:12px;border-radius:12px;border:1px solid var(--line);background:var(--card);color:var(--sub);font-weight:700;font-size:14px;cursor:pointer;transition:.15s}
.tab.on{background:linear-gradient(135deg,var(--accent),#6F7BFF);color:#fff;border-color:transparent}
.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:22px}
.field{margin-bottom:18px}
.field label{display:block;font-size:13px;color:var(--sub);margin-bottom:8px;font-weight:600}
.amt{position:relative}
.amt input{width:100%;padding:14px 42px 14px 14px;font-size:20px;font-weight:800;border:1px solid var(--line);border-radius:12px;background:#0F1830;color:var(--ink);text-align:right}
.amt input:focus{outline:none;border-color:var(--accent)}
.amt .unit{position:absolute;right:14px;top:50%;transform:translateY(-50%);color:var(--sub);font-weight:700}
.quick{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.quick button{flex:1;min-width:60px;padding:8px;border:1px solid var(--line);background:#0F1830;color:var(--accent2);border-radius:9px;font-size:12.5px;font-weight:700;cursor:pointer}
.slider label{display:flex;justify-content:space-between}
.slider .yv{color:var(--accent2);font-weight:800}
input[type=range]{width:100%;accent-color:var(--accent);height:26px}
.rangehint{display:flex;justify-content:space-between;font-size:11px;color:var(--sub);margin-top:-2px}
.result{margin-top:8px;text-align:center;background:linear-gradient(135deg,#132a52,#141d38);border:1px solid #315089;border-radius:16px;padding:22px}
.result .lab{font-size:13px;color:var(--accent2);font-weight:700;margin-bottom:6px}
.result .big{font-size:34px;font-weight:900;letter-spacing:-.02em;line-height:1.15}
.result .sub{font-size:13px;color:var(--sub);margin-top:8px;line-height:1.5}
.share{margin-top:14px;width:100%;padding:13px;border-radius:12px;border:none;background:#22315a;color:var(--ink);font-weight:700;font-size:14px;cursor:pointer}
.disc{margin-top:20px;font-size:11.5px;color:#8394BC;line-height:1.7;background:#111a33;border:1px solid var(--line);border-radius:12px;padding:14px}
.disc b{color:#AFC0E6}
.foot{text-align:center;color:#6B7BA6;font-size:11px;margin-top:22px}
"""

DIVIDEND_JS = """
(function(){
  var mode=1;
  var $=function(id){return document.getElementById(id);};
  function won(n){ n=Math.max(0,Math.round(n));
    if(n>=100000000){var e=Math.floor(n/100000000),m=Math.round((n%100000000)/10000);return e+'억'+(m?' '+m.toLocaleString()+'만':'')+'원';}
    if(n>=10000){return Math.round(n/10000).toLocaleString()+'만원';}
    return n.toLocaleString()+'원';}
  function num(v){return parseFloat((v||'').toString().replace(/[^0-9.]/g,''))||0;}
  function fmt(el){var v=num(el.value);el.value=v?v.toLocaleString():'';}
  function calc(){
    var y=parseFloat($('yield').value); $('yv').textContent=y.toFixed(1)+'%';
    if(mode===1){
      var amt=num($('amt').value); var monthly=amt*(y/100)/12;
      $('rlab').textContent='매달 예상 수령액 (가정)';
      $('rbig').textContent='월 '+won(monthly);
      $('rsub').textContent='연 '+won(monthly*12)+' · 연 분배율 '+y+'% 가정 · 세전·비용 미반영';
    } else {
      var tgt=num($('tgt').value); var need=(y>0)?(tgt*12/(y/100)):0;
      $('rlab').textContent='필요한 투자 원금 (가정)';
      $('rbig').textContent=won(need);
      $('rsub').textContent='매달 '+won(tgt)+' 받으려면 · 연 분배율 '+y+'% 가정 · 세전·비용 미반영';
    }
  }
  function setMode(m){ mode=m;
    $('t1').classList.toggle('on',m===1); $('t2').classList.toggle('on',m===2);
    $('box1').style.display=(m===1)?'block':'none'; $('box2').style.display=(m===2)?'block':'none';
    calc();
  }
  window.addEventListener('DOMContentLoaded',function(){
    $('t1').addEventListener('click',function(){setMode(1);});
    $('t2').addEventListener('click',function(){setMode(2);});
    ['amt','tgt'].forEach(function(id){var el=$(id);el.addEventListener('input',function(){fmt(el);calc();});});
    $('yield').addEventListener('input',calc);
    document.querySelectorAll('.quick button').forEach(function(b){
      b.addEventListener('click',function(){var t=$(b.getAttribute('data-t'));t.value=parseInt(b.getAttribute('data-v')).toLocaleString();calc();});
    });
    $('share').addEventListener('click',function(){
      if(navigator.share){navigator.share({title:'월 배당 계산기',url:location.href});}
      else if(navigator.clipboard){navigator.clipboard.writeText(location.href);$('share').textContent='링크가 복사됐어요';}
    });
    setMode(1);
  });
})();
"""


@app.get("/dividend", response_class=HTMLResponse)
def dividend_tool():
    body = (
        "<div class='wrap'>"
        "<div class='hero'><h1>월 배당 계산기</h1>"
        "<p>얼마를 넣으면 매달 얼마나 받을까?<br>반대로 매달 원하는 금액을 받으려면 얼마가 필요할까?</p></div>"
        "<div class='tabs'><div id='t1' class='tab on'>금액 → 월 수령</div>"
        "<div id='t2' class='tab'>목표 월수령 → 필요 금액</div></div>"
        "<div class='card'>"
        # 모드1
        "<div id='box1'>"
        "<div class='field'><label>투자 금액</label>"
        "<div class='amt'><input id='amt' inputmode='numeric' placeholder='0' value='200,000,000'><span class='unit'>원</span></div>"
        "<div class='quick'>"
        "<button data-t='amt' data-v='10000000'>1천만</button>"
        "<button data-t='amt' data-v='50000000'>5천만</button>"
        "<button data-t='amt' data-v='100000000'>1억</button>"
        "<button data-t='amt' data-v='300000000'>3억</button></div></div>"
        "</div>"
        # 모드2
        "<div id='box2' style='display:none'>"
        "<div class='field'><label>목표 월 수령액</label>"
        "<div class='amt'><input id='tgt' inputmode='numeric' placeholder='0' value='3,000,000'><span class='unit'>원</span></div>"
        "<div class='quick'>"
        "<button data-t='tgt' data-v='1000000'>100만</button>"
        "<button data-t='tgt' data-v='2000000'>200만</button>"
        "<button data-t='tgt' data-v='3000000'>300만</button>"
        "<button data-t='tgt' data-v='5000000'>500만</button></div></div>"
        "</div>"
        # 분배율 슬라이더
        "<div class='field slider'><label>연 분배율 가정 <span class='yv' id='yv'>8.0%</span></label>"
        "<input id='yield' type='range' min='1' max='20' step='0.5' value='8'>"
        "<div class='rangehint'><span>1%</span><span>20%</span></div></div>"
        # 결과
        "<div class='result'><div class='lab' id='rlab'>매달 예상 수령액 (가정)</div>"
        "<div class='big' id='rbig'>월 0원</div><div class='sub' id='rsub'></div></div>"
        "<button id='share' class='share'>계산기 공유하기</button>"
        "</div>"
        # 컴플 안내
        "<div class='disc'><b>꼭 확인하세요.</b> 이 계산기는 여러분이 직접 입력한 가정(연 분배율)에 따른 단순 산수 결과입니다. "
        "특정 금융상품의 수익률이 아니며, 미래의 수익이나 배당을 보장하지 않습니다. "
        "세금·수수료 등 비용은 반영되어 있지 않습니다. 투자 권유가 아닌 교육용 참고 자료입니다.</div>"
        "<div class='foot'>교육용 참고 도구 · 실제 투자 결정은 본인의 판단과 책임입니다.</div>"
        "</div>")
    return HTMLResponse(
        "<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<meta name='robots' content='noindex, nofollow'><meta name='theme-color' content='#0E1730'>"
        f"<title>월 배당 계산기</title>{FONT}<style>{DIVIDEND_CSS}</style>"
        f"<script>{DIVIDEND_JS}</script></head><body>{body}</body></html>")


# ---------- 공개 도구 공용 레이아웃 (지식·설문) ----------
PUBLIC_CSS = """
*{box-sizing:border-box} :root{--bg:#0E1730;--card:#17213C;--line:#2A375A;--ink:#EAF0FF;--sub:#9FB0D6;--accent:#4C8DFF;--accent2:#8AB4FF;--good:#37D39B}
html,body{margin:0} body{font-family:'Pretendard Variable',Pretendard,-apple-system,system-ui,sans-serif;background:radial-gradient(1200px 600px at 50% -10%,#1b2a52 0,#0E1730 60%),#0E1730;color:var(--ink);min-height:100vh}
.wrap{max-width:560px;margin:0 auto;padding:28px 18px 60px}
.hero{text-align:center;padding:10px 0 6px}
.hero h1{font-size:25px;font-weight:800;letter-spacing:-.02em;margin:0 0 8px}
.hero p{color:var(--sub);font-size:14px;line-height:1.6;margin:0}
.qcard{background:var(--card);border:1px solid var(--line);border-radius:14px;margin:10px 0;overflow:hidden}
.qcard summary{list-style:none;cursor:pointer;padding:16px 18px;font-weight:700;font-size:15px;display:flex;justify-content:space-between;gap:10px}
.qcard summary::-webkit-details-marker{display:none}
.qcard summary .arw{color:var(--accent2);transition:.2s}
.qcard[open] summary .arw{transform:rotate(90deg)}
.qcard .ans{padding:0 18px 16px;color:#CBD6F0;font-size:14px;line-height:1.7}
.cat{font-family:ui-monospace,monospace;font-size:11px;color:var(--accent2);letter-spacing:.05em;margin:22px 0 4px;font-weight:700}
.sq{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:20px;margin:12px 0}
.sq h3{font-size:16px;margin:0 0 14px;font-weight:800}
.sq .opt{display:block;width:100%;text-align:left;padding:13px 15px;margin:8px 0;border:1px solid var(--line);border-radius:11px;background:#0F1830;color:var(--ink);font-size:14.5px;font-weight:600;cursor:pointer;transition:.12s}
.sq .opt:hover{border-color:var(--accent)}
.sq .bar{margin:8px 0;position:relative;background:#0F1830;border-radius:9px;overflow:hidden;height:38px;border:1px solid var(--line)}
.sq .bar .fill{position:absolute;left:0;top:0;bottom:0;background:linear-gradient(90deg,#2C63C7,#4C8DFF);border-radius:9px;transition:width .5s}
.sq .bar .txt{position:absolute;left:12px;top:0;bottom:0;display:flex;align-items:center;font-size:13.5px;font-weight:700;z-index:1}
.sq .bar .pct{position:absolute;right:12px;top:0;bottom:0;display:flex;align-items:center;font-size:13px;font-weight:800;color:var(--accent2);z-index:1}
.sq .voted{font-size:12px;color:var(--good);margin-top:6px}
.sq .total{font-size:12px;color:var(--sub);margin-top:8px}
.disc{margin-top:20px;font-size:11.5px;color:#8394BC;line-height:1.7;background:#111a33;border:1px solid var(--line);border-radius:12px;padding:14px}
.disc b{color:#AFC0E6}
.foot{text-align:center;color:#6B7BA6;font-size:11px;margin-top:22px}
.plink{display:inline-block;margin:14px auto 0;color:var(--accent2);font-size:13px;text-decoration:none;font-weight:600}
"""


def public_page(title, body, extra_head=""):
    return HTMLResponse(
        "<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<meta name='robots' content='noindex, nofollow'><meta name='theme-color' content='#0E1730'>"
        f"<title>{html.escape(title)}</title>{FONT}<style>{PUBLIC_CSS}</style>{extra_head}"
        f"<body>{body}</body></html>")


# 대중용 지식 카드 (연금·투자·경제 기초 · 상품/브랜드/ETF 언급 없음, 교육용)
LEARN_CARDS = [
    ("연금 계좌 기초", [
        ("연금저축과 IRP, 뭐가 달라요?",
         "둘 다 노후를 위해 세제 혜택을 주는 계좌예요. 연금저축은 누구나 만들 수 있고, IRP(개인형 퇴직연금)는 소득이 있는 사람이 퇴직금까지 넣을 수 있어요. 두 계좌를 합쳐 연간 최대 900만 원까지 세액공제를 받을 수 있습니다."),
        ("ISA는 뭐예요?",
         "ISA(개인종합자산관리계좌)는 여러 상품을 한 계좌에서 굴리며 이자·수익에 세금 혜택을 받는 계좌예요. 만기 후 연금 계좌로 옮기면 추가 세제 혜택도 있습니다."),
        ("세액공제가 정확히 뭐예요?",
         "낸 세금에서 일정 금액을 직접 깎아주는 걸 말해요. 예를 들어 연금저축·IRP에 넣으면 넣은 금액의 일부(소득에 따라 13.2~16.5%)를 연말정산 때 돌려받습니다."),
    ]),
    ("투자 기본 개념", [
        ("복리가 왜 중요해요?",
         "번 수익에 다시 수익이 붙는 걸 복리라고 해요. 시간이 길수록 눈덩이처럼 커지기 때문에, 일찍 시작할수록 유리하다고 말하는 이유예요."),
        ("분산투자가 뭐예요?",
         "한 곳에 몰아넣지 않고 여러 자산·지역·업종에 나눠 담는 걸 말해요. 하나가 흔들려도 전체 충격을 줄이는 효과가 있습니다."),
        ("인플레이션이 내 돈에 어떤 영향을 줘요?",
         "물가가 오르면 같은 돈으로 살 수 있는 게 줄어요. 즉 현금을 그냥 두면 실질 가치가 조금씩 깎이는 셈이라, 이를 방어하려 투자하는 사람이 많습니다."),
    ]),
    ("요즘 뜨는 궁금증", [
        ("GPU랑 CPU, 뭐가 달라요?",
         "CPU는 복잡한 일을 순서대로 빠르게 처리하는 '만능 일꾼', GPU는 단순한 계산을 동시에 엄청 많이 처리하는 '병렬 일꾼'이에요. AI 학습처럼 같은 계산을 대량으로 돌릴 땐 GPU가 강해서 요즘 수요가 폭발했습니다."),
        ("요즘 IT 기기가 왜 이렇게 비싸졌어요?",
         "반도체·메모리 수요가 늘고 환율·부품 값이 오르면서 노트북 같은 IT 기기 가격도 전반적으로 올랐어요. 기술이 좋아진 만큼 값도 오른 부분이 있습니다."),
    ]),
]


@app.get("/learn", response_class=HTMLResponse)
def learn_tool():
    parts = ["<div class='wrap'>",
             "<div class='hero'><h1>3분 투자 상식</h1>"
             "<p>연금·투자·경제, 어렵게 느껴지던 것들을<br>가볍게 하나씩 풀어드려요.</p></div>"]
    for cat, cards in LEARN_CARDS:
        parts.append(f"<div class='cat'>{html.escape(cat)}</div>")
        for q, a in cards:
            parts.append(
                "<details class='qcard'><summary>"
                f"<span>{html.escape(q)}</span><span class='arw'>›</span></summary>"
                f"<div class='ans'>{html.escape(a)}</div></details>")
    parts.append("<div style='text-align:center'><a class='plink' href='/survey'>내 생각도 남기기 · 투자 설문 →</a></div>")
    parts.append("<div class='disc'><b>참고</b> 이 콘텐츠는 일반적인 개념을 쉽게 설명한 교육용 자료이며, "
                 "특정 상품 추천이나 투자 권유가 아닙니다. 투자 결정은 본인의 판단과 책임입니다.</div>")
    parts.append("<div class='foot'>교육용 참고 자료</div></div>")
    return public_page("3분 투자 상식", "".join(parts))


# 대중용 설문 (회의: '명분' 데이터 장치 · 상품/브랜드 언급 없음)
SURVEYS = [
    {"id": "start_age", "q": "연금·투자, 몇 살에 처음 시작하셨나요?",
     "options": ["20대 이전", "20대", "30대", "40대", "50대 이상", "아직 안 했어요"]},
    {"id": "monthly", "q": "매달 투자(적립)에 쓰는 금액은 얼마인가요?",
     "options": ["10만 원 미만", "10~30만 원", "30~50만 원", "50~100만 원", "100만 원 이상"]},
    {"id": "hesitate", "q": "투자를 망설이는 가장 큰 이유는?",
     "options": ["뭘 사야 할지 몰라서", "손실이 무서워서", "여윳돈이 없어서", "시간이 없어서", "이미 잘하고 있어서"]},
]


def _survey_tally(qid):
    rows = _rows("SELECT choice, COUNT(*) FROM survey_votes WHERE qid=? GROUP BY choice", (qid,))
    return {r[0]: r[1] for r in rows}


def _voted_set(request):
    return set((request.cookies.get("kdx_voted") or "").split(",")) - {""}


@app.get("/survey", response_class=HTMLResponse)
def survey_tool(request: Request):
    voted = _voted_set(request)
    parts = ["<div class='wrap'>",
             "<div class='hero'><h1>이런 거 궁금하지 않아요?</h1>"
             "<p>다들 어떻게 하고 있는지, 살짝 엿보기.<br>탭하면 바로 결과가 보여요.</p></div>"]
    for s in SURVEYS:
        qid = s["id"]
        parts.append(f"<div class='sq' data-qid='{qid}'><h3>{html.escape(s['q'])}</h3>")
        if qid in voted:
            parts.append(_survey_result_html(qid, s))
        else:
            parts.append("<div class='opts'>")
            for opt in s["options"]:
                parts.append(f"<button class='opt' data-qid='{qid}' data-choice=\"{html.escape(opt, quote=True)}\">{html.escape(opt)}</button>")
            parts.append("</div><div class='resultslot'></div>")
        parts.append("</div>")
    parts.append("<div class='disc'><b>참고</b> 이 설문은 방문자들의 응답을 익명으로 모아 보여주는 참고용 자료입니다. "
                 "투자 권유가 아니며, 특정 상품과 무관합니다.</div>")
    parts.append("<div class='foot'>익명 설문 · 참고용</div></div>")
    return public_page("투자 설문", "".join(parts), extra_head=f"<script>{SURVEY_JS}</script>")


def _survey_result_html(qid, s):
    tally = _survey_tally(qid)
    total = sum(tally.values()) or 0
    rows = []
    for opt in s["options"]:
        cnt = tally.get(opt, 0)
        pct = (cnt / total * 100) if total else 0
        rows.append(
            f"<div class='bar'><div class='fill' style='width:{pct:.0f}%'></div>"
            f"<span class='txt'>{html.escape(opt)}</span><span class='pct'>{pct:.0f}%</span></div>")
    return ("<div class='results'>" + "".join(rows)
            + f"<div class='total'>총 {total:,}명 참여</div>"
            + "<div class='voted'>✓ 응답해 주셔서 고마워요</div></div>")


SURVEY_JS = """
(function(){
  function esc(s){return (s||'').replace(/[&<>\"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c];});}
  document.addEventListener('click',function(e){
    var b=e.target.closest?e.target.closest('.opt'):null; if(!b) return;
    var qid=b.getAttribute('data-qid'), choice=b.getAttribute('data-choice');
    var card=b.closest('.sq'); var slot=card.querySelector('.resultslot'); var opts=card.querySelector('.opts');
    if(opts) opts.style.opacity='.4';
    fetch('/survey/vote',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({qid:qid,choice:choice})})
      .then(function(r){return r.json();}).then(function(j){
        if(!j.ok){ if(opts) opts.style.opacity='1'; return; }
        if(opts) opts.style.display='none';
        var total=j.total||0, html='';
        (j.options||[]).forEach(function(o){
          var pct=total?Math.round(o.count/total*100):0;
          html+="<div class='bar'><div class='fill' style='width:"+pct+"%'></div><span class='txt'>"+esc(o.label)+"</span><span class='pct'>"+pct+"%</span></div>";
        });
        html+="<div class='total'>총 "+total.toLocaleString()+"명 참여</div><div class='voted'>✓ 응답해 주셔서 고마워요</div>";
        if(slot) slot.innerHTML=html;
      }).catch(function(){ if(opts) opts.style.opacity='1'; });
  });
})();
"""


@app.post("/survey/vote")
async def survey_vote(req: Request):
    try:
        data = await req.json()
    except Exception:
        return JSONResponse({"ok": False}, status_code=400)
    qid = (data.get("qid") or "").strip()
    choice = (data.get("choice") or "").strip()
    s = next((x for x in SURVEYS if x["id"] == qid), None)
    if not s or choice not in s["options"]:
        return JSONResponse({"ok": False}, status_code=400)
    voted = _voted_set(req)
    if qid not in voted:
        con = _con()
        try:
            con.execute("INSERT INTO survey_votes(qid,choice,ts) VALUES(?,?,?)",
                        (qid, choice, datetime.now(KST).isoformat()))
            con.commit()
        except Exception:
            pass
        finally:
            con.close()
        voted.add(qid)
    tally = _survey_tally(qid)
    total = sum(tally.values())
    resp = JSONResponse({"ok": True, "total": total,
                         "options": [{"label": o, "count": tally.get(o, 0)} for o in s["options"]]})
    resp.set_cookie("kdx_voted", ",".join(sorted(voted)), max_age=60 * 60 * 24 * 180,
                    httponly=True, samesite="lax")
    return resp


@app.get("/", response_class=HTMLResponse)
def home():
    inner = (
        "<header class='mast'><p class='eyebrow'>KODEX Content Hub</p>"
        "<h1 class='title'>KODEX 시황 브리핑</h1>"
        "<p class='sub'>텔레그램 봇, 브리핑 아카이브, 숏폼 제작 기획을 한곳에서.</p>"
        f"<p class='maker'>제작자: {html.escape(MAKER)}</p></header>"
        "<div class='cards'>"
        "<a class='tile' href='/bot'><div class='tic'>💬</div>"
        "<h3>텔레그램 봇</h3><p>자동 브리핑 수신과 즉석 명령(/pm, /plan, /script) 사용법을 안내합니다.</p></a>"
        "<a class='tile' href='/archive'><div class='tic'>🗂</div>"
        "<h3>브리핑 아카이브</h3><p>매일 오전·오후 시황 브리핑을 날짜별로 모아 봅니다.</p></a>"
        "<a class='tile' href='/plan'><div class='tic'>🎬</div>"
        "<h3>제작 브리프</h3><p>밀고 싶은 상품·이슈를 넣으면 스토리 앵글·컴플·톤을 기획하고, 완성 스크립트까지 만듭니다.</p></a>"
        "<a class='tile' href='/check'><div class='tic'>🛡️</div>"
        "<h3>컴플 셀프체크</h3><p>대본·캡션을 넣으면 단정적 투자권유·수익 보장·미확인 인과 단정 등 위험 표현을 점검합니다.</p></a>"
        "<a class='tile' href='/report'><div class='tic'>📅</div>"
        "<h3>주간 리포트</h3><p>최근 7일 산출물·컴플 판정·집중 상품 흐름을 한 페이지로. 고객사 공유용.</p></a>"
        "<a class='tile' href='/data'><div class='tic'>📊</div>"
        "<h3>시황 데이터</h3><p>집중 상품의 최근 흐름을 공개 데이터 기반 그래프로 봅니다.</p></a>"
        "</div>"
        "<div class='dsectitle'>대중용 공개 도구 <span class='muted'>(비밀번호 없이 열림 · 외부 공유용)</span></div>"
        "<div class='cards'>"
        "<a class='tile' href='/dividend'><div class='tic'>💰</div>"
        "<h3>월 배당 계산기</h3><p>얼마 넣으면 매달 얼마? 상품명 없이 즉석 계산. 공유 링크: /dividend</p></a>"
        "<a class='tile' href='/learn'><div class='tic'>💡</div>"
        "<h3>3분 투자 상식</h3><p>연금·투자·경제를 쉽게 풀어주는 지식 카드. 공유 링크: /learn</p></a>"
        "<a class='tile' href='/survey'><div class='tic'>🗳️</div>"
        "<h3>투자 설문</h3><p>대중 응답을 모아 콘텐츠 명분·데이터로. 공유 링크: /survey</p></a>"
        "</div>"
        "<div class='dsectitle'>컴플라이언스</div>"
        "<div class='cards'>"
        "<a class='tile' href='/compliance'><div class='tic'>📚</div>"
        "<h3>컴플 판례집</h3><p>과거 점검한 대본을 판정·표현 유형별로 찾아 통과 사례를 참고합니다.</p></a>"
        "<a class='tile' href='/rules'><div class='tic'>📐</div>"
        "<h3>컴플 규칙 사전</h3><p>수익률·분배율·심사필 등 자주 걸리는 표현의 처리 기준.</p></a>"
        "</div>" + FOOT)
    return page("KODEX 시황 브리핑", inner, active="/")


@app.get("/bot", response_class=HTMLResponse)
def bot_guide():
    cmds = [
        ("/brief", "지금 오전형 브리핑(밤사이 미국장 + 전날 마감)을 받습니다."),
        ("/pm", "지금 오후형 브리핑(오늘 코스피 마감·장중 이벤트)을 받습니다."),
        ("/plan [상품/이슈]", "숏폼 제작 브리프를 만듭니다. 이 웹의 '제작 브리프'에서도 할 수 있습니다."),
        ("/script [이슈 + 상품]", "완성 숏폼 스크립트를 만듭니다. 이 웹에서도 만들 수 있습니다."),
        ("/check [대본·캡션]", "붙여넣은 문구의 컴플라이언스 위험 표현을 점검합니다. 이 웹의 '컴플 체크'에서도 할 수 있습니다."),
    ]
    cmd_html = "".join(
        f"<div class='cmd'><code>{html.escape(c)}</code><p>{html.escape(d)}</p></div>" for c, d in cmds)
    inner = (
        "<header class='mast'><p class='eyebrow'>Telegram Bot</p>"
        "<h1 class='title'>텔레그램 봇</h1>"
        "<p class='sub'>자동 브리핑을 받고, 즉석에서 브리핑·스크립트를 만드는 봇입니다.</p></header>"
        f"<a class='openbot' href='{html.escape(BOT_LINK)}' target='_blank' rel='noopener'>텔레그램 봇 열기</a>"
        "<div class='divider'>명령어</div>" + cmd_html +
        "<div class='divider'>사용법</div>"
        "<div class='cmd'><p>· 자동 브리핑은 공식 채널에 평일 오전 9시·오후 3시 45분에 올라옵니다. 채널을 구독해 두면 됩니다.</p>"
        "<p>· 즉석 명령(/brief, /pm, /plan, /script)은 봇과의 1:1 대화나 봇이 있는 그룹방에서 입력하세요. (채널에서는 명령을 쓸 수 없습니다.)</p>"
        "<p>· 예) <code>/script 마이크론 시총 1조 돌파, AI반도체TOP2플러스로</code></p></div>" + FOOT)
    return page("텔레그램 봇 안내", inner, active="/bot")


def search_archive(needles=None, type_="all", limit=80):
    """브리핑·브리프·스크립트를 키워드(needles: OR)로 검색해 최신순 통합 리스트로 반환."""
    needles = [n for n in (needles or []) if n]

    def where(cols):
        if not needles:
            return "", []
        parts, args = [], []
        for n in needles:
            parts.append("(" + " OR ".join(f"{c} LIKE ?" for c in cols) + ")")
            args += [f"%{n}%"] * len(cols)
        return " WHERE " + " OR ".join(parts), args

    out = []
    if type_ in ("all", "briefing"):
        w, a = where(["title", "body"])
        for bid, ts, kind, title in _rows(
                f"SELECT id, ts, kind, title FROM briefings{w} ORDER BY id DESC LIMIT ?", tuple(a + [limit])):
            out.append({"t": kind_label(kind), "tcls": (kind if kind in ("am", "pm") else "plan"),
                        "ts": ts, "title": (title or "(제목 없음)"), "url": f"/b/{bid}", "badge": ""})
    if type_ in ("all", "plan"):
        w, a = where(["request", "body"])
        for pid, ts, req in _rows(
                f"SELECT id, ts, request FROM plans{w} ORDER BY id DESC LIMIT ?", tuple(a + [limit])):
            out.append({"t": "제작 브리프", "tcls": "brief", "ts": ts,
                        "title": ((req or "(브리프)").strip()[:70]), "url": f"/plan/view/{pid}", "badge": ""})
    if type_ in ("all", "script"):
        w, a = where(["request", "body"])
        for sid, ts, req, vd in _rows(
                f"SELECT id, ts, request, check_verdict FROM scripts{w} ORDER BY id DESC LIMIT ?", tuple(a + [limit])):
            out.append({"t": "스크립트", "tcls": "script", "ts": ts,
                        "title": ((req or "(스크립트)").strip()[:70]), "url": f"/script/view/{sid}",
                        "badge": verdict_badge(vd)})
    out.sort(key=lambda x: x["ts"] or "", reverse=True)
    return out[:limit]


@app.get("/archive", response_class=HTMLResponse)
def archive(request: Request):
    q = (request.query_params.get("q") or "").strip()
    type_ = request.query_params.get("type") or "all"
    if type_ not in ("all", "briefing", "plan", "script"):
        type_ = "all"
    results = search_archive([q] if q else [], type_)

    def opt(val, label):
        return f"<option value='{val}'{' selected' if type_ == val else ''}>{label}</option>"
    form = (
        "<form method='get' action='/archive' class='searchbar'>"
        f"<input type='text' name='q' value='{html.escape(q)}' placeholder='키워드 검색 (제목·내용)' autocomplete='off'>"
        "<select name='type'>"
        + opt("all", "전체") + opt("briefing", "브리핑") + opt("plan", "브리프") + opt("script", "스크립트")
        + "</select><button type='submit' class='go'>검색</button></form>")
    parts = ["<header class='mast'><p class='eyebrow'>Archive</p>"
             "<h1 class='title'>아카이브</h1>"
             "<p class='sub'>브리핑·제작 브리프·완성 스크립트를 키워드와 종류로 찾아봅니다.</p></header>"
             + form]
    if q or type_ != "all":
        parts.append(f"<div class='reslabel'>검색 결과 {len(results)}건"
                     + (f" · ‘{html.escape(q)}’" if q else "") + "</div>")
    if not results:
        parts.append("<div class='empty'>결과가 없습니다. 다른 키워드로 검색해 보세요.</div>")
    else:
        for r in results:
            parts.append(
                f"<a class='rrow arow' href='{r['url']}'>"
                f"<span class='tchip {r['tcls']}'>{html.escape(r['t'])}</span>"
                f"<span class='atitle'>{html.escape(r['title'])}{r['badge']}</span>"
                f"<span class='rt'>{html.escape(fmt_time(r['ts']))}</span></a>")
    parts.append(FOOT)
    return page("아카이브", "".join(parts), active="/archive")


@app.get("/check", response_class=HTMLResponse)
def check_form():
    disabled = "" if CHECK_FN else "disabled"
    warn = "" if CHECK_FN else "<p class='sub' style='color:var(--am)'>컴플 체크 기능이 아직 연결되지 않았습니다.</p>"
    inner = (
        "<header class='mast'><p class='eyebrow'>Compliance Check</p>"
        "<h1 class='title'>컴플라이언스 셀프체크</h1>"
        "<p class='sub'>대본·캡션·문구를 넣으면 단정적 투자권유·수익 보장·미확인 인과 단정·"
        "수수료/위험등급/심사필 누락 같은 위험 표현을 짚어 드립니다. "
        "사전 점검용이며, 최종 판단은 삼성자산운용 준법 검수로 확정됩니다.</p></header>"
        + warn +
        "<div class='field'>"
        "<textarea id='creq' class='inp inp-tall' placeholder='점검할 대본이나 문구를 붙여넣으세요.'></textarea>"
        "<div class='checkwrap'>"
        f"<button class='go checkbtn' data-input='creq' {disabled}>컴플 체크 실행</button>"
        "<div class='checkstatus statusline'></div>"
        "<div class='checkresult'></div>"
        "</div></div>"
        "<div class='recent'><h3>컴플 참고</h3>"
        "<a class='rrow' href='/compliance'><span>컴플 판례집 — 과거 점검한 대본을 판정·표현 유형별로 찾기 →</span></a>"
        "<a class='rrow' href='/rules'><span>컴플 규칙 사전 — 수익률·분배율·심사필 등 표현 처리 기준 →</span></a></div>"
        + FOOT)
    return page("컴플라이언스 셀프체크", inner, active="/check", extra_head=f"<script>{CHECK_JS}</script>")


@app.get("/compliance", response_class=HTMLResponse)
def compliance_view(request: Request):
    verdict = request.query_params.get("verdict", "all")
    tag = request.query_params.get("tag", "all")
    if verdict not in ("all", "통과", "주의", "수정 필요"):
        verdict = "all"
    rows = list_checked(verdict, tag)

    def vchip(val, label):
        on = " on" if verdict == val else ""
        href = f"/compliance?verdict={quote(val)}&tag={quote(tag)}"
        return f"<a class='fchip{on}' href='{href}'>{html.escape(label)}</a>"

    def tchip2(val, label):
        on = " on" if tag == val else ""
        href = f"/compliance?verdict={quote(verdict)}&tag={quote(val)}"
        return f"<a class='fchip{on}' href='{href}'>{html.escape(label)}</a>"

    vfilter = "".join([vchip("all", "전체"), vchip("통과", "통과"),
                       vchip("주의", "주의"), vchip("수정 필요", "수정 필요")])
    tfilter = tchip2("all", "전체 유형") + "".join(tchip2(n, n) for n, _ in COMP_TAGS)
    parts = ["<header class='mast'><p class='eyebrow'>Compliance Archive</p>"
             "<h1 class='title'>컴플 판례집</h1>"
             "<p class='sub'>과거 점검한 대본을 판정과 표현 유형(수익률·월배당·분배율·커버드콜 등)으로 찾아, "
             "통과 사례의 표현 방식을 참고하세요.</p></header>"
             f"<div class='filterline'><span class='flab'>판정</span>{vfilter}</div>"
             f"<div class='filterline'><span class='flab'>표현 유형</span>{tfilter}</div>"]
    if not rows:
        parts.append("<div class='empty'>해당 조건의 점검 기록이 없습니다. "
                     "완성 스크립트 화면에서 '컴플 체크'를 실행하면 여기에 쌓입니다.</div>")
    else:
        parts.append(f"<div class='reslabel'>{len(rows)}건</div>")
        for sid, ts, req, vd, tags in rows:
            parts.append(
                f"<a class='rrow arow' href='/script/view/{sid}'>"
                f"<span class='atitle'>🎬 {html.escape((req or '(스크립트)').strip()[:44])}"
                f"{verdict_badge(vd)}</span>"
                f"<span class='rt'>{html.escape(fmt_time(ts))}</span></a>"
                + (f"<div class='ctags rowtags'>{''.join(f'<span class=ctag>{html.escape(t)}</span>' for t in tags.split(',') if t)}</div>" if tags else ""))
    parts.append(FOOT)
    return page("컴플 판례집", "".join(parts), active="/check")


# 컴플라이언스 규칙 사전 (내부 참고 · 회의 피드백 + 일반 ETF 광고 원칙 반영)
COMP_RULES = [
    ("수익률 표기", [
        "과거 수익률은 미래 수익을 보장하지 않는다는 점을 함께 명시한다.",
        "수익률을 쓸 때는 상장 이후(설정 이후) 수익률을 함께 표기하고, 기준일을 분명히 강조한다.",
        "특정 기간만 잘라 유리하게 보이게 하지 않는다. 누적·연평균 등 기준을 분명히 한다.",
    ]),
    ("배당·분배율·커버드콜", [
        "분배율·배당은 확정된 값이 아니며 변동·중단될 수 있음을 전제로 표현한다. 미래 분배를 보장하는 표현은 금지.",
        "커버드콜류는 분배 재원과 원금 훼손 가능성(분배가 원금에서 나올 수 있음)을 유의해서 다룬다.",
        "'월 배당', '월 O원' 같은 표현은 가정·예시임을 분명히 하고 특정 상품의 확정 수익처럼 보이지 않게 한다.",
    ]),
    ("원금·투자위험", [
        "예금자보호 대상이 아니며 원금손실(0~100%)이 가능하다는 유의문구를 갖춘다.",
        "투자위험등급, 자산가격·환율·신용등급 변동에 따른 위험을 누락하지 않는다.",
    ]),
    ("필수 고지·심사필", [
        "합성총보수·위험등급·증권거래비용 등 상품별로 바뀌는 수치는 임의로 쓰지 말고 [확인 필요]로 비운다.",
        "준법감시인 심사필은 정해진 형식(제 2026-000호, 기간)으로 두고 임의 번호를 만들지 않는다.",
        "광고 시점 기준이며 미래에는 달라질 수 있음을 밝힌다.",
    ]),
    ("표현·톤", [
        "단정적 투자권유('지금 사세요', '무조건 담아라')와 수익 보장('반드시 오른다') 표현은 금지.",
        "확인되지 않은 인과를 사실처럼 단정하지 않는다(예: 미확인 'OO 때문에 올랐다'). 완화하거나 근거·출처를 붙인다.",
        "'역대급', '지금이 마지막 기회' 같은 근거 없는 과장·최상급 표현은 피한다.",
    ]),
]


@app.get("/rules", response_class=HTMLResponse)
def rules_view():
    parts = ["<header class='mast'><p class='eyebrow'>Compliance Rules</p>"
             "<h1 class='title'>컴플 규칙 사전</h1>"
             "<p class='sub'>콘텐츠 제작 시 자주 걸리는 표현의 처리 기준을 정리했습니다. "
             "컴플 셀프체크와 판례집을 함께 쓰면 좋습니다. 최종 기준은 삼성자산운용 준법감시인 심사로 확정됩니다.</p></header>"]
    for cat, rules in COMP_RULES:
        items = "".join(f"<li>{html.escape(r)}</li>" for r in rules)
        parts.append(f"<section class='brief-sec'><h2 class='sec-h'><span class='sec-ic'>📌</span>{html.escape(cat)}</h2>"
                     f"<ul class='blist'>{items}</ul></section>")
    parts.append("<div class='recent'><h3>바로가기</h3>"
                 "<a class='rrow' href='/check'><span>이 대본·문구가 규칙에 맞는지 점검 →</span></a>"
                 "<a class='rrow' href='/compliance'><span>과거 통과 사례(판례집)에서 표현 방식 참고 →</span></a></div>")
    parts.append(FOOT)
    return page("컴플 규칙 사전", "".join(parts), active="/check")


@app.get("/report", response_class=HTMLResponse)
def report_view():
    c = report_counts()
    now = datetime.now(KST)
    start = now - timedelta(days=7)
    rng = f"{start.month}월 {start.day}일 ~ {now.month}월 {now.day}일"

    def card(label, big, sub):
        return (f"<div class='statcard'><div class='stbig'>{big}</div>"
                f"<div class='stlab'>{html.escape(label)}</div>"
                + (f"<div class='stsub'>{html.escape(sub)}</div>" if sub else "") + "</div>")

    stats = ("<div class='statgrid'>"
             + card("브리핑", str(c["am"] + c["pm"]), f"오전 {c['am']} · 오후 {c['pm']}")
             + card("제작 브리프", str(c["plans"]), "")
             + card("완성 스크립트", str(c["scripts"]), "")
             + card("컴플 체크", str(c["checks"]), "")
             + "</div>")

    v = c["verdicts"]
    verdict_html = ""
    if v:
        order = ["통과", "주의", "수정 필요"]
        chips = " ".join(f"{verdict_badge(k)} {v[k]}건" for k in order if k in v)
        verdict_html = ("<div class='rsec'><h3>이번 주 스크립트 컴플 판정</h3>"
                        f"<div class='vdist'>{chips}</div></div>")

    focus, _kospi, _comp = get_market()
    prows = []
    for item in focus:
        s = item.get("series") or []
        if len(s) < 2:
            continue
        base = s[max(0, len(s) - 6)]["close"]
        last = s[-1]["close"]
        wk = ((last / base - 1) * 100) if base else 0
        arrow = "▲" if wk > 0 else ("▼" if wk < 0 else "→")
        col = "up" if wk > 0 else ("down" if wk < 0 else "")
        prows.append(
            f"<div class='wprow'><span class='wpname'>{html.escape(item['name'])} "
            f"<span class='dcode'>{html.escape(item['code'])}</span></span>"
            f"<span class='wchg {col}'>{arrow} {abs(wk):.2f}% "
            f"<span class='muted'>({_fmt_num(last)}원)</span></span></div>")
    focus_html = ("<div class='rsec'><h3>집중 상품 주간 흐름 <span class='muted'>(최근 약 5거래일)</span></h3>"
                  + ("".join(prows) or "<p class='sub'>시세 데이터를 가져오지 못했습니다.</p>") + "</div>")

    cut = _week_cutoff_iso()
    briefs = [r for r in list_briefings(40) if r[1] and r[1] >= cut][:6]
    if briefs:
        brows = "".join(
            f"<a class='rrow' href='/b/{bid}'><span>{html.escape(kind_label(kind))} · "
            f"{html.escape((title or '(제목 없음)')[:48])}</span>"
            f"<span class='rt'>{html.escape(fmt_time(ts))}</span></a>"
            for bid, ts, ymd, kind, source, title in briefs)
        briefs_html = f"<div class='rsec'><h3>이번 주 브리핑</h3>{brows}</div>"
    else:
        briefs_html = ""

    srecent = [r for r in list_scripts(40) if r[1] and r[1] >= cut][:6]
    if srecent:
        srows = "".join(
            f"<a class='rrow' href='/script/view/{sid}'><span>🎬 "
            f"{html.escape((req or '(스크립트)').strip()[:46])}{verdict_badge(vd)}</span>"
            f"<span class='rt'>{html.escape(fmt_time(ts))}</span></a>"
            for sid, ts, req, vd in srecent)
        scripts_html = f"<div class='rsec'><h3>이번 주 스크립트</h3>{srows}</div>"
    else:
        scripts_html = ""

    inner = (
        "<header class='mast'><p class='eyebrow'>Weekly Report</p>"
        "<h1 class='title'>주간 리포트</h1>"
        f"<p class='sub'>{html.escape(rng)} · 최근 7일 활동 요약입니다. 고객사 공유용으로 이 링크를 그대로 전달하셔도 됩니다.</p></header>"
        + stats + verdict_html + focus_html + briefs_html + scripts_html + FOOT)
    return page("주간 리포트", inner, active="/report")


@app.get("/b/{bid}", response_class=HTMLResponse)
def brief_detail(bid: int):
    row = get_briefing(bid)
    if not row:
        return page("브리핑을 찾을 수 없음",
                    "<a class='back' href='/archive'>← 아카이브로</a>"
                    "<div class='empty'>해당 브리핑을 찾을 수 없습니다.</div>", active="/archive")
    _id, ts, ymd, kind, source, title, body = row
    k = kind if kind in ("am", "pm") else ""
    inner = ("<a class='back' href='/archive'>← 아카이브로</a>"
             f"<div class='meta'><span class='pill {k}'>{html.escape(kind_label(kind))}</span>"
             f"<span class='tag'>{html.escape(source_label(source))}</span></div>"
             f"<h1 class='dtitle'>{html.escape(title or '(제목 없음)')}</h1>"
             f"<div class='dmeta'>{html.escape(fmt_date(ymd, ts))} · {html.escape(fmt_time(ts))}</div>"
             f"<div class='body'>{html.escape(body or '')}</div>" + FOOT)
    return page(title or "브리핑", inner, active="/archive")


@app.get("/plan", response_class=HTMLResponse)
def plan_form():
    recents = list_plans()
    rec_html = ""
    if recents:
        rows = "".join(
            f"<a class='rrow' href='/plan/view/{pid}'>"
            f"<span>{html.escape((req or '').strip()[:60] or '(제목 없음)')}</span>"
            f"<span class='rt'>{html.escape(fmt_time(ts))}</span></a>"
            for pid, ts, req in recents)
        rec_html = f"<div class='recent'><h3>최근 생성한 브리프</h3>{rows}</div>"
    srecents = list_scripts()
    srec_html = ""
    if srecents:
        srows = "".join(
            f"<a class='rrow' href='/script/view/{sid}'>"
            f"<span>🎬 {html.escape((req or '').strip()[:56] or '(제목 없음)')}{verdict_badge(v)}</span>"
            f"<span class='rt'>{html.escape(fmt_time(ts))}</span></a>"
            for sid, ts, req, v in srecents)
        srec_html = f"<div class='recent'><h3>최근 만든 스크립트</h3>{srows}</div>"
    disabled = "" if PLAN_FN else "disabled"
    warn = "" if PLAN_FN else "<p class='sub' style='color:var(--am)'>제작 기능이 아직 연결되지 않았습니다.</p>"
    inner = (
        "<header class='mast'><p class='eyebrow'>Production Brief</p>"
        "<h1 class='title'>제작 브리프</h1>"
        "<p class='sub'>밀고 싶은 KODEX 상품이나 오늘의 이슈를 넣으면, "
        "스토리 앵글·경쟁사 차별점·컴플 체크·톤 가이드를 정리해 드립니다.</p></header>"
        + warn +
        "<div class='field'>"
        "<textarea id='req' class='inp' placeholder='예) KODEX 미국우주항공, 우주항공 테마 강세'></textarea>"
        f"<div><button id='go' class='go' {disabled}>브리프 생성</button></div>"
        "<div id='status' class='statusline'></div></div>" + rec_html + srec_html + FOOT)
    js = _poll_js("go", "req", "status", "/plan/new", "/plan/view/", "plan_id")
    return page("제작 브리프", inner, active="/plan", extra_head=f"<script>{js}</script>")


@app.post("/plan/new")
async def plan_new(req: Request):
    q, _data, err = await _read_req(req, PLAN_FN)
    if err:
        return err
    return _start_job(PLAN_FN, q, "plan_id", lambda text: _save_plan(q, text))


@app.post("/script/new")
async def script_new(req: Request):
    q, data, err = await _read_req(req, SCRIPT_FN)
    if err:
        return err
    plan_id = data.get("plan_id")
    try:
        plan_id = int(plan_id) if plan_id not in (None, "") else None
    except Exception:
        plan_id = None
    return _start_job(SCRIPT_FN, q, "script_id", lambda text: _save_script(q, text, plan_id))


async def _read_req(req, fn):
    try:
        data = await req.json()
    except Exception:
        return None, None, JSONResponse({"error": "잘못된 요청입니다."}, status_code=400)
    q = (data.get("request") or "").strip()
    if not q:
        return None, None, JSONResponse({"error": "내용을 입력해 주세요."}, status_code=400)
    if fn is None:
        return None, None, JSONResponse({"error": "이 기능이 연결되지 않았습니다."}, status_code=503)
    return q, data, None


def _start_job(fn, q, id_field, save_fn):
    job_id = uuid.uuid4().hex
    with LOCK:
        JOBS[job_id] = {"status": "pending"}
    threading.Thread(target=_run_job, args=(job_id, fn, q, id_field, save_fn), daemon=True).start()
    return JSONResponse({"job_id": job_id})


def _run_job(job_id, fn, q, id_field, save_fn):
    try:
        text = fn(q)
        new_id = save_fn(text)
        with LOCK:
            JOBS[job_id] = {"status": "done", id_field: new_id}
    except Exception as e:
        with LOCK:
            JOBS[job_id] = {"status": "error", "error": str(e)[:200]}


def _start_text_job(fn, text_in, sid=None, kind="check"):
    # 저장 없이 결과 HTML을 바로 돌려주는 잡. kind=check면 판정 저장, caption이면 캡션 렌더.
    job_id = uuid.uuid4().hex
    with LOCK:
        JOBS[job_id] = {"status": "pending"}
    threading.Thread(target=_run_text_job, args=(job_id, fn, text_in, sid, kind), daemon=True).start()
    return JSONResponse({"job_id": job_id})


def _run_text_job(job_id, fn, text_in, sid=None, kind="check"):
    try:
        text = fn(text_in)
        if kind == "caption":
            out = render_caption(text)
        else:
            if sid:
                save_check_result(sid, _extract_verdict(text), text, _detect_tags(text_in))
            out = render_check(text)
        with LOCK:
            JOBS[job_id] = {"status": "done", "html": out}
    except Exception as e:
        with LOCK:
            JOBS[job_id] = {"status": "error", "error": str(e)[:200]}


def _text_in_from(data):
    sid = data.get("script_id")
    if sid:
        try:
            row = get_script(int(sid))
        except Exception:
            row = None
        return (row[3] if row else ""), (int(sid) if sid else None)
    return (data.get("request") or "").strip(), None


@app.post("/check/run")
async def check_run(req: Request):
    try:
        data = await req.json()
    except Exception:
        return JSONResponse({"error": "잘못된 요청입니다."}, status_code=400)
    if CHECK_FN is None:
        return JSONResponse({"error": "컴플 체크 기능이 연결되지 않았습니다."}, status_code=503)
    text_in, sid = _text_in_from(data)
    if not text_in:
        return JSONResponse({"error": "점검할 내용이 없습니다."}, status_code=400)
    return _start_text_job(CHECK_FN, text_in, sid=sid, kind="check")


@app.post("/caption/run")
async def caption_run(req: Request):
    try:
        data = await req.json()
    except Exception:
        return JSONResponse({"error": "잘못된 요청입니다."}, status_code=400)
    if CAPTION_FN is None:
        return JSONResponse({"error": "캡션 생성 기능이 연결되지 않았습니다."}, status_code=503)
    text_in, _sid = _text_in_from(data)
    if not text_in:
        return JSONResponse({"error": "내용이 없습니다."}, status_code=400)
    return _start_text_job(CAPTION_FN, text_in, kind="caption")


@app.get("/job/{job_id}")
def job_status(job_id: str):
    with LOCK:
        j = JOBS.get(job_id)
    if not j:
        return JSONResponse({"status": "error", "error": "세션이 만료되었습니다. 다시 시도해 주세요."})
    return JSONResponse(j)


@app.get("/plan/view/{pid}", response_class=HTMLResponse)
def plan_view(pid: int):
    row = get_plan(pid)
    if not row:
        return page("브리프를 찾을 수 없음",
                    "<a class='back' href='/plan'>← 제작 브리프로</a>"
                    "<div class='empty'>해당 브리프를 찾을 수 없습니다.</div>", active="/plan")
    _id, ts, request, body = row
    prefill = html.escape((request or "").strip())
    sdisabled = "" if SCRIPT_FN else "disabled"
    made = scripts_for_plan(pid)
    made_html = ""
    if made:
        mrows = "".join(
            f"<a class='rrow' href='/script/view/{sid}'>"
            f"<span>🎬 {html.escape((sreq or '').strip()[:56] or '(스크립트)')}{verdict_badge(v)}</span>"
            f"<span class='rt'>{html.escape(fmt_time(sts))}</span></a>"
            for sid, sts, sreq, v in made)
        made_html = f"<div class='recent'><h3>이 브리프로 만든 스크립트</h3>{mrows}</div>"
    script_block = (
        "<section class='brief-sec'>"
        "<h2 class='sec-h'><span class='sec-ic'>🎥</span>이 브리프로 완성 스크립트 만들기</h2>"
        "<p class='sec-p'>원하는 앵글이나 방향을 적으면 40~60초 숏폼 스크립트를 웹에서 바로 만들어 드립니다.</p>"
        f"<textarea id='sreq' class='inp' placeholder='예) 1번 앵글로, KODEX 미국우주항공'>{prefill}</textarea>"
        f"<div><button id='sgo' class='go' {sdisabled}>스크립트 생성</button></div>"
        "<div id='sstatus' class='statusline'></div></section>")
    inner = ("<a class='back' href='/plan'>← 제작 브리프로</a>"
             f"<div class='dmeta' style='margin-top:14px'>입력: {html.escape((request or '').strip()[:120])}"
             f" · {html.escape(fmt_time(ts))}</div>"
             + render_brief(body) + script_block + made_html + FOOT)
    js = _poll_js("sgo", "sreq", "sstatus", "/script/new", "/script/view/", "script_id",
                  extra_payload=f", plan_id: {pid}")
    return page("제작 브리프 결과", inner, active="/plan", extra_head=f"<script>{js}</script>")


@app.get("/script/view/{sid}", response_class=HTMLResponse)
def script_view(sid: int):
    row = get_script(sid)
    if not row:
        return page("스크립트를 찾을 수 없음",
                    "<a class='back' href='/plan'>← 제작 브리프로</a>"
                    "<div class='empty'>해당 스크립트를 찾을 수 없습니다.</div>", active="/plan")
    _id, ts, request, body, plan_id, verdict, check_body, check_tags = row
    if plan_id:
        back = f"<a class='back' href='/plan/view/{plan_id}'>← 이 스크립트의 브리프로</a>"
    else:
        back = "<a class='back' href='/plan'>← 제작 브리프로</a>"
    tag_html = _tag_chips(check_tags)
    saved_check = (f"<div class='savedcheck'>최근 점검 결과 (저장됨)</div>{render_check(check_body)}"
                   if check_body else "")
    check_block = ""
    if CHECK_FN:
        check_block = (
            "<section class='brief-sec'>"
            "<h2 class='sec-h'><span class='sec-ic'>🛡️</span>컴플라이언스 셀프체크</h2>"
            "<p class='sec-p'>이 대본에 단정적 투자권유·수익 보장·미확인 인과 단정 등 위험 표현이 없는지 점검합니다. "
            "점검 결과와 판정은 저장되어 '컴플 판례'에서 유형별로 다시 찾아볼 수 있습니다. "
            "최종 판단은 삼성자산운용 준법 검수로 확정됩니다.</p>"
            + saved_check +
            "<div class='checkwrap'>"
            f"<button class='go checkbtn' data-sid='{sid}'>{'다시 점검' if check_body else '이 대본 컴플 체크'}</button>"
            "<div class='checkstatus statusline'></div>"
            "<div class='checkresult'></div>"
            "</div></section>")
    caption_block = ""
    if CAPTION_FN:
        caption_block = (
            "<section class='brief-sec'>"
            "<h2 class='sec-h'><span class='sec-ic'>#️⃣</span>업로드 캡션·해시태그</h2>"
            "<p class='sec-p'>이 대본으로 유튜브 쇼츠·릴스 설명란에 넣을 캡션과 해시태그를 만듭니다.</p>"
            "<div class='capwrap'>"
            f"<button class='go capbtn' data-sid='{sid}'>캡션·해시태그 생성</button>"
            "<div class='capstatus statusline'></div>"
            "<div class='capresult'></div>"
            "</div></section>")
    inner = (back +
             f"<h1 class='dtitle' style='margin-top:14px'>완성 스크립트{verdict_badge(verdict)}</h1>"
             f"<div class='dmeta'>입력: {html.escape((request or '').strip()[:120])} · {html.escape(fmt_time(ts))}</div>"
             + tag_html + render_script(body) + check_block + caption_block + FOOT)
    return page("완성 스크립트", inner, active="/plan",
                extra_head=f"<script>{CHECK_JS}</script><script>{CAPTION_JS}</script>")


@app.get("/data", response_class=HTMLResponse)
def data_view():
    focus, kospi, comp = get_market()
    news = get_all_news()
    parts = ["<header class='mast'><p class='eyebrow'>Market Data</p>"
             "<h1 class='title'>시황 데이터</h1>"
             "<p class='sub'>집중 상품의 최근 흐름과 오늘의 시황 숏폼 소재입니다. 공개 데이터(네이버금융) 기준.</p></header>"]
    if kospi and len(kospi) >= 2:
        parts.append("<div class='dsectitle'>코스피 지수</div>")
        parts.append(_data_card("코스피", "KOSPI", kospi, "gk", news_slot=False))
    if focus:
        parts.append("<div class='dsectitle'>집중 상품</div>")
        got = False
        for idx, item in enumerate(focus):
            parts.append(_data_card(item["name"], item["code"], item.get("series") or [],
                                    f"g{idx}", competitor=comp.get(item["code"]),
                                    news=news.get(item["code"]),
                                    detail_url=f"/product/{item['code']}"))
            got = got or len(item.get("series") or []) >= 2
        if not got and not (kospi and len(kospi) >= 2):
            parts.append("<div class='empty'>시세 데이터를 가져오지 못했습니다. 잠시 후 새로고침해 주세요.</div>")
    else:
        parts.append("<div class='empty'>표시할 집중 상품이 없습니다.</div>")
    parts.append(FOOT)
    return page("시황 데이터", "".join(parts), active="/data", extra_head=f"<script>{DATA_JS}</script>")


@app.get("/product/{code}", response_class=HTMLResponse)
def product_view(code: str):
    prod = next((p for p in settings.FOCUS_PRODUCTS if p["code"] == code), None)
    if not prod:
        return page("상품을 찾을 수 없음",
                    "<a class='back' href='/data'>← 시황 데이터로</a>"
                    "<div class='empty'>집중 상품 목록에 없는 종목코드입니다.</div>", active="/data")
    name = prod["name"]
    focus, _kospi, comp = get_market()
    item = next((f for f in focus if f["code"] == code), None)
    series = (item.get("series") if item else []) or []
    news = get_all_news().get(code)
    card = _data_card(name, code, series, "gp", competitor=comp.get(code), news=news)

    related = search_archive([code, name], "all", 60)
    if related:
        rrows = "".join(
            f"<a class='rrow arow' href='{r['url']}'>"
            f"<span class='tchip {r['tcls']}'>{html.escape(r['t'])}</span>"
            f"<span class='atitle'>{html.escape(r['title'])}{r['badge']}</span>"
            f"<span class='rt'>{html.escape(fmt_time(r['ts']))}</span></a>"
            for r in related)
        related_html = f"<div class='rsec'><h3>이 상품 관련 콘텐츠 <span class='muted'>({len(related)}건)</span></h3>{rrows}</div>"
    else:
        related_html = ("<div class='rsec'><h3>이 상품 관련 콘텐츠</h3>"
                        "<div class='empty'>아직 이 상품으로 만든 브리프·스크립트가 없습니다. "
                        "제작 브리프에서 만들어 보세요.</div></div>")

    inner = (
        "<a class='back' href='/data'>← 시황 데이터로</a>"
        f"<header class='mast' style='margin-top:12px'><p class='eyebrow'>Product</p>"
        f"<h1 class='title'>{html.escape(name)}</h1>"
        f"<p class='sub'>종목코드 {html.escape(code)} · 최근 흐름, 오늘의 시황 소재, 관련 콘텐츠를 한곳에서.</p></header>"
        + card + related_html
        + "<div style='margin-top:16px'>"
        f"<a class='go' style='display:inline-block;text-decoration:none' href='/plan'>이 상품으로 제작 브리프 만들기</a></div>"
        + FOOT)
    return page(name, inner, active="/data", extra_head=f"<script>{DATA_JS}</script>")
