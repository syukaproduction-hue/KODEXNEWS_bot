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
BOT_LINK = "https://t.me/kodex_economy"
MAKER = "주식회사 슈카친구들"
KST = timezone(timedelta(hours=9))
WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]

# 접근 비밀번호(게이트). Railway에 WEB_PASSWORD를 넣으면 그 값이 우선, 없으면 기본 'KODEX'.
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "KODEX")
AUTH_TOKEN = hashlib.sha256(("kdx:" + WEB_PASSWORD).encode()).hexdigest()
AUTH_COOKIE = "kdx_auth"
GATE_EXEMPT = {"/login", "/login/auth", "/robots.txt", "/logo.svg"}

_BASE = Path(__file__).parent
LOGO_PATH = _BASE / "logo_kodex_ko.svg"
_LOGO_CACHE = None

JOBS = {}
LOCK = threading.Lock()


def configure(db_path, plan_fn=None, script_fn=None, check_fn=None):
    global DB_PATH, PLAN_FN, SCRIPT_FN, CHECK_FN
    DB_PATH = str(db_path)
    PLAN_FN = plan_fn
    SCRIPT_FN = script_fn
    CHECK_FN = check_fn


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
        check_verdict TEXT, check_at TEXT)""")
    for col, typ in (("plan_id", "INTEGER"), ("check_verdict", "TEXT"), ("check_at", "TEXT")):
        try:
            con.execute(f"ALTER TABLE scripts ADD COLUMN {col} {typ}")  # 기존 테이블 대비
        except Exception:
            pass
    con.execute("""CREATE TABLE IF NOT EXISTS product_news(
        code TEXT PRIMARY KEY, title TEXT, url TEXT, comp_name TEXT, comp_code TEXT, updated_at TEXT)""")
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
    r = _rows("SELECT id, ts, request, body, plan_id, check_verdict FROM scripts WHERE id=?", (sid,))
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


def save_check_verdict(sid, verdict):
    if not sid or not verdict:
        return
    con = _con()
    try:
        con.execute("UPDATE scripts SET check_verdict=?, check_at=? WHERE id=?",
                    (verdict, datetime.now(KST).isoformat(), int(sid)))
        con.commit()
    except Exception:
        pass
    finally:
        con.close()


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


def _data_card(name, code, series, gid, competitor=None, news=None, news_slot=True):
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
    return f"<div class='dcard'>{body}{comp_note}{newsblock}</div>"


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


@app.get("/archive", response_class=HTMLResponse)
def archive():
    rows = list_briefings()
    parts = ["<header class='mast'><p class='eyebrow'>Briefing Archive</p>"
             "<h1 class='title'>브리핑 아카이브</h1>"
             "<p class='sub'>매일 오전·오후 브리핑을 모아 둔 아카이브입니다.</p></header>"]
    if not rows:
        parts.append("<div class='empty'>아직 저장된 브리핑이 없습니다.<br>"
                     "봇에서 <code>/brief</code> 를 보내면 여기에 쌓입니다.</div>")
    else:
        cur = None
        for bid, ts, ymd, kind, source, title in rows:
            if ymd != cur:
                cur = ymd
                parts.append(f"<div class='datehead'>{html.escape(fmt_date(ymd, ts))}</div>")
            k = kind if kind in ("am", "pm") else ""
            parts.append(
                f"<a class='card {k}' href='/b/{bid}'><div class='meta'>"
                f"<span class='pill {k}'>{html.escape(kind_label(kind))}</span>"
                f"<span class='tag'>{html.escape(source_label(source))}</span>"
                f"<span class='time'>{html.escape(fmt_time(ts))}</span></div>"
                f"<p class='ctitle'>{html.escape(title or '(제목 없음)')}</p></a>")
    parts.append(FOOT)
    return page("브리핑 아카이브", "".join(parts), active="/archive")


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
        "</div></div>" + FOOT)
    return page("컴플라이언스 셀프체크", inner, active="/check", extra_head=f"<script>{CHECK_JS}</script>")


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


def _start_text_job(fn, text_in, sid=None):
    # 저장 없이 결과 HTML을 바로 돌려주는 잡 (컴플 체크용). sid가 있으면 판정을 스크립트에 기록.
    job_id = uuid.uuid4().hex
    with LOCK:
        JOBS[job_id] = {"status": "pending"}
    threading.Thread(target=_run_text_job, args=(job_id, fn, text_in, sid), daemon=True).start()
    return JSONResponse({"job_id": job_id})


def _run_text_job(job_id, fn, text_in, sid=None):
    try:
        text = fn(text_in)
        if sid:
            save_check_verdict(sid, _extract_verdict(text))
        with LOCK:
            JOBS[job_id] = {"status": "done", "html": render_check(text)}
    except Exception as e:
        with LOCK:
            JOBS[job_id] = {"status": "error", "error": str(e)[:200]}


@app.post("/check/run")
async def check_run(req: Request):
    try:
        data = await req.json()
    except Exception:
        return JSONResponse({"error": "잘못된 요청입니다."}, status_code=400)
    if CHECK_FN is None:
        return JSONResponse({"error": "컴플 체크 기능이 연결되지 않았습니다."}, status_code=503)
    sid = data.get("script_id")
    if sid:
        try:
            row = get_script(int(sid))
        except Exception:
            row = None
        text_in = row[3] if row else ""
    else:
        text_in = (data.get("request") or "").strip()
    if not text_in:
        return JSONResponse({"error": "점검할 내용이 없습니다."}, status_code=400)
    return _start_text_job(CHECK_FN, text_in, sid=(int(sid) if sid else None))


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
    _id, ts, request, body, plan_id, verdict = row
    if plan_id:
        back = f"<a class='back' href='/plan/view/{plan_id}'>← 이 스크립트의 브리프로</a>"
    else:
        back = "<a class='back' href='/plan'>← 제작 브리프로</a>"
    check_block = ""
    if CHECK_FN:
        check_block = (
            "<section class='brief-sec'>"
            "<h2 class='sec-h'><span class='sec-ic'>🛡️</span>컴플라이언스 셀프체크</h2>"
            "<p class='sec-p'>이 대본에 단정적 투자권유·수익 보장·미확인 인과 단정 등 위험 표현이 없는지 점검합니다. "
            "최종 판단은 삼성자산운용 준법 검수로 확정됩니다.</p>"
            "<div class='checkwrap'>"
            f"<button class='go checkbtn' data-sid='{sid}'>이 대본 컴플 체크</button>"
            "<div class='checkstatus statusline'></div>"
            "<div class='checkresult'></div>"
            "</div></section>")
    inner = (back +
             f"<h1 class='dtitle' style='margin-top:14px'>완성 스크립트{verdict_badge(verdict)}</h1>"
             f"<div class='dmeta'>입력: {html.escape((request or '').strip()[:120])} · {html.escape(fmt_time(ts))}</div>"
             + render_script(body) + check_block + FOOT)
    return page("완성 스크립트", inner, active="/plan", extra_head=f"<script>{CHECK_JS}</script>")


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
                                    news=news.get(item["code"])))
            got = got or len(item.get("series") or []) >= 2
        if not got and not (kospi and len(kospi) >= 2):
            parts.append("<div class='empty'>시세 데이터를 가져오지 못했습니다. 잠시 후 새로고침해 주세요.</div>")
    else:
        parts.append("<div class='empty'>표시할 집중 상품이 없습니다.</div>")
    parts.append(FOOT)
    return page("시황 데이터", "".join(parts), active="/data", extra_head=f"<script>{DATA_JS}</script>")
