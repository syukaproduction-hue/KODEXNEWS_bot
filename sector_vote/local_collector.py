"""Run transcript collection on a local/residential IP and upload to Railway."""

import argparse
import getpass
import json
import math
import os
import sys
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
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


def select_channel_batch(channels: list[dict], *, start: int, batch_size: int) -> tuple[list[dict], int]:
    if not channels:
        return [], 0
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    normalized_start = start % len(channels)
    batch = channels[normalized_start:normalized_start + batch_size]
    next_start = (normalized_start + len(batch)) % len(channels)
    return batch, next_start


def validate_cli_numbers(*, hours: int, max_per_channel: int, batch_size: int, delay: float) -> None:
    if hours <= 0 or max_per_channel <= 0 or batch_size <= 0:
        raise ValueError("시간·영상 수·배치 크기는 양수여야 합니다")
    if not math.isfinite(delay) or delay < 0:
        raise ValueError("지연 시간은 유한한 0 이상의 숫자여야 합니다")


def load_batch_start(path: Path) -> int:
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("next_start", 0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0


def save_batch_start(path: Path, next_start: int) -> None:
    temp_path = path.with_name(path.name + ".tmp")
    try:
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump({"next_start": next_start}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except OSError:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


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
    delay_seconds: float = 0,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict:
    cutoff = now.astimezone(timezone.utc) - timedelta(hours=lookback_hours)
    base_url = normalize_service_url(service_url)
    result = {
        "channels_checked": 0,
        "videos_uploaded": 0,
        "videos_skipped": 0,
        "errors": [],
        "ip_blocked": False,
    }
    transcript_attempted = False

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
                if transcript_attempted and delay_seconds > 0:
                    if progress:
                        progress(f"  다음 요청까지 {delay_seconds:g}초 대기")
                    sleeper(delay_seconds)
                transcript_attempted = True
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
                if type(exc).__name__ in {"IpBlocked", "RequestBlocked"}:
                    result["ip_blocked"] = True
                    if progress:
                        progress("  IP 차단 감지: 추가 요청을 즉시 중단합니다.")
                    return result
                if progress:
                    progress(f"  건너뜀: {video_id} ({type(exc).__name__})")
    return result


def collection_exit_code(result: dict) -> int:
    """A completed no-op is successful; an IP ban asks the user to change networks."""
    return 3 if result.get("ip_blocked") else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="로컬 IP에서 YouTube 자막을 수집해 섹터 분석 서버로 전송")
    parser.add_argument(
        "--url",
        default=os.environ.get("SECTOR_SERVICE_URL", DEFAULT_SERVICE_URL),
        help="Railway 섹터 서비스 주소",
    )
    parser.add_argument("--hours", type=int, default=36, help="최근 몇 시간의 영상을 볼지")
    parser.add_argument("--max-per-channel", type=int, default=1, help="채널별 최대 영상 수")
    parser.add_argument("--batch-size", type=int, default=5, help="한 번에 확인할 채널 수")
    parser.add_argument("--delay", type=float, default=12, help="자막 요청 사이 대기 시간(초)")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")

    try:
        args.url = normalize_service_url(args.url)
        validate_cli_numbers(
            hours=args.hours,
            max_per_channel=args.max_per_channel,
            batch_size=args.batch_size,
            delay=args.delay,
        )
    except ValueError as exc:
        print(f"입력값 오류: {exc}")
        return 2

    state_path = Path(__file__).parent / ".collector-state.json"
    start = load_batch_start(state_path)
    channel_batch, next_start = select_channel_batch(CHANNELS, start=start, batch_size=args.batch_size)
    if channel_batch:
        first_number = start % len(CHANNELS) + 1
        last_number = first_number + len(channel_batch) - 1
        print(f"이번 실행은 채널 {first_number}-{last_number}/{len(CHANNELS)}만 저속으로 확인합니다.")

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
        channels=channel_batch,
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
        delay_seconds=args.delay,
    )

    state_error = None
    if not result["ip_blocked"]:
        try:
            save_batch_start(state_path, next_start)
        except OSError as exc:
            state_error = str(exc)

    print("\n수집 완료")
    print(f"- 확인 채널: {result['channels_checked']}")
    print(f"- 분석 전송: {result['videos_uploaded']}")
    print(f"- 기존·기간 제외: {result['videos_skipped']}")
    print(f"- 오류·자막 없음: {len(result['errors'])}")
    if state_error:
        print(f"- 경고: 배치 진행 상태 저장 실패: {state_error}")
        print("- 업로드 결과는 서버에 반영됐지만 다음 실행 배치를 기록하지 못했습니다.")
        return 1
    if result["ip_blocked"]:
        print("- IP 차단 감지: 모바일 핫스팟 등 새 네트워크로 바꾼 뒤 다시 실행하세요.")
        print("- 같은 배치부터 다시 시작하며 추가 요청은 보내지 않았습니다.")
    else:
        next_number = next_start + 1
        print(f"- 다음 실행 시작 채널: {next_number}/{len(CHANNELS)}")
    return collection_exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
