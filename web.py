"""
KODEX 시황 브리핑 — 웹 아카이브 + 제작 브리프 (통합 홈)
- 하나의 홈(/)에서 세 메뉴로: 아카이브(/archive), 제작 브리프(/plan), 데이터(/data, 준비중).
- bot.py와 같은 SQLite DB를 공유한다. DB 경로와 '브리프 생성 함수'를 configure()로 주입받는다.
- 공개 페이지지만 noindex/robots로 검색엔진 노출은 막는다.
"""

import re
import html
import uuid
import sqlite3
import threading
from datetime import datetime, timezone, timedelta

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

DB_PATH = None      # bot.py가 주입
PLAN_FN = None      # bot.py가 주입 (요청문 -> 브리프 텍스트)
KST = timezone(timedelta(hours=9))
WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]

JOBS = {}           # 제작 브리프 생성 작업 상태 (메모리)
LOCK = threading.Lock()


def configure(db_path, plan_fn=None):
    global DB_PATH, PLAN_FN
    DB_PATH = str(db_path)
    PLAN_FN = plan_fn


# ================= DB =================
def _con():
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS briefings(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, ymd TEXT,
        kind TEXT, source TEXT, title TEXT, body TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS plans(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, request TEXT, body TEXT)""")
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


def save_plan(request, body):
    con = _con()
    try:
        cur = con.execute("INSERT INTO plans(ts,request,body) VALUES(?,?,?)",
                          (datetime.now(KST).isoformat(), request, body))
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


# ================= 제작 브리프 텍스트 -> 읽기 좋은 HTML =================
_HEADERS = ["🎬", "📌", "🎯", "🧩", "⚠️", "⚠", "🕐", "▶"]


def _match_header(line):
    s = line.strip()
    for h in _HEADERS:
        if s.startswith(h):
            return h
    return None


def _maybe_label(text):
    # "라벨: 내용" 형태면 라벨을 굵게 (짧고 문장부호가 없을 때만)
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
            cur = ["", "", []]
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


# ================= HTML 뼈대 =================
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
  line-height:1.62;-webkit-font-smoothing:antialiased}
.topbar{position:sticky;top:0;z-index:5;background:rgba(255,255,255,.92);
  backdrop-filter:saturate(180%) blur(8px);border-bottom:1px solid var(--line)}
.topbar .in{max-width:760px;margin:0 auto;display:flex;align-items:center;
  gap:14px;padding:11px 18px}
.brand{font-weight:800;font-size:15px;letter-spacing:-.01em;text-decoration:none;color:var(--ink)}
.nav{display:flex;gap:4px;margin-left:auto;flex-wrap:wrap}
.nav a{font-family:var(--mono);font-size:12px;color:var(--muted);text-decoration:none;
  padding:5px 9px;border-radius:8px}
.nav a:hover{color:var(--ink);background:var(--bg)}
.nav a.on{color:var(--accent);background:var(--pm-bg)}
.wrap{max-width:760px;margin:0 auto;padding:0 18px 72px}
header.mast{padding:30px 0 16px;border-bottom:2px solid var(--ink);margin-bottom:8px}
.eyebrow{font-family:var(--mono);font-size:12px;letter-spacing:.14em;
  text-transform:uppercase;color:var(--muted);margin:0 0 6px}
h1.title{font-size:30px;font-weight:800;letter-spacing:-.01em;margin:0}
.sub{color:var(--muted);font-size:14px;margin:6px 0 0}
.cards{display:grid;gap:12px;margin-top:22px}
a.tile{display:block;text-decoration:none;color:inherit;background:var(--surface);
  border:1px solid var(--line);border-radius:14px;padding:18px 18px;
  transition:transform .12s ease,box-shadow .12s ease}
a.tile:hover{transform:translateY(-2px);box-shadow:0 8px 22px rgba(16,20,24,.07)}
a.tile.soon{opacity:.62}
.tile .tic{font-size:22px}
.tile h3{margin:8px 0 4px;font-size:17px;font-weight:700}
.tile p{margin:0;color:var(--muted);font-size:13.5px}
.tile .badge{float:right;font-family:var(--mono);font-size:10px;color:var(--muted);
  border:1px solid var(--line);border-radius:999px;padding:2px 8px}
.datehead{font-family:var(--mono);font-size:13px;color:var(--muted);
  letter-spacing:.02em;margin:26px 0 10px;display:flex;align-items:center;gap:10px}
.datehead::after{content:"";flex:1;height:1px;background:var(--line)}
a.card{display:block;text-decoration:none;color:inherit;background:var(--surface);
  border:1px solid var(--line);border-left:3px solid var(--line);border-radius:12px;
  padding:14px 16px;margin:0 0 10px;transition:transform .12s ease,box-shadow .12s ease}
a.card:hover{transform:translateY(-1px);box-shadow:0 6px 18px rgba(16,20,24,.06)}
a.card.am{border-left-color:var(--am)} a.card.pm{border-left-color:var(--pm)}
.meta{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.pill{font-family:var(--mono);font-size:11px;font-weight:600;padding:2px 8px;border-radius:999px}
.pill.am{color:var(--am);background:var(--am-bg)} .pill.pm{color:var(--pm);background:var(--pm-bg)}
.tag{font-family:var(--mono);font-size:11px;color:var(--muted)}
.time{font-family:var(--mono);font-size:11px;color:var(--muted);margin-left:auto}
.ctitle{font-size:15px;font-weight:600;line-height:1.45;margin:0}
.empty{background:var(--surface);border:1px dashed var(--line);border-radius:12px;
  padding:26px 18px;color:var(--muted);text-align:center;margin-top:20px}
.empty code{font-family:var(--mono);background:var(--bg);padding:2px 6px;border-radius:6px;color:var(--ink)}
a.back{display:inline-block;font-family:var(--mono);font-size:13px;color:var(--accent);
  text-decoration:none;margin:18px 0 4px}
a.back:hover{text-decoration:underline}
.dtitle{font-size:22px;font-weight:800;line-height:1.4;margin:14px 0 6px}
.dmeta{font-family:var(--mono);font-size:12px;color:var(--muted);margin-bottom:16px}
.body{background:var(--surface);border:1px solid var(--line);border-radius:12px;
  padding:18px;white-space:pre-wrap;word-break:break-word;font-size:15px}
/* 제작 브리프 렌더 */
.brief-sec{background:var(--surface);border:1px solid var(--line);border-radius:14px;
  padding:16px 18px;margin:0 0 14px}
.sec-h{display:flex;align-items:center;gap:9px;font-size:15px;font-weight:700;
  margin:0 0 10px;padding-bottom:9px;border-bottom:1px solid var(--line)}
.sec-ic{font-size:16px}
.sec-p{margin:0 0 8px;font-size:14.5px;color:#28303a}
ul.blist{margin:6px 0 0;padding-left:18px} ul.blist li{margin:4px 0;font-size:14px}
li.plain{list-style:none;margin-left:-18px}
.angle{background:var(--bg);border:1px solid var(--line);border-radius:10px;
  padding:12px 14px;margin:10px 0}
.angle-h{font-weight:700;font-size:14.5px;display:flex;gap:8px;align-items:baseline}
.anum{font-family:var(--mono);color:var(--accent);font-weight:700}
/* 제작 브리프 입력 */
.field{margin-top:8px}
textarea#req{width:100%;min-height:96px;resize:vertical;padding:13px 14px;font-size:15px;
  font-family:var(--sans);border:1px solid var(--line);border-radius:12px;background:var(--surface);color:var(--ink)}
textarea#req:focus{outline:none;border-color:var(--accent)}
button#go{margin-top:10px;background:var(--accent);color:#fff;border:0;border-radius:10px;
  font-size:15px;font-weight:600;padding:12px 20px;cursor:pointer}
button#go:disabled{opacity:.55;cursor:default}
#status{margin-top:14px;font-size:14px;color:var(--muted);display:flex;align-items:center;gap:10px;min-height:22px}
.spin{width:16px;height:16px;border:2px solid var(--line);border-top-color:var(--accent);
  border-radius:50%;animation:sp .8s linear infinite;display:inline-block}
@keyframes sp{to{transform:rotate(360deg)}}
.recent{margin-top:30px}
.recent h3{font-family:var(--mono);font-size:12px;letter-spacing:.06em;text-transform:uppercase;
  color:var(--muted);margin:0 0 8px}
a.rrow{display:flex;gap:10px;text-decoration:none;color:inherit;padding:11px 14px;background:var(--surface);
  border:1px solid var(--line);border-radius:10px;margin-bottom:8px;font-size:14px}
a.rrow:hover{border-color:var(--accent)}
a.rrow .rt{font-family:var(--mono);font-size:11px;color:var(--muted);white-space:nowrap}
footer{margin-top:34px;padding-top:14px;border-top:1px solid var(--line);font-size:12px;color:var(--muted)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:6px}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
"""

FONT = ('<link rel="stylesheet" '
        'href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable.min.css">')


def _nav(active):
    items = [("/", "홈"), ("/archive", "아카이브"), ("/plan", "제작 브리프"), ("/data", "데이터")]
    links = "".join(
        f"<a href='{href}' class='{'on' if active == href else ''}'>{html.escape(label)}</a>"
        for href, label in items)
    return ("<div class='topbar'><div class='in'>"
            "<a class='brand' href='/'>KODEX 시황</a>"
            f"<nav class='nav'>{links}</nav></div></div>")


def page(title, inner, active="", extra_head=""):
    return HTMLResponse(
        "<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<meta name='robots' content='noindex, nofollow'>"
        "<meta name='theme-color' content='#0B4EA2'>"
        f"<title>{html.escape(title)}</title>{FONT}<style>{CSS}</style>{extra_head}</head>"
        f"<body>{_nav(active)}<div class='wrap'>{inner}</div></body></html>")


FOOT = ("<footer>본 페이지는 콘텐츠 기획 참고용입니다. "
        "모든 수치·주가·뉴스는 사용 전 원문 및 준법 확인이 필요합니다.</footer>")


# ================= 라우트 =================
@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return "User-agent: *\nDisallow: /\n"


@app.get("/", response_class=HTMLResponse)
def home():
    inner = (
        "<header class='mast'>"
        "<p class='eyebrow'>KODEX Content Hub</p>"
        "<h1 class='title'>KODEX 시황 브리핑</h1>"
        "<p class='sub'>브리핑 아카이브와 숏폼 제작 기획을 한곳에서.</p>"
        "</header>"
        "<div class='cards'>"
        "<a class='tile' href='/archive'><div class='tic'>🗂</div>"
        "<h3>브리핑 아카이브</h3><p>매일 오전·오후 시황 브리핑을 날짜별로 모아 봅니다.</p></a>"
        "<a class='tile' href='/plan'><div class='tic'>🎬</div>"
        "<h3>제작 브리프</h3><p>밀고 싶은 상품·이슈를 넣으면 스토리 앵글·컴플·톤을 기획합니다.</p></a>"
        "<a class='tile soon' href='/data'><span class='badge'>준비 중</span><div class='tic'>📊</div>"
        "<h3>시황 데이터</h3><p>집중 상품 흐름을 그래프로 보는 화면을 준비하고 있습니다.</p></a>"
        "</div>" + FOOT)
    return page("KODEX 시황 브리핑", inner, active="/")


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
            f"<span class='rt' style='margin-left:auto'>{html.escape(fmt_time(ts))}</span></a>"
            for pid, ts, req in recents)
        rec_html = f"<div class='recent'><h3>최근 생성한 브리프</h3>{rows}</div>"
    disabled = "" if PLAN_FN else "disabled"
    warn = "" if PLAN_FN else "<p class='sub' style='color:var(--am)'>제작 기능이 아직 연결되지 않았습니다.</p>"
    inner = (
        "<header class='mast'><p class='eyebrow'>Production Brief</p>"
        "<h1 class='title'>제작 브리프</h1>"
        "<p class='sub'>밀고 싶은 KODEX 상품이나 오늘의 이슈를 넣으면, "
        "스토리 앵글·경쟁사 차별점·컴플 체크·톤 가이드를 정리해 드립니다.</p></header>"
        + warn +
        "<div class='field'>"
        "<textarea id='req' placeholder='예) KODEX 미국우주항공, 우주항공 테마 강세'></textarea>"
        f"<div><button id='go' {disabled}>브리프 생성</button></div>"
        "<div id='status'></div></div>" + rec_html + FOOT)
    return page("제작 브리프", inner, active="/plan", extra_head=f"<script>{PLAN_JS}</script>")


