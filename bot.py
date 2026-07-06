"""
KODEX 시황 뉴스봇
- 평일 오전 9시: 밤사이 미국장 + 전날 마감 브리핑
- 평일 오후 3:30: 오늘 한국장 마감 + 장중 이벤트/소재 후보 브리핑
- /brief, /pm : 즉시 브리핑(오전형/오후형)
- /script <이슈+상품> : 숏폼 스크립트
- /start : 구독 등록 (이걸 눌러야 자동 발송 명단에 들어감)
- /stop : 구독 해지
- /stats : (운영자 전용) 구독자 수·사용량·추정 비용
- /chatid : 채팅 ID 확인
설정은 settings.py, 브리핑 방식은 *_prompt.md 에서 수정합니다.
"""

import os
import re
import json
import asyncio
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, time
from pathlib import Path

import pytz
import requests
from anthropic import Anthropic
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ChatMemberHandler,
    ContextTypes,
)

import settings
import market_data

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("kodex-bot")

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
# 운영자(마케터) 본인 ID. /stats 권한용. Railway Variables에 ADMIN_CHAT_ID로 넣으면 됨.
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "").strip()
# 자동 브리핑을 발송할 채널 ID. Railway Variables에 TARGET_CHANNEL_ID로 넣음 (-100... 형태).
TARGET_CHANNEL_ID = os.environ.get("TARGET_CHANNEL_ID", "").strip()

TZ = pytz.timezone(settings.TIMEZONE)
BASE = Path(__file__).parent
PROMPT_AM = BASE / "briefing_prompt.md"
PROMPT_PM = BASE / "briefing_pm_prompt.md"
SCRIPT_PROMPT = BASE / "script_prompt.md"
BRIEF_PROMPT = BASE / "brief_prompt.md"

# DB 위치: Railway 볼륨을 /data 에 연결하면 영구 보존됨. 없으면 로컬 파일로 동작.
DB_DIR = Path(os.environ.get("DATA_DIR", "/data"))
try:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH = DB_DIR / "kodex_bot.db"
except Exception:
    DB_PATH = BASE / "kodex_bot.db"  # 볼륨 없을 때 임시(재배포 시 초기화될 수 있음)

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)
WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]
TG_LIMIT = 4096


