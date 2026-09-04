"""Standalone FastAPI app for YouTube-script sector consensus."""

import html
import math
import os
import secrets
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator

from sector_vote.channels import CHANNELS
from sector_vote.classifier import classify_transcript
from sector_vote.ingest import refresh_channels
from sector_vote.scheduler import run_periodic
from sector_vote.sector_logic import SectorCall, aggregate_calls, normalize_sector
from sector_vote.storage import SectorStore
from sector_vote.transcript_provider import fetch_supadata_transcript
from sector_vote.youtube_source import fetch_latest_videos, fetch_transcript


def _clean_channel(value: str) -> str:
    return " ".join(str(value or "").split())


class TranscriptPayload(BaseModel):
    video_id: str = Field(min_length=3, max_length=30, pattern=r"^[A-Za-z0-9_-]+$")
    channel: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=300)
    url: str = Field(min_length=8, max_length=500)
    published_at: str
    transcript: str = Field(min_length=20, max_length=100_000)

    @field_validator("channel")
    @classmethod
    def normalize_channel(cls, value: str) -> str:
        cleaned = _clean_channel(value)
        if not cleaned:
            raise ValueError("channel must not be blank")
        return cleaned


CSS = """
:root{--bg:#f4f7fb;--ink:#101828;--muted:#667085;--line:#e4e7ec;--blue:#175cd3;--red:#d92d20;--flat:#7f56d9;--card:#fff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:Pretendard,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
a{color:inherit}.wrap{max-width:860px;margin:auto;padding:0 18px 80px}.top{display:flex;align-items:center;padding:18px 0;color:var(--muted);font-size:13px}.brand{font-weight:900;color:var(--ink);letter-spacing:-.03em}.top nav{margin-left:auto;display:flex;gap:14px}.top a{text-decoration:none}
.hero{position:relative;overflow:hidden;border-radius:26px;padding:30px 24px;background:linear-gradient(135deg,#101828 0%,#1849a9 100%);color:white;box-shadow:0 18px 45px rgba(16,24,40,.16)}
.hero:after{content:"";position:absolute;width:230px;height:230px;border-radius:50%;right:-90px;top:-100px;background:rgba(255,255,255,.1)}.eyebrow{font-size:12px;letter-spacing:.13em;font-weight:800;color:#b2ccff}.hero h1{font-size:34px;letter-spacing:-.055em;margin:9px 0 8px}.hero p{margin:0;color:#d1e0ff;line-height:1.65;font-size:14px}.stats{display:flex;gap:8px;margin-top:20px;flex-wrap:wrap}.stat{background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.14);border-radius:12px;padding:8px 11px;font-size:12px}.stat b{font-size:16px;margin-right:4px}
.headline{display:flex;align-items:end;margin:30px 2px 12px}.headline h2{font-size:20px;letter-spacing:-.04em;margin:0}.headline span{margin-left:auto;color:var(--muted);font-size:12px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:18px;box-shadow:0 4px 14px rgba(16,24,40,.035)}.ctop{display:flex;align-items:center;gap:10px}.icon{width:42px;height:42px;border-radius:13px;display:grid;place-items:center;font-size:21px;background:#eef4ff}.name{font-weight:850;font-size:17px;letter-spacing:-.035em}.pill{margin-left:auto;border-radius:999px;padding:5px 9px;font-size:11px;font-weight:800}.up{color:var(--red);background:#fff1f0}.down{color:var(--blue);background:#eff4ff}.neutral{color:var(--flat);background:#f4f3ff}.bar{display:flex;height:10px;border-radius:99px;overflow:hidden;background:#f2f4f7;margin:17px 0 10px}.bar i{display:block}.bu{background:var(--red)}.bn{background:#98a2b3}.bd{background:var(--blue)}.nums{display:flex;gap:12px;font-size:12px;color:var(--muted)}.nums b{color:var(--ink)}.empty{background:white;border:1px dashed #98a2b3;border-radius:18px;text-align:center;padding:48px 20px;color:var(--muted)}.empty strong{display:block;color:var(--ink);font-size:18px;margin-bottom:6px}.note{margin-top:20px;padding:17px 18px;border-radius:16px;background:#eef4ff;color:#344054;font-size:13px;line-height:1.7}.note b{color:#1849a9}.footer{margin-top:28px;border-top:1px solid var(--line);padding-top:17px;color:var(--muted);font-size:11px;line-height:1.7}
.evidence{margin-top:16px;padding-top:14px;border-top:1px solid var(--line)}.evidence summary{cursor:pointer;color:#1849a9;font-size:13px;font-weight:800;list-style:none}.evidence summary::-webkit-details-marker{display:none}.evidence summary:after{content:"＋";float:right}.evidence[open] summary:after{content:"－"}.evlist{display:grid;gap:13px;margin-top:13px}.evitem{display:grid;grid-template-columns:112px 1fr;gap:12px;padding:12px;border-radius:14px;background:#f8fafc;border:1px solid #eaecf0}.evthumb{display:block}.evthumb img{display:block;width:112px;aspect-ratio:16/9;object-fit:cover;border-radius:9px;background:#e4e7ec}.evbody{min-width:0}.evmeta{display:flex;gap:7px;align-items:center;font-size:12px}.evmeta b{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.evpill{margin-left:auto;padding:3px 7px;border-radius:999px;font-size:10px;font-weight:800}.evtitle{display:block;margin-top:4px;text-decoration:none;font-size:13px;font-weight:750;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.evtitle:hover,.evgo:hover{text-decoration:underline}.evbody blockquote{margin:7px 0 0;color:#344054;font-size:12px;line-height:1.55}.evreason{margin-top:4px;color:var(--muted);font-size:11px}.evgo{display:inline-block;margin-top:7px;color:#1849a9;text-decoration:none;font-size:11px;font-weight:750}
@media(max-width:640px){.grid{grid-template-columns:1fr}.hero{padding:26px 20px}.hero h1{font-size:29px}.headline{align-items:start;flex-direction:column;gap:4px}.headline span{margin:0}.top nav{gap:9px}.evitem{grid-template-columns:96px 1fr;padding:10px}.evthumb img{width:96px}.evbody blockquote{font-size:11.5px}}
"""

