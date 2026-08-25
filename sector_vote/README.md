# 내일 섹터 한표

기존 `KODEXNEWS_bot` 서비스와 파일을 건드리지 않고 `sector_vote/`에 분리한 독립 FastAPI 웹앱입니다.
경제·금융 유튜브 20개 채널의 공개 영상 스크립트에서 **다음 거래일 섹터 방향**만 AI로 분류해, 인물 순위 없이 섹터 합계만 공개합니다.

## 공개 화면

- `/` — 내일 섹터 한표
- `/methodology` — 집계 기준과 분석 채널
- `/api/summary` — 인물명이 없는 공개 섹터 합계 JSON
- `/health` — 상태 확인

## 보호된 운영 API

모든 요청에 `X-Admin-Token: $SECTOR_ADMIN_TOKEN` 헤더가 필요합니다.

- `POST /api/refresh` — 최근 36시간 내 채널 영상의 자막을 자동 수집·분류
- `GET /api/refresh/status` — 최근 작업 결과와 오류 확인
- `GET /api/evidence` — 채널명·영상 링크·짧은 인용을 포함한 내부 검증 자료
- `POST /api/ingest/transcript` — 외부 수집기가 전달한 스크립트 분석

영상 전문은 DB에 저장하지 않습니다. 공개 화면에도 유튜버별 순위와 원문을 노출하지 않습니다.

## 집계 원칙

1. `next_session`으로 분류된 다음 거래일 전망만 공개 집계합니다.
2. 한 채널은 한 섹터에 한 표만 반영하며, 같은 섹터에 여러 발언이 있으면 최신 판정을 사용합니다.
3. 구독자 수·인지도 가중치를 사용하지 않습니다.
4. 장기 전망, 개별 종목, ETF·상품 추천은 집계에서 제외합니다.
5. 공개 화면은 섹터별 강세·중립·약세 합계만 표시합니다.

## 로컬 실행

```bash
cd sector_vote
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
set SECTOR_ADMIN_TOKEN=change-me
set ANTHROPIC_API_KEY=your-key
.venv/Scripts/uvicorn sector_vote.app:app --app-dir .. --reload
```

저장소 루트에서는 다음 명령도 사용할 수 있습니다.

```bash
uv run --with-requirements sector_vote/requirements.txt uvicorn sector_vote.app:app --port 8000
```

## 테스트

```bash
uv run --with-requirements sector_vote/requirements-dev.txt python -m pytest sector_vote/tests -q
uv run --with-requirements sector_vote/requirements-dev.txt ruff check sector_vote
```

## Railway에 기존 서비스와 별도로 배포

동일 저장소에서 **새 Railway 서비스**를 추가합니다. 현재 운영 중인 서비스 설정은 변경하지 않습니다.

1. `KODEXNEWS_bot` 저장소를 새 서비스에 연결
2. 서비스의 **Root Directory**를 `/sector_vote`로 설정
3. `/data`에 Railway Volume 연결
4. Variables 설정
   - `ANTHROPIC_API_KEY`
   - `SECTOR_ADMIN_TOKEN` — 길고 무작위인 별도 값
   - `SECTOR_DB_PATH=/data/sector_vote.db`
5. Health check: `/health`

## Railway가 YouTube 자막을 차단당할 때: 로컬 수집기

YouTube는 Railway 같은 클라우드 IP의 자막 요청을 차단할 수 있습니다. 이 경우 Windows PC의 일반 인터넷 회선에서 자막만 수집하고, 보호된 API로 Railway에 전송합니다. AI 분류와 DB 저장은 Railway가 계속 담당합니다.

### 가장 쉬운 실행 방법

1. GitHub의 `feature/sector-vote` 브랜치를 ZIP으로 내려받아 압축을 풉니다.
2. `sector_vote/run_local_collector.bat`를 더블클릭합니다.
3. 최초 실행 때 전용 가상환경과 패키지를 자동 설치합니다.
4. 검은 창에서 Railway의 `SECTOR_ADMIN_TOKEN`을 입력합니다. 입력 문자는 화면에 보이지 않습니다.
5. 매 실행마다 20개 중 5개 채널만 확인하고, 채널별 최신 영상 1개를 대상으로 자막 요청 사이에 12초씩 대기합니다.
6. 다음 실행에서는 자동으로 다음 5개 채널로 넘어가며, 네 번 실행하면 20개 채널을 한 바퀴 확인합니다.

토큰은 메모리에서만 사용하며 GitHub나 파일에 저장하지 않습니다. 이미 처리된 영상은 보호된 영상-ID API를 조회해 건너뜁니다. `IpBlocked`가 감지되면 같은 IP로 추가 요청을 보내지 않고 즉시 중단하며, 다음 실행도 같은 채널 배치부터 다시 시작합니다.

### 무료 운영 권장 순서

1. 기존 Wi-Fi에서 `IpBlocked`가 발생했다면 즉시 재실행하지 않습니다.
2. PC를 휴대전화 모바일 핫스팟에 연결해 새로운 공인 IP를 사용합니다.
3. `run_local_collector.bat`을 한 번 실행합니다. 한 번에 5개 채널만 처리합니다.
4. 성공 후 다음 배치는 최소 10~30분 뒤 실행합니다.
5. IP 차단 문구가 나오면 네트워크를 다시 바꾸거나 충분히 기다립니다.

### 명령줄 실행

저장소 루트에서:

```bash
uv run --with-requirements sector_vote/requirements.txt python -m sector_vote.local_collector
```

다른 Railway 주소를 사용할 때:

```bash
uv run --with-requirements sector_vote/requirements.txt python -m sector_vote.local_collector --url https://example.up.railway.app
```

## 자동 갱신

cron-job.org 같은 외부 스케줄러에서 평일 오전 또는 원하는 시각에 아래 요청을 보냅니다.

```bash
curl -X POST "https://새-서비스주소/api/refresh" \
  -H "X-Admin-Token: $SECTOR_ADMIN_TOKEN"
```

갱신은 백그라운드에서 실행되며 요청은 즉시 `202 Accepted`를 반환합니다. 작업 결과와 채널별 오류는 `/api/refresh/status`에서 확인합니다.

## 환경변수

| 변수 | 필수 | 설명 |
|---|---:|---|
| `ANTHROPIC_API_KEY` | 예 | 스크립트 섹터 분류 |
| `SECTOR_ADMIN_TOKEN` | 예 | 운영 API 보호 |
| `SECTOR_DB_PATH` | 권장 | 운영에서는 `/data/sector_vote.db` |
| `PORT` | Railway 자동 | Uvicorn 포트 |

## 운영 유의사항

- 자동 생성 자막이 없거나 YouTube가 서버 IP의 자막 요청을 제한하면 해당 영상은 오류 목록에 남고 나머지 채널은 계속 처리합니다.
- 박종훈팀장(바이킹스)은 공동 채널의 영상 제목에 `박종훈` 또는 `바이킹스`가 포함된 경우만 수집합니다.
- 유튜브 스크립트·데이터 사용 범위, 시장 데이터의 라이선스, 공개 문구는 정식 공개 전 별도 검토가 필요합니다.
