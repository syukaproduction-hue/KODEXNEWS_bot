"""
KODEX 시황 브리핑 — 웹 아카이브 (첫 메뉴)
- bot.py와 같은 SQLite DB(briefings 테이블)를 읽어, 저장된 브리핑을 날짜별로 보여준다.
- bot.py가 이 모듈을 백그라운드 스레드로 함께 실행한다. DB 경로는 configure()로 주입받는다.
- 공개 페이지지만 noindex/robots로 검색엔진 노출은 막는다.
"""

import html
import sqlite3
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, PlainTextResponse

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

DB_PATH = None  # bot.py가 configure()로 채운다
WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]


def configure(db_path):
    global DB_PATH
    DB_PATH = str(db_path)


# ---------- DB 읽기 (연결은 요청마다 열고 닫는다) ----------
def _rows(sql, args=()):
    if not DB_PATH:
        return []
    try:
        con = sqlite3.connect(DB_PATH)
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()
    except Exception:
        return []  # 테이블이 아직 없으면 빈 목록으로 처리


def list_briefings(limit=300):
    return _rows(
        "SELECT id, ts, ymd, kind, source, title FROM briefings ORDER BY id DESC LIMIT ?",
        (limit,))


def get_briefing(bid):
    r = _rows(
        "SELECT id, ts, ymd, kind, source, title, body FROM briefings WHERE id=?",
        (bid,))
    return r[0] if r else None


# ---------- 표시용 포맷 ----------
def fmt_time(ts):
    try:
        dt = datetime.fromisoformat(ts)
        return f"{dt.strftime('%H:%M')}"
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


