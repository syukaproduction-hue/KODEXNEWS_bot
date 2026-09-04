# 내일 섹터 한표

기존 `KODEXNEWS_bot` 서비스와 파일을 건드리지 않고 `sector_vote/`에 분리한 독립 FastAPI 웹앱입니다.
경제·금융 유튜브 20개 채널의 공개 영상 스크립트에서 **다음 거래일 섹터 방향**만 AI로 분류해, 인물 순위 없이 섹터 합계만 공개합니다.

## 공개 화면

- `/` — 내일 섹터 한표
- `/methodology` — 집계 기준과 분석 채널
- `/api/summary` — 최근 36시간 합의, 최근 7일 관심 섹터, 채널별 짧은 근거 JSON
- `/health` — 상태 확인

## 보호된 운영 API

모든 요청에 `X-Admin-Token: $SECTOR_ADMIN_TOKEN` 헤더가 필요합니다.

- `POST /api/refresh` — 최근 36시간 내 채널 영상의 자막을 자동 수집·분류
- `GET /api/refresh/status` — 최근 작업 결과와 오류 확인
- `GET /api/evidence` — 채널명·영상 링크·짧은 인용을 포함한 내부 검증 자료
- `POST /api/ingest/transcript` — 외부 수집기가 전달한 스크립트 분석

영상 전문은 DB에 저장하지 않습니다. 공개 화면에는 유튜버별 순위를 만들지 않고, 판정 투명성을 위한 채널명·120자 이내 인용·원본 영상 링크만 제공합니다.

## 집계 원칙

1. `next_session`으로 분류된 다음 거래일 전망만 공개 집계합니다.
2. 한 채널은 한 섹터에 한 표만 반영하며, 같은 섹터에 여러 발언이 있으면 최신 판정을 사용합니다.
3. 구독자 수·인지도 가중치를 사용하지 않습니다.
4. 장기 전망, 개별 종목, ETF·상품 추천은 집계에서 제외합니다.
5. 메인 합의는 최근 36시간, 관심 섹터는 최근 7일을 기준으로 표시합니다.
6. 채널별 근거는 순위가 아니라 해당 표의 출처 확인용으로만 제공합니다.

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
   - `SUPADATA_API_KEY` — Railway IP 차단을 우회하는 관리형 자막 API 키
   - `SECTOR_ADMIN_TOKEN` — 길고 무작위인 별도 값
   - `SECTOR_DB_PATH=/data/sector_vote.db`
5. Health check: `/health`

## 서버 자동 수집

Railway의 클라우드 IP는 YouTube 자막 요청이 차단될 수 있으므로, 운영 서버에서는 Supadata의 관리형 Transcript API를 사용합니다.

1. Supadata에서 API 키를 발급합니다: <https://supadata.ai/>
2. Railway 새 섹터 서비스의 Variables에 `SUPADATA_API_KEY`를 추가합니다.
3. 재배포되면 서버 시작 직후 첫 수집을 실행하고, 이후 기본 360분(6시간)마다 자동 실행합니다.
4. 주기를 바꾸려면 `SECTOR_AUTO_REFRESH_MINUTES`를 설정합니다. `0`이면 자동 실행을 끕니다.
5. `/health`의 `automation`에서 활성화 여부·주기·공급자를 확인합니다.
6. 상세 성공·실패 내역은 관리자 토큰으로 `/api/refresh/status`에서 확인합니다.

Supadata 요청은 `mode=native`로 고정해 기존 YouTube 자막만 가져오며, 비용이 큰 AI 음성 전사는 자동 실행하지 않습니다. 공급자 문서상 네이티브 자막 1건은 1크레딧입니다. 유료 호출 전 SQLite에 영상 작업을 원자적으로 선점하므로 중복 프로세스가 같은 영상을 동시에 결제하지 않습니다. 자막 없음은 영구 제외하고, 일시 오류는 6시간 뒤에만 재시도합니다.

운영 데이터는 최근 30일 메타데이터·짧은 인용·판정만 보관하며 자동 갱신 때 이전 자료를 삭제합니다. 전체 자막은 저장하지 않습니다. Docker 기본 명령은 Uvicorn 단일 worker이며, Railway에서도 기본 1 replica 운영을 권장합니다. 복수 프로세스가 겹쳐도 SQLite 작업 선점이 동일 영상 중복 결제를 방지합니다.

## 비상용 로컬 수집기

관리형 자막 API 장애나 크레딧 소진 때만 Windows PC 수집기를 비상 수단으로 사용할 수 있습니다. 평상시 운영에는 로컬 PC가 필요하지 않습니다.

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

## 수동 갱신

자동 주기 사이에 즉시 다시 확인하려면 아래 보호 API를 호출합니다.

```bash
curl -X POST "https://새-서비스주소/api/refresh" \
  -H "X-Admin-Token: $SECTOR_ADMIN_TOKEN"
```

갱신은 백그라운드에서 실행되며 요청은 즉시 `202 Accepted`를 반환합니다. 작업 결과와 채널별 오류는 `/api/refresh/status`에서 확인합니다.

## 환경변수

| 변수 | 필수 | 설명 |
|---|---:|---|
| `ANTHROPIC_API_KEY` | 예 | 스크립트 섹터 분류 |
| `SUPADATA_API_KEY` | 자동화 시 예 | 관리형 YouTube 자막 수집 |
| `SECTOR_ADMIN_TOKEN` | 예 | 운영 API 보호 |
| `SECTOR_DB_PATH` | 권장 | 운영에서는 `/data/sector_vote.db` |
| `SECTOR_AUTO_REFRESH_MINUTES` | 아니오 | 기본 360분, `0`이면 자동화 끔 |
| `PORT` | Railway 자동 | Uvicorn 포트 |

## 운영 유의사항

- 네이티브 자막이 없는 영상은 오류 목록에 남기고 나머지 채널을 계속 처리합니다. AI 음성 전사는 비용 통제를 위해 사용하지 않습니다.
- 박종훈팀장(바이킹스)은 공동 채널의 영상 제목에 `박종훈` 또는 `바이킹스`가 포함된 경우만 수집합니다.
- 유튜브 스크립트·데이터 사용 범위, 시장 데이터의 라이선스, 공개 문구는 정식 공개 전 별도 검토가 필요합니다.