PLAN_JS = """
document.addEventListener('DOMContentLoaded', function(){
  var go=document.getElementById('go'), req=document.getElementById('req'), st=document.getElementById('status');
  if(!go) return;
  go.addEventListener('click', function(){
    var v=(req.value||'').trim();
    if(!v){ st.textContent='상품명이나 이슈를 입력해 주세요.'; return; }
    go.disabled=true;
    st.innerHTML="<span class='spin'></span><span>브리프를 생성 중입니다… 웹 검색 포함 30초~1분 걸립니다. 이 화면을 닫지 마세요.</span>";
    fetch('/plan/new',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request:v})})
      .then(function(r){return r.json();})
      .then(function(j){ if(!j.job_id){throw new Error(j.error||'요청 실패');} poll(j.job_id); })
      .catch(function(e){ go.disabled=false; st.textContent='오류: '+e.message; });
  });
  function poll(id){
    var t=setInterval(function(){
      fetch('/plan/status/'+id).then(function(r){return r.json();}).then(function(j){
        if(j.status==='done'){ clearInterval(t); window.location='/plan/view/'+j.plan_id; }
        else if(j.status==='error'){ clearInterval(t); go.disabled=false; st.textContent='생성 중 오류: '+(j.error||'알 수 없음'); }
      }).catch(function(){});
    }, 3000);
  }
});
"""