# ---------- HTML 뼈대 ----------
CSS = """
:root{
  --ink:#101418; --bg:#F5F6F8; --surface:#FFFFFF; --line:#E4E7EC;
  --muted:#697586; --accent:#0B4EA2; --am:#B45309; --am-bg:#FBEEDD;
  --pm:#0B4EA2; --pm-bg:#E7EEF8;
  --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
  --sans:"Pretendard Variable",Pretendard,-apple-system,"Apple SD Gothic Neo","Noto Sans KR","Malgun Gothic",sans-serif;
}
*{box-sizing:border-box}
html,body{margin:0}
body{background:var(--bg);color:var(--ink);font-family:var(--sans);
  line-height:1.62;-webkit-font-smoothing:antialiased;padding:0 18px 64px}
.wrap{max-width:720px;margin:0 auto}
header.mast{padding:34px 0 18px;border-bottom:2px solid var(--ink);margin-bottom:8px}
.eyebrow{font-family:var(--mono);font-size:12px;letter-spacing:.14em;
  text-transform:uppercase;color:var(--muted);margin:0 0 6px}
h1.title{font-size:30px;font-weight:800;letter-spacing:-.01em;margin:0}
.sub{color:var(--muted);font-size:14px;margin:6px 0 0}
.datehead{font-family:var(--mono);font-size:13px;color:var(--muted);
  letter-spacing:.02em;margin:26px 0 10px;display:flex;align-items:center;gap:10px}
.datehead::after{content:"";flex:1;height:1px;background:var(--line)}
a.card{display:block;text-decoration:none;color:inherit;background:var(--surface);
  border:1px solid var(--line);border-left:3px solid var(--line);border-radius:12px;
  padding:14px 16px;margin:0 0 10px;transition:transform .12s ease,box-shadow .12s ease}
a.card:hover{transform:translateY(-1px);box-shadow:0 6px 18px rgba(16,20,24,.06)}
a.card.am{border-left-color:var(--am)}
a.card.pm{border-left-color:var(--pm)}
.meta{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.pill{font-family:var(--mono);font-size:11px;font-weight:600;padding:2px 8px;border-radius:999px}
.pill.am{color:var(--am);background:var(--am-bg)}
.pill.pm{color:var(--pm);background:var(--pm-bg)}
.tag{font-family:var(--mono);font-size:11px;color:var(--muted)}
.time{font-family:var(--mono);font-size:11px;color:var(--muted);margin-left:auto}
.ctitle{font-size:15px;font-weight:600;line-height:1.45;margin:0}
.empty{background:var(--surface);border:1px dashed var(--line);border-radius:12px;
  padding:26px 18px;color:var(--muted);text-align:center;margin-top:20px}
.empty code{font-family:var(--mono);background:var(--bg);padding:2px 6px;border-radius:6px;color:var(--ink)}
a.back{display:inline-block;font-family:var(--mono);font-size:13px;color:var(--accent);
  text-decoration:none;margin:22px 0 4px}
a.back:hover{text-decoration:underline}
.dtitle{font-size:21px;font-weight:800;line-height:1.4;margin:8px 0 4px}
.dmeta{font-family:var(--mono);font-size:12px;color:var(--muted);margin-bottom:16px}
.body{background:var(--surface);border:1px solid var(--line);border-radius:12px;
  padding:18px 18px;white-space:pre-wrap;word-break:break-word;font-size:15px}
footer{margin-top:34px;padding-top:14px;border-top:1px solid var(--line);
  font-size:12px;color:var(--muted)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:6px}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""

FONT = ('<link rel="stylesheet" '
        'href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable.min.css">')


def page(title, inner):
    return HTMLResponse(
        "<!doctype html><html lang='ko'><head>"
        "<meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<meta name='robots' content='noindex, nofollow'>"
        "<meta name='theme-color' content='#0B4EA2'>"
        f"<title>{html.escape(title)}</title>"
        f"{FONT}<style>{CSS}</style></head>"
        f"<body><div class='wrap'>{inner}</div></body></html>")


# ---------- 라우트 ----------
@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return "User-agent: *\nDisallow: /\n"


@app.get("/", response_class=HTMLResponse)
def index():
    rows = list_briefings()
    parts = [
        "<header class='mast'>",
        "<p class='eyebrow'>Briefing Archive</p>",
        "<h1 class='title'>KODEX 시황 브리핑</h1>",
        "<p class='sub'>매일 오전·오후 브리핑을 모아 둔 아카이브입니다.</p>",
        "</header>",
    ]
    if not rows:
        parts.append(
            "<div class='empty'>아직 저장된 브리핑이 없습니다.<br>"
            "봇에서 <code>/brief</code> 를 보내면 여기에 쌓입니다.</div>")
    else:
        cur = None
        for bid, ts, ymd, kind, source, title in rows:
            if ymd != cur:
                cur = ymd
                parts.append(f"<div class='datehead'>{html.escape(fmt_date(ymd, ts))}</div>")
            k = kind if kind in ("am", "pm") else ""
            parts.append(
                f"<a class='card {k}' href='/b/{bid}'>"
                f"<div class='meta'>"
                f"<span class='pill {k}'>{html.escape(kind_label(kind))}</span>"
                f"<span class='tag'>{html.escape(source_label(source))}</span>"
                f"<span class='time'>{html.escape(fmt_time(ts))}</span>"
                f"</div>"
                f"<p class='ctitle'>{html.escape(title or '(제목 없음)')}</p>"
                f"</a>")
    parts.append("<footer>본 페이지는 콘텐츠 기획 참고용 아카이브입니다. "
                 "모든 수치·주가·뉴스는 사용 전 원문 및 준법 확인이 필요합니다.</footer>")
    return page("KODEX 시황 브리핑 아카이브", "".join(parts))


@app.get("/b/{bid}", response_class=HTMLResponse)
def detail(bid: int):
    row = get_briefing(bid)
    if not row:
        inner = ("<a class='back' href='/'>← 목록으로</a>"
                 "<div class='empty'>해당 브리핑을 찾을 수 없습니다.</div>")
        return page("브리핑을 찾을 수 없음", inner)
    _id, ts, ymd, kind, source, title, body = row
    k = kind if kind in ("am", "pm") else ""
    inner = (
        "<a class='back' href='/'>← 목록으로</a>"
        f"<div class='meta'>"
        f"<span class='pill {k}'>{html.escape(kind_label(kind))}</span>"
        f"<span class='tag'>{html.escape(source_label(source))}</span></div>"
        f"<h1 class='dtitle'>{html.escape(title or '(제목 없음)')}</h1>"
        f"<div class='dmeta'>{html.escape(fmt_date(ymd, ts))} · {html.escape(fmt_time(ts))}</div>"
        f"<div class='body'>{html.escape(body or '')}</div>"
        "<footer>본 페이지는 콘텐츠 기획 참고용 아카이브입니다. "
        "모든 수치·주가·뉴스는 사용 전 원문 및 준법 확인이 필요합니다.</footer>")
    return page(title or "브리핑", inner)