ICONS = {
    "반도체": "▦", "2차전지": "⚡", "자동차": "◆", "바이오·헬스케어": "✚",
    "금융": "₩", "조선·방산": "⚓", "에너지·화학": "◉", "인터넷·게임": "⌁",
    "소비재": "▣", "건설·리츠": "▥", "AI·로봇": "✦", "기타": "●",
}
LABELS = {"up": "강세 우세", "down": "약세 우세", "neutral": "의견 팽팽"}
CALL_LABELS = {"up": "강세", "down": "약세", "neutral": "중립"}


def _to_calls(rows: list[dict]) -> list[SectorCall]:
    calls = []
    for row in rows:
        channel = _clean_channel(row.get("channel", ""))
        if not channel:
            continue
        calls.append(SectorCall(
            channel, row["sector"], row["direction"], row["horizon"], row["published_at"]
        ))
    return calls


def _attach_evidence(sectors: list[dict], rows: list[dict]) -> list[dict]:
    latest: dict[tuple[str, str], dict] = {}
    for row in rows:
        if row.get("horizon") != "next_session":
            continue
        sector = normalize_sector(row.get("sector", ""))
        channel = _clean_channel(row.get("channel", ""))
        if not channel:
            continue
        direction = row.get("direction")
        if direction not in CALL_LABELS:
            direction = "neutral"
        video_id = str(row.get("video_id", ""))
        if not video_id or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for char in video_id):
            continue
        item = {
            "channel": channel,
            "direction": direction,
            "direction_label": CALL_LABELS[direction],
            "quote": str(row.get("quote", "")).strip()[:120],
            "reason": str(row.get("reason", "")).strip()[:160],
            "title": str(row.get("title", "")).strip()[:300],
            "video_url": f"https://www.youtube.com/watch?v={video_id}",
            "thumbnail_url": f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg",
            "published_at": str(row.get("published_at", "")),
        }
        key = (channel, sector)
        if key not in latest or item["published_at"] >= latest[key]["published_at"]:
            latest[key] = item

    by_sector: dict[str, list[dict]] = {}
    for (_channel, sector), item in latest.items():
        by_sector.setdefault(sector, []).append(item)
    for items in by_sector.values():
        items.sort(key=lambda item: item["published_at"], reverse=True)

    for sector_row in sectors:
        sector_row["evidence"] = by_sector.get(sector_row["sector"], [])
    return sectors