@app.post("/plan/new")
async def plan_new(req: Request):
    try:
        data = await req.json()
    except Exception:
        return JSONResponse({"error": "잘못된 요청입니다."}, status_code=400)
    q = (data.get("request") or "").strip()
    if not q:
        return JSONResponse({"error": "상품명이나 이슈를 입력해 주세요."}, status_code=400)
    if PLAN_FN is None:
        return JSONResponse({"error": "제작 기능이 연결되지 않았습니다."}, status_code=503)
    job_id = uuid.uuid4().hex
    with LOCK:
        JOBS[job_id] = {"status": "pending"}
    threading.Thread(target=_run_plan, args=(job_id, q), daemon=True).start()
    return JSONResponse({"job_id": job_id})


def _run_plan(job_id, q):
    try:
        text = PLAN_FN(q)
        pid = save_plan(q, text)
        with LOCK:
            JOBS[job_id] = {"status": "done", "plan_id": pid}
    except Exception as e:
        with LOCK:
            JOBS[job_id] = {"status": "error", "error": str(e)[:200]}


@app.get("/plan/status/{job_id}")
def plan_status(job_id: str):
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
    inner = ("<a class='back' href='/plan'>← 제작 브리프로</a>"
             f"<div class='dmeta' style='margin-top:14px'>입력: {html.escape((request or '').strip()[:120])}"
             f" · {html.escape(fmt_time(ts))}</div>"
             + render_brief(body) + FOOT)
    return page("제작 브리프 결과", inner, active="/plan")


@app.get("/data", response_class=HTMLResponse)
def data_placeholder():
    inner = ("<header class='mast'><p class='eyebrow'>Market Data</p>"
             "<h1 class='title'>시황 데이터</h1>"
             "<p class='sub'>집중 상품 흐름을 그래프로 보는 화면입니다.</p></header>"
             "<div class='empty'>준비 중입니다. 곧 공개 데이터 기반 시황 차트가 들어갑니다.</div>" + FOOT)
    return page("시황 데이터", inner, active="/data")
