"""Standalone FastAPI app for YouTube-script sector consensus."""

import html
import os
import secrets
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from sector_vote.channels import CHANNELS
from sector_vote.classifier import classify_transcript
from sector_vote.ingest import refresh_channels
from sector_vote.sector_logic import SectorCall, aggregate_calls
from sector_vote.storage import SectorStore
from sector_vote.youtube_source import fetch_latest_videos, fetch_transcript


class TranscriptPayload(BaseModel):
    video_id: str = Field(min_length=3, max_length=30)
    channel: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=300)
    url: str = Field(min_length=8, max_length=500)
    published_at: str
    transcript: str = Field(min_length=20, max_length=100_000)


CSS = """
:root{--bg:#f4f7fb;--ink:#101828;--muted:#667085;--line:#e4e7ec;--blue:#175cd3;--red:#d92d20;--flat:#7f56d9;--card:#fff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:Pretendard,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
a{color:inherit}.wrap{max-width:860px;margin:auto;padding:0 18px 80px}.top{display:flex;align-items:center;padding:18px 0;color:var(--muted);font-size:13px}.brand{font-weight:900;color:var(--ink);letter-spacing:-.03em}.top nav{margin-left:auto;display:flex;gap:14px}.top a{text-decoration:none}
.hero{position:relative;overflow:hidden;border-radius:26px;padding:30px 24px;background:linear-gradient(135deg,#101828 0%,#1849a9 100%);color:white;box-shadow:0 18px 45px rgba(16,24,40,.16)}
.hero:after{content:"";position:absolute;width:230px;height:230px;border-radius:50%;right:-90px;top:-100px;background:rgba(255,255,255,.1)}.eyebrow{font-size:12px;letter-spacing:.13em;font-weight:800;color:#b2ccff}.hero h1{font-size:34px;letter-spacing:-.055em;margin:9px 0 8px}.hero p{margin:0;color:#d1e0ff;line-height:1.65;font-size:14px}.stats{display:flex;gap:8px;margin-top:20px;flex-wrap:wrap}.stat{background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.14);border-radius:12px;padding:8px 11px;font-size:12px}.stat b{font-size:16px;margin-right:4px}
.headline{display:flex;align-items:end;margin:30px 2px 12px}.headline h2{font-size:20px;letter-spacing:-.04em;margin:0}.headline span{margin-left:auto;color:var(--muted);font-size:12px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:18px;box-shadow:0 4px 14px rgba(16,24,40,.035)}.ctop{display:flex;align-items:center;gap:10px}.icon{width:42px;height:42px;border-radius:13px;display:grid;place-items:center;font-size:21px;background:#eef4ff}.name{font-weight:850;font-size:17px;letter-spacing:-.035em}.pill{margin-left:auto;border-radius:999px;padding:5px 9px;font-size:11px;font-weight:800}.up{color:var(--red);background:#fff1f0}.down{color:var(--blue);background:#eff4ff}.neutral{color:var(--flat);background:#f4f3ff}.bar{display:flex;height:10px;border-radius:99px;overflow:hidden;background:#f2f4f7;margin:17px 0 10px}.bar i{display:block}.bu{background:var(--red)}.bn{background:#98a2b3}.bd{background:var(--blue)}.nums{display:flex;gap:12px;font-size:12px;color:var(--muted)}.nums b{color:var(--ink)}.empty{background:white;border:1px dashed #98a2b3;border-radius:18px;text-align:center;padding:48px 20px;color:var(--muted)}.empty strong{display:block;color:var(--ink);font-size:18px;margin-bottom:6px}.note{margin-top:20px;padding:17px 18px;border-radius:16px;background:#eef4ff;color:#344054;font-size:13px;line-height:1.7}.note b{color:#1849a9}.footer{margin-top:28px;border-top:1px solid var(--line);padding-top:17px;color:var(--muted);font-size:11px;line-height:1.7}
@media(max-width:640px){.grid{grid-template-columns:1fr}.hero{padding:26px 20px}.hero h1{font-size:29px}.headline{align-items:start;flex-direction:column;gap:4px}.headline span{margin:0}.top nav{gap:9px}}
"""

ICONS = {
    "반도체": "▦", "2차전지": "⚡", "자동차": "◆", "바이오·헬스케어": "✚",
    "금융": "₩", "조선·방산": "⚓", "에너지·화학": "◉", "인터넷·게임": "⌁",
    "소비재": "▣", "건설·리츠": "▥", "AI·로봇": "✦", "기타": "●",
}
LABELS = {"up": "강세 우세", "down": "약세 우세", "neutral": "의견 팽팽"}


def _to_calls(rows: list[dict]) -> list[SectorCall]:
    return [SectorCall(
        row["channel"], row["sector"], row["direction"], row["horizon"], row["published_at"]
    ) for row in rows]


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
) -> FastAPI:
    app = FastAPI(title="내일 섹터 한표", docs_url=None, redoc_url=None)
    store = SectorStore(db_path)
    configured_channels = channels if channels is not None else CHANNELS
    video_fetcher = fetch_videos_fn or fetch_latest_videos
    script_fetcher = fetch_script_fn or fetch_transcript
    clock = now_fn or (lambda: datetime.now(timezone.utc))
    refresh_state = {"running": False, "last_result": None}
    refresh_lock = threading.Lock()

    def classify(text: str) -> list[dict]:
        if classify_fn:
            return classify_fn(text)
        return classify_transcript(text, os.environ.get("ANTHROPIC_API_KEY", ""))

    def summary_data() -> dict:
        current = clock().astimezone(timezone.utc)
        cutoff = current - timedelta(hours=36)
        rows = store.list_calls(cutoff.isoformat())
        sectors = aggregate_calls(_to_calls(rows))
        status = store.status()
        return {
            "generated_at": current.isoformat(),
            "sectors": sectors,
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
        return {"status": "ok", **store.status()}

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
            result = refresh_channels(
                store=store,
                channels=configured_channels,
                now=clock(),
                lookback_hours=36,
                fetch_videos=video_fetcher,
                fetch_script=script_fetcher,
                classify=classify,
            )
            with refresh_lock:
                refresh_state["last_result"] = result
        finally:
            with refresh_lock:
                refresh_state["running"] = False

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
        <div class='note'><b>읽는 법</b><br>빨강은 강세, 파랑은 약세, 회색은 중립입니다. 구독자 수와 인지도는 가중하지 않고 모든 채널을 같은 1표로 계산합니다.</div>
        <footer class='footer'>본 페이지는 공개된 영상 스크립트를 AI로 분류한 참고용 콘텐츠입니다. 특정 상품·종목 추천이나 투자 권유가 아니며, 자동 분류 결과에는 오류가 있을 수 있습니다. 영상 전문은 저장·공개하지 않고 짧은 판정 근거만 내부 검증에 사용합니다.</footer></div>"""
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
        <li>메인 화면에는 인물 순위를 만들지 않고 섹터 합계만 공개합니다.</li></ol>
        <h2>분석 대상 {len(configured_channels)}개 채널</h2><ul>{channel_items}</ul></div>
        <footer class='footer'>분류 오류 제보와 채널 변경은 운영자가 원본 영상과 타임코드를 확인한 뒤 반영합니다.</footer></div>"""
        return _page(body, "집계 기준 · 내일 섹터 한표")

    return app


DB_PATH = os.environ.get("SECTOR_DB_PATH", str(Path(__file__).parent / "sector_vote.db"))
ADMIN_TOKEN = os.environ.get("SECTOR_ADMIN_TOKEN", "")
app = create_app(DB_PATH, admin_token=ADMIN_TOKEN)