def _render_evidence(items: list[dict]) -> str:
    if not items:
        return ""
    rendered = []
    for item in items:
        channel = html.escape(item["channel"])
        title = html.escape(item["title"] or "원본 영상")
        raw_quote = item["quote"]
        raw_reason = item["reason"]
        quote = html.escape(raw_quote)
        reason = html.escape(raw_reason)
        video_url = html.escape(item["video_url"], quote=True)
        thumbnail_url = html.escape(item["thumbnail_url"], quote=True)
        direction = item["direction"]
        quote_html = f"<blockquote><b>AI 발언 발췌</b><br>“{quote}”</blockquote>" if raw_quote else ""
        reason_html = ""
        if raw_reason and raw_reason != raw_quote:
            reason_html = f"<div class='evreason'><b>판정 요약</b> {reason}</div>"
        elif not raw_quote:
            reason_html = "<div class='evreason'><b>판정 요약</b> 짧은 요약이 없습니다.</div>"
        rendered.append(f"""<div class='evitem'>
          <a class='evthumb' href='{video_url}' target='_blank' rel='noopener noreferrer nofollow'>
            <img src='{thumbnail_url}' alt='{channel} 영상 썸네일' loading='lazy' referrerpolicy='no-referrer'></a>
          <div class='evbody'><div class='evmeta'><b>{channel}</b><span class='evpill {direction}'>{CALL_LABELS[direction]}</span></div>
          <a class='evtitle' href='{video_url}' target='_blank' rel='noopener noreferrer nofollow'>{title}</a>
          {quote_html}{reason_html}
          <a class='evgo' href='{video_url}' target='_blank' rel='noopener noreferrer nofollow'>YouTube에서 바로 보기 →</a></div>
        </div>""")
    return f"<details class='evidence'><summary>근거 영상 {len(items)}개 보기</summary><div class='evlist'>{''.join(rendered)}</div></details>"


def _render_cards(sectors: list[dict]) -> str:
    if not sectors:
        return "<div class='empty'><strong>아직 집계된 섹터 전망이 없어요</strong>분석 작업이 끝나면 이곳에 오늘의 섹터 한표가 표시됩니다.</div>"
    cards = []
    for row in sectors:
        total = max(1, row["total"])
        up = row["up"] / total * 100
        neutral = row["neutral"] / total * 100
        down = row["down"] / total * 100
        sector = html.escape(row["sector"])
        consensus = row["consensus"]
        cards.append(f"""<article class='card'>
          <div class='ctop'><span class='icon'>{ICONS.get(row['sector'],'●')}</span>
          <span class='name'>{sector}</span><span class='pill {consensus}'>{LABELS[consensus]}</span></div>
          <div class='bar' aria-label='강세 {row['up']}, 중립 {row['neutral']}, 약세 {row['down']}'>
          <i class='bu' style='width:{up:.1f}%'></i><i class='bn' style='width:{neutral:.1f}%'></i><i class='bd' style='width:{down:.1f}%'></i></div>
          <div class='nums'><span>강세 <b>{row['up']}</b></span><span>중립 <b>{row['neutral']}</b></span><span>약세 <b>{row['down']}</b></span><span>총 <b>{row['total']}</b>표</span></div>
          {_render_evidence(row.get('evidence', []))}
        </article>""")
    return "<div class='grid'>" + "".join(cards) + "</div>"