# ===================== DB =====================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS subscribers(
        chat_id TEXT PRIMARY KEY, name TEXT, joined_at TEXT, active INTEGER DEFAULT 1)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS usage_log(
        ts TEXT, chat_id TEXT, kind TEXT, in_tok INTEGER, out_tok INTEGER)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS briefings(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, ymd TEXT,
        kind TEXT, source TEXT, title TEXT, body TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS plans(
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, request TEXT, body TEXT)""")
    return conn


def add_subscriber(chat_id, name):
    conn = db()
    conn.execute(
        "INSERT INTO subscribers(chat_id,name,joined_at,active) VALUES(?,?,?,1) "
        "ON CONFLICT(chat_id) DO UPDATE SET active=1, name=excluded.name",
        (str(chat_id), name, datetime.now(TZ).isoformat()),
    )
    conn.commit(); conn.close()


def remove_subscriber(chat_id):
    conn = db()
    conn.execute("UPDATE subscribers SET active=0 WHERE chat_id=?", (str(chat_id),))
    conn.commit(); conn.close()


def active_subscribers():
    conn = db()
    rows = conn.execute("SELECT chat_id FROM subscribers WHERE active=1").fetchall()
    conn.close()
    return [r[0] for r in rows]


def log_usage(chat_id, kind, in_tok, out_tok):
    conn = db()
    conn.execute(
        "INSERT INTO usage_log(ts,chat_id,kind,in_tok,out_tok) VALUES(?,?,?,?,?)",
        (datetime.now(TZ).isoformat(), str(chat_id), kind, int(in_tok), int(out_tok)),
    )
    conn.commit(); conn.close()


def save_briefing(kind, source, text):
    # 생성된 브리핑을 아카이브용으로 저장. kind='am'/'pm', source='auto'/'manual'.
    # 저장 실패가 브리핑 발송을 막으면 안 되므로 여기서 예외를 삼킨다.
    try:
        conn = db()
        now = datetime.now(TZ)
        title = (text.split("\n", 1)[0] or "").strip()[:200]
        conn.execute(
            "INSERT INTO briefings(ts,ymd,kind,source,title,body) VALUES(?,?,?,?,?,?)",
            (now.isoformat(), now.strftime("%Y-%m-%d"), kind, source, title, text),
        )
        conn.commit(); conn.close()
    except Exception:
        log.exception("save_briefing failed")


def save_plan(request, body):
    # 제작 브리프(/plan 결과)를 웹 '제작 브리프' 화면용으로 저장. 웹판과 동일 스키마.
    try:
        conn = db()
        conn.execute(
            "INSERT INTO plans(ts,request,body) VALUES(?,?,?)",
            (datetime.now(TZ).isoformat(), (request or "")[:1000], body),
        )
        conn.commit(); conn.close()
    except Exception:
        log.exception("save_plan failed")


def month_stats():
    conn = db()
    prefix = datetime.now(TZ).strftime("%Y-%m")
    sub = conn.execute("SELECT COUNT(*) FROM subscribers WHERE active=1").fetchone()[0]
    rows = conn.execute(
        "SELECT kind, COUNT(*), COALESCE(SUM(in_tok),0), COALESCE(SUM(out_tok),0) "
        "FROM usage_log WHERE ts LIKE ? GROUP BY kind", (prefix + "%",)
    ).fetchall()
    conn.close()
    return sub, rows


# ===================== 코스피 종가 조회 (네이버금융) =====================
def fetch_kospi_close():
    """네이버금융에서 코스피 현재가·등락을 읽어 한 줄 문장으로 반환. 실패하면 None."""
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://m.stock.naver.com/"}
    endpoints = [
        "https://polling.finance.naver.com/api/realtime/domestic/index/KOSPI",
        "https://m.stock.naver.com/api/index/KOSPI/basic",
    ]
    for url in endpoints:
        try:
            r = requests.get(url, headers=headers, timeout=8)
            if r.status_code != 200:
                continue
            data = r.json()
            # 두 엔드포인트의 응답 구조가 달라 모두 대응
            node = data
            if isinstance(data, dict) and "datas" in data:
                node = data["datas"][0]
            close = (node.get("closePrice") or node.get("nv") or node.get("now"))
            change = (node.get("compareToPreviousClosePrice") or node.get("cv"))
            rate = (node.get("fluctuationsRatio") or node.get("cr"))
            sign = node.get("compareToPreviousPrice")
            if isinstance(sign, dict):
                sign = sign.get("text", "")
            if not close:
                continue
            close_s = str(close).replace(",", "")
            change_s = str(change).replace(",", "") if change is not None else ""
            arrow = ""
            try:
                cv = float(change_s)
                arrow = "▲" if cv > 0 else ("▼" if cv < 0 else "보합")
                change_s = f"{abs(cv):,.2f}"
            except Exception:
                pass
            rate_s = f" ({rate}%)" if rate not in (None, "") else ""
            return f"코스피 {float(close_s):,.2f} {arrow}{change_s}{rate_s} (출처: 네이버금융, 한국거래소 기준)"
        except Exception:
            continue
    return None


# ===================== 텍스트 정리 =====================
def clean_for_telegram(text: str) -> str:
    text = text.replace("**", "").replace("__", "").replace("`", "")
    out = []
    for raw in text.split("\n"):
        line = raw.rstrip()
        line = re.sub(r"^\s*#{1,6}\s*", "", line)
        line = re.sub(r"^\s*>\s?", "", line)
        line = re.sub(r"^(\s*)[*+]\s+", r"\1· ", line)
        line = re.sub(r"\*([^*\n]+)\*", r"\1", line)
        if re.fullmatch(r"\s*(-{3,}|\*{3,}|_{3,})\s*", line):
            continue
        if re.fullmatch(r"\s*[-·*•]\s*", line):
            continue
        out.append(line)
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = _join_broken_lines(text)
    for marker in ("☀", "🌆"):
        i = text.find(marker)
        if i > 0:
            text = text[i:]
            break
    return text.strip()


# 문장 중간에서 끊긴 줄을 자연스럽게 이어붙인다.
def _join_broken_lines(text: str) -> str:
    lines = text.split("\n")
    # 새 블록의 시작으로 볼 패턴: 항목기호(·-•), 번호(1.), 이모지/대괄호 머리말, 빈 줄
    starts = re.compile(r"^\s*(·|-|•|\d+\.|\[|☀|🌆|📈|📉|🎯|🔔|🗂|🏢|🚩|🌙|⚠)")
    # 문장이 끝났다고 볼 종결: 마침표/물음표/느낌표/콜론/따옴표/괄호 등
    ends_ok = re.compile(r'[.!?:;,)\]"”』」\d]$')
    result = []
    for ln in lines:
        s = ln.rstrip()
        if (result and s.strip() and result[-1].strip()
                and not starts.match(s) and not ends_ok.search(result[-1].rstrip())):
            # 이전 줄이 문장 중간에서 끊겼고, 이 줄이 새 항목이 아니면 이어붙임
            result[-1] = result[-1].rstrip() + " " + s.strip()
        else:
            result.append(s)
    return "\n".join(result)


# ===================== 날짜/상품 컨텍스트 =====================
def build_date_context(pm: bool = False) -> str:
    today = datetime.now(TZ).date()
    wd = today.weekday()
    today_str = f"{today} ({WEEKDAY_KR[wd]})"
    if pm:
        return f"오늘은 {today_str}이고 한국 증시 마감 직후다. 오늘 장중에 발생한 이슈를 중심으로 정리한다."
    if wd == 0:
        fri = today - timedelta(days=3)
        return (f"오늘은 {today_str} 아침이다. 직전 영업일은 {fri}(금)이며 주말 미국장·해외 이슈도 함께 다룬다. "
                f"헤더는 '{today} ({WEEKDAY_KR[wd]}) 아침 · 미국 시장 정리'로 표기한다. '마감 기준'이라는 표현은 쓰지 않는다.")
    prev = today - timedelta(days=1)
    return (f"오늘은 {today_str} 아침이다. 밤사이 미국 증시와 직전 한국장({prev}, {WEEKDAY_KR[prev.weekday()]}) 흐름을 정리한다. "
            f"헤더는 '{today} ({WEEKDAY_KR[wd]}) 아침 · 미국 시장 정리'로 표기한다. '마감 기준'이라는 표현은 쓰지 않는다.")


def build_products_block() -> str:
    lines = ["## 현재 집중 상품 (이 목록과 연결되는 이슈만 소재 후보로)"]
    code_to_name = {}
    for p in settings.FOCUS_PRODUCTS:
        lines.append(f"- {p['name']} ({p['code']})")
        code_to_name[p["code"]] = p["name"]
    if settings.PRODUCT_SETS:
        lines.append("\n## 연동 상품 세트 (함께 비교 제시)")
        for s in settings.PRODUCT_SETS:
            names = " + ".join(f"{code_to_name.get(c, c)}({c})" for c in s["members"])
            lines.append(f"- {names} → {s['note']}")
    lines.append(f"\n하루 소재 후보 개수: {settings.CANDIDATE_COUNT}개")
    return "\n".join(lines)


# ===================== Claude 호출 =====================
def _call(system_text, user_text):
    resp = anthropic_client.messages.create(
        model=settings.MODEL,
        max_tokens=settings.MAX_TOKENS,
        system=system_text,
        messages=[{"role": "user", "content": user_text}],
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
    )
    parts = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
    text = clean_for_telegram("\n".join(p for p in parts if p).strip())
    in_tok = getattr(resp.usage, "input_tokens", 0) or 0
    out_tok = getattr(resp.usage, "output_tokens", 0) or 0
    return (text or "결과가 비어 있습니다. 잠시 후 다시 시도해 주세요."), in_tok, out_tok


def generate_brief_sync(pm: bool = False):
    prompt_file = PROMPT_PM if pm else PROMPT_AM
    system_text = (prompt_file.read_text(encoding="utf-8") + "\n\n"
                   + build_products_block() + "\n\n## 오늘 날짜 안내\n" + build_date_context(pm))
    if pm:
        kospi = fetch_kospi_close()
        if kospi:
            system_text += ("\n\n## 코스피 마감 (확정 데이터 — 이 값을 '오늘 코스피 마감' 첫 줄에 그대로 쓸 것)\n"
                            + kospi)
        else:
            system_text += ("\n\n## 코스피 마감\n확정 종가 데이터를 가져오지 못했다. "
                            "추측하지 말고 '오늘 코스피 마감' 항목을 통째로 생략한다.")
    else:
        notable = market_data.notable_focus_products()
        if notable:
            system_text += (
                "\n\n## 어제 눈에 띈 집중 상품 (공개 데이터 · 네이버금융 기준)\n"
                "아래는 직전 영업일 집중 상품 시세다. '📊 어제 집중 상품 움직임' 항목과 "
                "소재 후보 판단에 참고한다. 출처는 '네이버금융 시세'로 표기한다. "
                "집중 상품을 임의로 바꾸거나 투자권유로 쓰지 않는다.\n" + notable)
    user = "오늘의 KODEX 장 마감 브리핑을 작성해줘." if pm else "오늘의 KODEX 시황 브리핑을 작성해줘."
    return _call(system_text, user)


def generate_script_sync(req: str):
    system_text = SCRIPT_PROMPT.read_text(encoding="utf-8") + "\n\n" + build_products_block()
    return _call(system_text, f"다음 이슈로 숏폼 스크립트를 써줘:\n{req}")


def generate_plan_sync(req: str):
    system_text = BRIEF_PROMPT.read_text(encoding="utf-8") + "\n\n" + build_products_block()
    return _call(system_text, f"다음 상품/이슈로 숏폼 제작 브리프를 작성해줘:\n{req}")


def web_generate_plan(req: str):
    # 웹 '제작 브리프' 화면에서 호출. 텍스트만 반환하고 사용량은 여기서 기록한다.
    text, itok, otok = generate_plan_sync(req)
    log_usage("WEB", "plan", itok, otok)
    return text


# ===================== 발송 =====================
async def send_long(bot, chat_id, text: str):
    if len(text) <= TG_LIMIT:
        await bot.send_message(chat_id=chat_id, text=text); return
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > TG_LIMIT:
            if chunk:
                await bot.send_message(chat_id=chat_id, text=chunk)
            chunk = ""
        chunk = (chunk + "\n" + line) if chunk else line
    if chunk:
        await bot.send_message(chat_id=chat_id, text=chunk)


WELCOME = (
    "KODEX 시황 뉴스봇입니다.\n\n"
    "[자동 브리핑 — 채널에서]\n"
    "1. 오전 9시: 밤사이 미국장 + 전날 마감\n"
    "2. 오후 3:30: 오늘 한국장 마감 + 장중 이벤트·소재 후보\n"
    "→ 자동 브리핑은 공식 채널에 평일 오전·오후로 올라옵니다. 채널을 구독해 두시면 됩니다.\n\n"
    "[직접 명령 — 여기(1:1) 또는 팀 그룹방에서]\n"
    "채널에서는 명령을 쓸 수 없어요. 아래 명령은 봇과의 1:1 대화나 봇을 추가한 그룹방에서 사용하세요.\n"
    "· /plan KODEX AI전력핵심설비  (제작 브리프: 스토리·컴플·톤 기획)\n"
    "· /script 마이크론 시총 1조 돌파, 미국AI반도체TOP3플러스로  (숏폼 스크립트 작성)\n"
    "· /brief  (지금 오전형 브리핑 받기)\n"
    "· /pm  (지금 오후형 장중 브리핑 받기)\n"
)


# ===================== 명령 핸들러 =====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "자동 브리핑은 공식 채널에서 발송됩니다. 받지 않으시려면 채널을 나가시면 돼요.\n"
        "이 봇과의 1:1 대화에서는 /script, /brief, /pm 명령을 계속 쓸 수 있습니다.")


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"이 채팅 ID: {update.effective_chat.id}")


async def cmd_marketdata(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 운영자 전용: 공개 데이터(네이버금융) 조회가 정상인지 확인하는 진단 명령.
    if ADMIN_CHAT_ID and str(update.effective_chat.id) != ADMIN_CHAT_ID:
        await update.message.reply_text("이 명령은 운영자만 사용할 수 있어요.")
        return
    await update.message.reply_text("공개 데이터(네이버금융)로 집중 상품 시세를 확인 중입니다…")
    try:
        block = await asyncio.to_thread(market_data.notable_focus_products)
    except Exception as e:
        log.exception("marketdata failed")
        await update.message.reply_text(f"조회 중 오류가 발생했습니다: {e}")
        return
    if not block:
        await update.message.reply_text(
            "집중 상품 시세 데이터를 가져오지 못했습니다.\n"
            "종목코드가 맞는지, 네이버금융 응답이 정상인지 확인이 필요합니다.")
        return
    await update.message.reply_text(
        "집중 상품 전일 시세 (등락률 절댓값 큰 순 · 네이버금융 기준)\n\n" + block
        + "\n\n※ 진단용입니다. 이 숫자가 네이버금융 화면과 맞는지 확인되면 오전 브리핑에 연결합니다.")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ADMIN_CHAT_ID and str(update.effective_chat.id) != ADMIN_CHAT_ID:
        await update.message.reply_text("이 명령은 운영자만 사용할 수 있어요.")
        return
    sub, rows = month_stats()
    total_in = sum(r[2] for r in rows)
    total_out = sum(r[3] for r in rows)
    cost = (total_in / 1_000_000 * settings.PRICE_INPUT_PER_MTOK
            + total_out / 1_000_000 * settings.PRICE_OUTPUT_PER_MTOK)
    month = datetime.now(TZ).strftime("%Y-%m")
    lines = [f"📊 {month} 사용 현황 (청구 참고용)", ""]
    if rows:
        for kind, cnt, itok, otok in rows:
            lines.append(f"· {kind}: {cnt}회 (입력 {itok:,} / 출력 {otok:,} 토큰)")
    else:
        lines.append("· 이번 달 호출 기록 없음")
    lines += ["", f"· 추정 토큰: 입력 {total_in:,} / 출력 {total_out:,}",
              f"· 추정 API 비용: 약 ${cost:,.2f} (참고용)", "",
              "정확한 청구액은 Anthropic Console Usage 및 Railway Usage 기준으로 확인하세요."]
    await update.message.reply_text("\n".join(lines))


async def cmd_script(update: Update, context: ContextTypes.DEFAULT_TYPE):
    req = " ".join(context.args).strip()
    if not req:
        await update.message.reply_text(
            "사용법: /script 다음에 이슈와 원하는 상품을 적어주세요.\n"
            "예) /script 마이크론 시총 1조 돌파, 미국AI반도체TOP3플러스로 숏폼")
        return
    await update.message.reply_text("스크립트를 작성 중입니다… (웹 검색 포함, 30초~1분 소요)")
    try:
        text, itok, otok = await asyncio.to_thread(generate_script_sync, req)
        log_usage(update.effective_chat.id, "script", itok, otok)
        await send_long(context.bot, update.effective_chat.id, text)
    except Exception as e:
        log.exception("script failed")
        await update.message.reply_text(f"스크립트 생성 중 오류가 발생했습니다: {e}")


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    req = " ".join(context.args).strip()
    if not req:
        await update.message.reply_text(
            "사용법: /plan 다음에 밀고 싶은 상품(과 오늘 이슈)을 적어주세요.\n"
            "예) /plan KODEX AI전력핵심설비\n"
            "→ 스토리 앵글·영상 방향·컴플 체크·톤 가이드를 기획해 드립니다. "
            "마음에 드는 앵글은 /script 로 완성 스크립트를 만드세요.")
        return
    await update.message.reply_text("제작 브리프를 작성 중입니다… (웹 검색 포함, 30초~1분 소요)")
    try:
        text, itok, otok = await asyncio.to_thread(generate_plan_sync, req)
        log_usage(update.effective_chat.id, "plan", itok, otok)
        save_plan(req, text)
        await send_long(context.bot, update.effective_chat.id, text)
    except Exception as e:
        log.exception("plan failed")
        await update.message.reply_text(f"제작 브리프 생성 중 오류가 발생했습니다: {e}")


async def _brief_cmd(update, context, pm):
    label = "오후 장중" if pm else "오전"
    await update.message.reply_text(f"{label} 브리핑을 작성 중입니다… (웹 검색 포함, 30초~1분 소요)")
    try:
        text, itok, otok = await asyncio.to_thread(generate_brief_sync, pm)
        log_usage(update.effective_chat.id, "pm" if pm else "brief", itok, otok)
        save_briefing("pm" if pm else "am", "manual", text)
        await send_long(context.bot, update.effective_chat.id, text)
    except Exception as e:
        log.exception("brief failed")
        await update.message.reply_text(f"브리핑 생성 중 오류가 발생했습니다: {e}")


async def cmd_brief(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _brief_cmd(update, context, pm=False)


async def cmd_pm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _brief_cmd(update, context, pm=True)


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    status = update.my_chat_member.new_chat_member.status
    if status in ("member", "administrator"):
        log.info("봇이 채팅에 추가됨: id=%s type=%s title=%s status=%s",
                 chat.id, chat.type, getattr(chat, "title", ""), status)
        # 채널/그룹에 추가되면 자동 발송 설정에 필요한 ID를 알려준다.
        guide = (
            f"봇이 추가되었습니다.\n"
            f"이 채팅 ID: {chat.id}\n\n"
            f"자동 9시·15:30 브리핑을 이 채널로 받으려면, "
            f"Railway의 TARGET_CHANNEL_ID 값을 위 ID로 설정하세요. "
            f"(봇이 이 채널의 관리자이고 '메시지 게시' 권한이 있어야 합니다.)")
        try:
            await context.bot.send_message(chat_id=chat.id, text=guide)
        except Exception:
            # 채널은 권한 전이라 전송이 막힐 수 있음 → 로그(id=...)로 확인
            pass


# ===================== 자동 발송 =====================
async def broadcast(context, pm):
    today = datetime.now(TZ).date()
    if today.weekday() >= 5:
        log.info("주말이므로 자동 발송 건너뜀"); return
    if not TARGET_CHANNEL_ID:
        log.warning("TARGET_CHANNEL_ID 미설정 → 자동 발송 대상 없음"); return
    try:
        text, itok, otok = await asyncio.to_thread(generate_brief_sync, pm)
    except Exception:
        log.exception("auto brief gen failed"); return
    log_usage("AUTO", "pm" if pm else "brief", itok, otok)
    save_briefing("pm" if pm else "am", "auto", text)
    try:
        await send_long(context.bot, TARGET_CHANNEL_ID, text)
        log.info("자동 %s 브리핑 채널 발송 완료", "오후" if pm else "오전")
    except Exception as e:
        log.exception("채널 발송 실패")
        if ADMIN_CHAT_ID:
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=f"⚠️ 채널 자동 발송 실패: {e}\n봇이 채널 관리자인지, TARGET_CHANNEL_ID가 맞는지 확인하세요.")
            except Exception:
                pass


async def job_am(context: ContextTypes.DEFAULT_TYPE):
    await broadcast(context, pm=False)


async def job_pm(context: ContextTypes.DEFAULT_TYPE):
    await broadcast(context, pm=True)


def start_web():
    # 웹(홈/아카이브/제작 브리프)을 백그라운드 스레드에서 실행한다.
    # import를 여기서 해서, fastapi/uvicorn 미설치나 web.py 누락 시에도 봇은 계속 돈다.
    try:
        import uvicorn
        import web
        web.configure(DB_PATH, web_generate_plan)
        port = int(os.environ.get("PORT", "8080"))
        config = uvicorn.Config(web.app, host="0.0.0.0", port=port, log_level="warning")
        log.info("웹 서버 시작: 0.0.0.0:%s", port)
        uvicorn.Server(config).run()  # 비 메인 스레드 → uvicorn이 시그널 핸들러를 설치하지 않음
    except Exception:
        log.exception("웹 서버를 시작하지 못했습니다 — 봇은 계속 실행됩니다")


def main():
    if os.environ.get("ENABLE_WEB", "1") != "0":
        threading.Thread(target=start_web, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("marketdata", cmd_marketdata))
    app.add_handler(CommandHandler("brief", cmd_brief))
    app.add_handler(CommandHandler("pm", cmd_pm))
    app.add_handler(CommandHandler("script", cmd_script))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    app.job_queue.run_daily(job_am, time=time(
        hour=settings.SCHEDULE_HOUR, minute=settings.SCHEDULE_MINUTE, tzinfo=TZ))
    app.job_queue.run_daily(job_pm, time=time(
        hour=settings.SCHEDULE_PM_HOUR, minute=settings.SCHEDULE_PM_MINUTE, tzinfo=TZ))

    log.info("봇 시작 (오전 %02d:%02d / 오후 %02d:%02d %s)",
             settings.SCHEDULE_HOUR, settings.SCHEDULE_MINUTE,
             settings.SCHEDULE_PM_HOUR, settings.SCHEDULE_PM_MINUTE, settings.TIMEZONE)
    app.run_polling()


if __name__ == "__main__":
    main()
