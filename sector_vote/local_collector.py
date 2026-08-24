"""Run transcript collection on a local/residential IP and upload to Railway."""

import argparse
import getpass
import os
import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import requests

from sector_vote.channels import CHANNELS
from sector_vote.youtube_source import fetch_latest_videos, fetch_transcript

DEFAULT_SERVICE_URL = "https://patient-amazement-production-2257.up.railway.app"


def normalize_service_url(value: str) -> str:
    parsed = urlsplit((value or "").strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("서비스 주소는 유효한 https:// 주소여야 합니다")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("서비스 주소에 인증정보·쿼리·프래그먼트를 넣을 수 없습니다")
    if parsed.path not in {"", "/"}:
        raise ValueError("서비스 주소에는 도메인만 입력하세요")
    return f"https://{parsed.netloc}"


def _require_success_without_redirect(response: requests.Response) -> None:
    if 300 <= response.status_code < 400:
        raise RuntimeError("인증 요청에서 리다이렉트가 발생해 보안을 위해 중단했습니다")
    response.raise_for_status()


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def fetch_known_video_ids(service_url: str, token: str) -> set[str]:
    base_url = normalize_service_url(service_url)
    response = requests.get(
        f"{base_url}/api/videos",
        headers={"X-Admin-Token": token},
        timeout=30,
        allow_redirects=False,
    )
    _require_success_without_redirect(response)
    return set(response.json().get("video_ids", []))


def upload_transcript(service_url: str, token: str, payload: dict) -> None:
    base_url = normalize_service_url(service_url)
    response = requests.post(
        f"{base_url}/api/ingest/transcript",
        headers={"X-Admin-Token": token},
        json=payload,
        timeout=180,
        allow_redirects=False,
    )
    _require_success_without_redirect(response)


def collect_and_upload(
    *,
    channels: list[dict],
    service_url: str,
    token: str,
    now: datetime,
    lookback_hours: int,
    known_video_ids: set[str],
    fetch_videos: Callable[[dict], list[dict]],
    fetch_script: Callable[[str], str],
    upload: Callable[[str, str, dict], None],
    max_videos_per_channel: int = 2,
    progress: Callable[[str], None] | None = None,
) -> dict:
    cutoff = now.astimezone(timezone.utc) - timedelta(hours=lookback_hours)
    base_url = normalize_service_url(service_url)
    result = {
        "channels_checked": 0,
        "videos_uploaded": 0,
        "videos_skipped": 0,
        "errors": [],
    }

    for channel in channels:
        result["channels_checked"] += 1
        name = channel["name"]
        if progress:
            progress(f"[{result['channels_checked']}/{len(channels)}] {name} 확인")
        try:
            videos = fetch_videos(channel)
        except Exception as exc:  # noqa: BLE001 - isolate network failures per channel
            result["errors"].append({"channel": name, "error": str(exc)[:200]})
            continue

        candidates = []
        for video in videos:
            try:
                published_at = _parse_iso(video["published_at"])
            except (KeyError, TypeError, ValueError) as exc:
                result["errors"].append({
                    "channel": name,
                    "video_id": video.get("video_id", ""),
                    "error": f"invalid published_at: {exc}"[:200],
                })
                continue
            if published_at < cutoff:
                result["videos_skipped"] += 1
                continue
            candidates.append(video)

        for video in candidates[:max_videos_per_channel]:
            video_id = video["video_id"]
            if video_id in known_video_ids:
                result["videos_skipped"] += 1
                continue
            try:
                transcript = fetch_script(video_id)
                payload = {
                    "video_id": video_id,
                    "channel": name,
                    "title": video["title"],
                    "url": video["url"],
                    "published_at": video["published_at"],
                    "transcript": transcript[:95_000],
                }
                upload(f"{base_url}/api/ingest/transcript", token, payload)
                known_video_ids.add(video_id)
                result["videos_uploaded"] += 1
                if progress:
                    progress(f"  업로드 완료: {video['title'][:70]}")
            except Exception as exc:  # noqa: BLE001 - continue with remaining videos
                result["errors"].append({
                    "channel": name,
                    "video_id": video_id,
                    "error": str(exc)[:200],
                })
                if progress:
                    progress(f"  건너뜀: {video_id} ({type(exc).__name__})")
    return result


def collection_exit_code(_result: dict) -> int:
    """A completed run is successful even when there was nothing new to upload."""
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="로컬 IP에서 YouTube 자막을 수집해 섹터 분석 서버로 전송")
    parser.add_argument(
        "--url",
        default=os.environ.get("SECTOR_SERVICE_URL", DEFAULT_SERVICE_URL),
        help="Railway 섹터 서비스 주소",
    )
    parser.add_argument("--hours", type=int, default=36, help="최근 몇 시간의 영상을 볼지")
    parser.add_argument("--max-per-channel", type=int, default=2, help="채널별 최대 영상 수")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    try:
        args.url = normalize_service_url(args.url)
    except ValueError as exc:
        print(f"서비스 주소 오류: {exc}")
        return 2

    token = getpass.getpass("Railway의 SECTOR_ADMIN_TOKEN을 입력하세요: ")
    if not token:
        print("토큰이 비어 있어 중단합니다.")
        return 2

    try:
        known_ids = fetch_known_video_ids(args.url, token)
    except Exception as exc:  # noqa: BLE001 - present a concise CLI error
        print(f"서버 인증 또는 기존 데이터 조회 실패: {exc}")
        return 1

    result = collect_and_upload(
        channels=CHANNELS,
        service_url=args.url,
        token=token,
        now=datetime.now(timezone.utc),
        lookback_hours=args.hours,
        known_video_ids=known_ids,
        fetch_videos=fetch_latest_videos,
        fetch_script=fetch_transcript,
        upload=lambda _url, auth, payload: upload_transcript(args.url, auth, payload),
        max_videos_per_channel=args.max_per_channel,
        progress=print,
    )

    print("\n수집 완료")
    print(f"- 확인 채널: {result['channels_checked']}")
    print(f"- 분석 전송: {result['videos_uploaded']}")
    print(f"- 기존·기간 제외: {result['videos_skipped']}")
    print(f"- 오류·자막 없음: {len(result['errors'])}")
    return collection_exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