def _page(body: str, title: str = "내일 섹터 한표") -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang='ko'><head><meta charset='utf-8'>
    <meta name='viewport' content='width=device-width,initial-scale=1'><title>{html.escape(title)}</title>
    <meta name='description' content='경제·금융 유튜브 스크립트에서 섹터 전망만 모아 보여주는 시장 심리 페이지'>
    <style>{CSS}</style></head><body>{body}</body></html>""")


def create_app(
    db_path: str | Path,
    *,
    admin_token: str,
    classify_fn: Callable[[str], list[dict]] | None = None,
    channels: list[dict] | None = None,
    fetch_videos_fn: Callable[[dict], list[dict]] | None = None,
    fetch_script_fn: Callable[[str], str] | None = None,
    now_fn: Callable[[], datetime] | None = None,
    auto_refresh_minutes: float | None = None,
    transcript_provider_name: str | None = None,
) -> FastAPI:
    store = SectorStore(db_path)
    configured_channels = channels if channels is not None else CHANNELS
    video_fetcher = fetch_videos_fn or fetch_latest_videos
    supadata_key = os.environ.get("SUPADATA_API_KEY", "").strip()
    if fetch_script_fn:
        script_fetcher = fetch_script_fn
    elif supadata_key:
        script_fetcher = lambda video_id: fetch_supadata_transcript(
            video_id, supadata_key, cancel_event=scheduler_stop
        )
    else:
        script_fetcher = fetch_transcript
    provider_name = transcript_provider_name or ("supadata" if supadata_key else "youtube-direct")
    if auto_refresh_minutes is None:
        configured_interval = os.environ.get("SECTOR_AUTO_REFRESH_MINUTES")
        auto_refresh_minutes = float(configured_interval) if configured_interval else (360 if supadata_key else 0)
    if not math.isfinite(auto_refresh_minutes) or auto_refresh_minutes < 0:
        raise ValueError("auto_refresh_minutes must be finite and not negative")
    clock = now_fn or (lambda: datetime.now(timezone.utc))
    refresh_state = {"running": False, "last_result": None, "scheduler_error": None}
    refresh_lock = threading.Lock()
    scheduler_stop = threading.Event()

    def scheduler_error(exc: Exception):
        with refresh_lock:
            refresh_state["scheduler_error"] = str(exc)[:180]

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        worker = None
        if auto_refresh_minutes > 0:
            worker = threading.Thread(
                target=run_periodic,
                args=(run_refresh_if_idle, auto_refresh_minutes * 60, scheduler_stop),
                kwargs={"on_error": scheduler_error},
                name="sector-auto-refresh",
                daemon=True,
            )
            worker.start()
        try:
            yield
        finally:
            scheduler_stop.set()
            if worker:
                worker.join()

    app = FastAPI(title="내일 섹터 한표", docs_url=None, redoc_url=None, lifespan=lifespan)

    def classify(text: str) -> list[dict]:
        if classify_fn:
            return classify_fn(text)
        return classify_transcript(text, os.environ.get("ANTHROPIC_API_KEY", ""))

    def summary_data() -> dict:
        current = clock().astimezone(timezone.utc)
        daily_cutoff = current - timedelta(hours=36)
        weekly_cutoff = current - timedelta(days=7)
        rows = store.list_calls(daily_cutoff.isoformat())
        weekly_rows = store.list_calls(weekly_cutoff.isoformat())
        sectors = _attach_evidence(aggregate_calls(_to_calls(rows)), rows)
        status = store.status()
        return {
            "generated_at": current.isoformat(),
            "sectors": sectors,
            "weekly_sectors": aggregate_calls(_to_calls(weekly_rows)),
            "videos_analyzed": status["videos"],
            "calls_analyzed": status["calls"],
            "channels_configured": len(configured_channels),
        }

    def require_token(x_admin_token: str | None = Header(default=None)):
        if (
            not admin_token
            or x_admin_token is None
            or not secrets.compare_digest(x_admin_token, admin_token)
        ):
            raise HTTPException(status_code=401, detail="invalid admin token")

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            **store.status(),
            "automation": {
                "enabled": auto_refresh_minutes > 0,
                "interval_minutes": auto_refresh_minutes,
                "provider": provider_name,
            },
        }

    @app.get("/api/summary")
    def api_summary():
        return summary_data()

    @app.get("/api/videos", dependencies=[Depends(require_token)])
    def api_videos():
        return {"video_ids": sorted(store.list_video_ids())}

    @app.get("/api/evidence", dependencies=[Depends(require_token)])
    def api_evidence():
        return store.list_calls()

    @app.post("/api/ingest/transcript", dependencies=[Depends(require_token)])
    def ingest_transcript(payload: TranscriptPayload):
        calls = classify(payload.transcript)
        store.save_video_calls(
            video_id=payload.video_id, channel=payload.channel, title=payload.title,
            url=payload.url, published_at=payload.published_at, calls=calls,
        )
        return {"ok": True, "calls_saved": len(calls)}

    def run_refresh():
        try:
            current = clock()
            videos_pruned = store.prune_before(
                (current.astimezone(timezone.utc) - timedelta(days=30)).isoformat()
            )
            result = refresh_channels(
                store=store,
                channels=configured_channels,
                now=current,
                lookback_hours=36,
                fetch_videos=video_fetcher,
                fetch_script=script_fetcher,
                classify=classify,
                clock=clock,
            )
            result["videos_pruned"] = videos_pruned
            with refresh_lock:
                refresh_state["last_result"] = result
                refresh_state["scheduler_error"] = None
        except Exception as exc:  # noqa: BLE001 - keep both manual and scheduled refresh observable
            with refresh_lock:
                refresh_state["last_result"] = {
                    "channels_checked": 0,
                    "videos_analyzed": 0,
                    "calls_saved": 0,
                    "errors": [{"channel": "system", "error": str(exc)[:180]}],
                }
        finally:
            with refresh_lock:
                refresh_state["running"] = False

    def run_refresh_if_idle() -> bool:
        with refresh_lock:
            if refresh_state["running"]:
                return False
            refresh_state["running"] = True
        run_refresh()
        return True

    @app.post("/api/refresh", status_code=202, dependencies=[Depends(require_token)])
    def refresh(background_tasks: BackgroundTasks):
        with refresh_lock:
            if refresh_state["running"]:
                raise HTTPException(status_code=409, detail="refresh already running")
            refresh_state["running"] = True
        background_tasks.add_task(run_refresh)
        return {"accepted": True}

    @app.get("/api/refresh/status", dependencies=[Depends(require_token)])
    def refresh_status():
        with refresh_lock:
            return dict(refresh_state)

    @app.get("/", response_class=HTMLResponse)
    def index():
        data = summary_data()
        body = f"""<div class='wrap'><header class='top'><span class='brand'>SECTOR ONE VOTE</span>
        <nav><a href='/'>오늘</a><a href='/methodology'>집계 기준</a></nav></header>
        <section class='hero'><div class='eyebrow'>YOUTUBE SCRIPT CONSENSUS</div><h1>내일 섹터 한표</h1>
        <p>사람에게 투표를 받지 않습니다.<br>경제·금융 유튜브 스크립트에서 다음 장에 대한 섹터 방향만 모았습니다.</p>
        <div class='stats'><span class='stat'><b>{data['channels_configured']}</b>분석 채널</span>
        <span class='stat'><b>{data['videos_analyzed']}</b>분석 영상</span><span class='stat'><b>{data['calls_analyzed']}</b>섹터 콜</span></div></section>
        <div class='headline'><h2>유튜버들은 어느 섹터를 보고 있을까?</h2><span>채널당 섹터별 1표 · 다음 거래일 전망만</span></div>
        {_render_cards(data['sectors'])}
        <div class='headline'><h2>최근 7일 관심 섹터</h2><span>최근 일주일간 공개된 다음 거래일 전망</span></div>
        {_render_cards(data['weekly_sectors'])}
        <div class='note'><b>읽는 법</b><br>빨강은 강세, 파랑은 약세, 회색은 중립입니다. 구독자 수와 인지도는 가중하지 않고 모든 채널을 같은 1표로 계산합니다.</div>
        <footer class='footer'>본 페이지는 공개된 영상 스크립트를 AI로 분류한 참고용 콘텐츠입니다. 특정 상품·종목 추천이나 투자 권유가 아니며, 자동 분류 결과에는 오류가 있을 수 있습니다. 영상 전문은 저장·공개하지 않고 채널명·짧은 판정 근거·원본 영상 링크만 제공합니다.</footer></div>"""
        return _page(body)

    @app.get("/methodology", response_class=HTMLResponse)
    def methodology():
        channel_items = "".join(f"<li>{html.escape(channel['name'])}</li>" for channel in configured_channels)
        body = f"""<div class='wrap'><header class='top'><a class='brand' href='/'>SECTOR ONE VOTE</a>
        <nav><a href='/'>오늘</a><a href='/methodology'>집계 기준</a></nav></header>
        <section class='hero'><div class='eyebrow'>METHODOLOGY</div><h1>어떻게 집계하나요?</h1><p>인물을 평가하지 않고, 스크립트 속 섹터 방향만 동일한 기준으로 셉니다.</p></section>
        <div class='card' style='margin-top:18px;line-height:1.8'><h2>핵심 원칙</h2><ol>
        <li>다음 거래일 방향이 명시된 발언만 집계합니다.</li><li>장기 전망·개별 종목·ETF·상품 추천은 제외합니다.</li>
        <li>한 채널은 한 섹터에 하루 최대 1표만 반영합니다.</li><li>구독자 수와 인지도 가중치는 사용하지 않습니다.</li>
        <li>메인 화면에는 인물 순위를 만들지 않고 섹터 합계만 공개합니다.</li>
        <li>판정 투명성을 위해 채널명·120자 이내 인용·원본 영상 링크를 함께 제공합니다.</li></ol>
        <h2>분석 대상 {len(configured_channels)}개 채널</h2><ul>{channel_items}</ul></div>
        <footer class='footer'>분류 오류 제보와 채널 변경은 운영자가 원본 영상과 타임코드를 확인한 뒤 반영합니다.</footer></div>"""
        return _page(body, "집계 기준 · 내일 섹터 한표")

    return app


DB_PATH = os.environ.get("SECTOR_DB_PATH", str(Path(__file__).parent / "sector_vote.db"))
ADMIN_TOKEN = os.environ.get("SECTOR_ADMIN_TOKEN", "")
app = create_app(DB_PATH, admin_token=ADMIN_TOKEN)
