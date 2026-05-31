"""
KODEX 시황 뉴스봇
- 매일 평일 오전 9시(한국시간) 자동 브리핑 발송
- /brief 명령으로 즉시 테스트 발송
- /chatid 명령으로 현재 채팅 ID 확인
운영자는 보통 이 파일을 수정할 필요가 없습니다. (설정은 settings.py)
"""

import os
import re
import asyncio
import logging
from datetime import datetime, timedelta, time
from pathlib import Path

import pytz
from anthropic import Anthropic
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

import settings

# ---------- 기본 설정 ----------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("kodex-bot")

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
TARGET_CHAT_ID = os.environ.get("TARGET_CHAT_ID", "").strip()

TZ = pytz.timezone(settings.TIMEZONE)
PROMPT_PATH = Path(__file__).parent / "briefing_prompt.md"

anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)

WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]
TG_LIMIT = 4096


# ---------- 마크다운 기호 제거 + 줄바꿈 정리 ----------
def clean_for_telegram(text: str) -> str:
    text = text.replace("**", "").replace("__", "").replace("`", "")
    out = []
    for raw in text.split("\n"):
        line = raw.rstrip()
        line = re.sub(r"^\s*#{1,6}\s*", "", line)        # # 헤더 기호 제거
        line = re.sub(r"^\s*>\s?", "", line)              # > 인용 기호 제거
        line = re.sub(r"^(\s*)[*+]\s+", r"\1· ", line)    # 마크다운 불릿 *,+ → ·
        line = re.sub(r"\*([^*\n]+)\*", r"\1", line)       # 남은 *기울임* 제거
        if re.fullmatch(r"\s*(-{3,}|\*{3,}|_{3,})\s*", line):  # --- *** ___ 구분선 제거
            continue
        if re.fullmatch(r"\s*[-·*•]\s*", line):            # 내용 없는 불릿 줄 삭제
            continue
        out.append(line)
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)                # 빈 줄 3개 이상 → 2개
    sun = text.find("☀")                                  # 브리핑 앞 군더더기 문장 제거
    if sun > 0:
        text = text[sun:]
    return text.strip()


# ---------- 날짜/기간 안내문 만들기 ----------
def build_date_context() -> str:
    now = datetime.now(TZ)
    today = now.date()
    wd = today.weekday()  # 월=0 ... 일=6
    today_str = f"{today} ({WEEKDAY_KR[wd]})"

    if wd == 0:  # 월요일 → 직전 금~일 포함
        friday = today - timedelta(days=3)
        return (
            f"오늘은 {today_str}이다. 직전 영업일은 {friday} (금)이며, "
            f"주말({today - timedelta(days=2)}~{today - timedelta(days=1)}) 동안의 "
            f"미국 증시·해외 이슈도 함께 다룬다. 기준일은 '{friday} (금) 마감 기준'으로 표기한다."
        )
    else:
        prev = today - timedelta(days=1)
        return (
            f"오늘은 {today_str}이다. 직전 영업일은 {prev} ({WEEKDAY_KR[prev.weekday()]})이며, "
            f"그날 마감 기준으로 정리한다. 기준일은 '{prev} ({WEEKDAY_KR[prev.weekday()]}) 마감 기준'으로 표기한다."
        )


# ---------- 집중 상품/세트 안내문 만들기 ----------
def build_products_block() -> str:
    lines = ["## 현재 집중 상품 (이 목록과 연결되는 이슈만 소재 후보로)"]
    code_to_name = {}
    for p in settings.FOCUS_PRODUCTS:
        lines.append(f"- {p['name']} ({p['code']})")
        code_to_name[p["code"]] = p["name"]

    if settings.PRODUCT_SETS:
        lines.append("\n## 연동 상품 세트 (함께 비교 제시)")
        for s in settings.PRODUCT_SETS:
            names = " + ".join(
                f"{code_to_name.get(c, c)}({c})" for c in s["members"]
            )
            lines.append(f"- {names} → {s['note']}")

    lines.append(f"\n하루 소재 후보 개수: {settings.CANDIDATE_COUNT}개")
    return "\n".join(lines)


# ---------- Claude 호출 (브리핑 생성) ----------
def generate_brief_sync() -> str:
    base_prompt = PROMPT_PATH.read_text(encoding="utf-8")
    system_text = (
        base_prompt
        + "\n\n"
        + build_products_block()
        + "\n\n## 오늘 날짜 안내\n"
        + build_date_context()
    )
    resp = anthropic_client.messages.create(
        model=settings.MODEL,
        max_tokens=settings.MAX_TOKENS,
        system=system_text,
        messages=[{"role": "user", "content": "오늘의 KODEX 시황 브리핑을 작성해줘."}],
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
    )
    parts = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
    text = "\n".join(p for p in parts if p).strip()
    text = clean_for_telegram(text)
    return text or "브리핑 생성 결과가 비어 있습니다. 잠시 후 다시 시도해 주세요."


async def generate_brief() -> str:
    return await asyncio.to_thread(generate_brief_sync)


# ---------- 긴 메시지 분할 발송 (줄 단위) ----------
async def send_long(bot, chat_id, text: str):
    if len(text) <= TG_LIMIT:
        await bot.send_message(chat_id=chat_id, text=text)
        return
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > TG_LIMIT:
            if chunk:
                await bot.send_message(chat_id=chat_id, text=chunk)
            chunk = ""
        chunk = (chunk + "\n" + line) if chunk else line
    if chunk:
        await bot.send_message(chat_id=chat_id, text=chunk)


# ---------- 명령 핸들러 ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "KODEX 시황 뉴스봇입니다.\n"
        "· /brief : 지금 즉시 브리핑 받아보기 (테스트)\n"
        "· /chatid : 이 채팅방의 ID 확인\n"
        "평일 오전 9시에 자동으로 브리핑을 보냅니다."
    )


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"이 채팅 ID: {update.effective_chat.id}")


async def cmd_brief(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("브리핑을 작성 중입니다… (웹 검색 포함, 30초~1분 소요)")
    try:
        text = await generate_brief()
        await send_long(context.bot, update.effective_chat.id, text)
    except Exception as e:
        log.exception("brief failed")
        await update.message.reply_text(f"브리핑 생성 중 오류가 발생했습니다: {e}")


# ---------- 매일 자동 발송 ----------
async def daily_brief_job(context: ContextTypes.DEFAULT_TYPE):
    today = datetime.now(TZ).date()
    if today.weekday() >= 5:  # 토(5)·일(6)은 발송 안 함
        log.info("주말이므로 자동 발송 건너뜀")
        return
    if not TARGET_CHAT_ID:
        log.warning("TARGET_CHAT_ID 미설정 → 자동 발송 불가")
        return
    try:
        text = await generate_brief()
        await send_long(context.bot, TARGET_CHAT_ID, text)
        log.info("일일 브리핑 발송 완료")
    except Exception as e:
        log.exception("daily brief failed")
        try:
            await context.bot.send_message(
                chat_id=TARGET_CHAT_ID,
                text=f"⚠️ 자동 브리핑 생성 중 오류: {e}",
            )
        except Exception:
            pass


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CommandHandler("brief", cmd_brief))

    app.job_queue.run_daily(
        daily_brief_job,
        time=time(hour=settings.SCHEDULE_HOUR, minute=settings.SCHEDULE_MINUTE, tzinfo=TZ),
    )

    log.info("봇 시작 (매일 %02d:%02d %s 발송 예약)",
             settings.SCHEDULE_HOUR, settings.SCHEDULE_MINUTE, settings.TIMEZONE)
    app.run_polling()


if __name__ == "__main__":
    main()
