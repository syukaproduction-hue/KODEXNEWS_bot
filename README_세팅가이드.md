# KODEX 시황 뉴스봇 — 세팅 가이드 (비개발자용)

이 가이드는 **코딩을 몰라도** 브라우저에서 클릭과 복붙만으로 따라 할 수 있게 만들었습니다.
처음 한 번만 좀 번거롭고, 그 뒤로는 알아서 매일 돕니다. 막히면 어느 단계인지 알려주세요.

전체 흐름: ① 텔레그램 봇 만들기 → ② Anthropic API 키 받기 → ③ 코드를 GitHub에 올리기
→ ④ Railway에 배포 → ⑤ 채팅 ID 연결 → ⑥ 테스트. (대략 30~40분)

> 보안 원칙: **봇 토큰과 API 키는 절대 코드나 채팅에 쓰지 마세요.** ④단계의 'Variables'에만 입력합니다.

---

## ① 텔레그램 봇 만들기 (5분, 무료)

1. 텔레그램에서 **@BotFather** 를 검색해 대화를 엽니다.
2. `/newbot` 입력 → 봇 이름 → 사용자명(영문, 끝이 `bot`)을 정합니다.
3. BotFather가 주는 **토큰**(예: `8123...:AAF...`)을 복사해 둡니다. → 이게 `TELEGRAM_BOT_TOKEN`.

## ② Anthropic API 키 받기 (5분)

1. https://console.anthropic.com 에 **본인이 직접** 가입/로그인합니다. (제가 대신 만들 수 없어요.)
2. 좌측 **API Keys** → **Create Key** → 키를 복사해 둡니다. → 이게 `ANTHROPIC_API_KEY`.
3. **Billing**(결제수단)을 등록하고 소액(예: $5~10) 크레딧을 충전합니다. 웹 검색 포함 브리핑 1회는
   보통 수십 원~수백 원 수준이지만, 정확한 단가는 https://docs.claude.com/en/docs/about-claude/models 에서 확인하세요.

## ③ 코드를 GitHub에 올리기 (10분)

1. https://github.com 에 가입/로그인합니다.
2. 우측 상단 **+ → New repository** → 이름(예: `kodex-bot`) → **Private** 선택 → **Create**.
3. 새 저장소 화면에서 **uploading an existing file** 링크 클릭.
4. 제가 드린 압축파일을 푼 뒤, 그 **안의 파일들**(bot.py, settings.py, briefing_prompt.md,
   requirements.txt, Procfile, .gitignore)을 드래그해서 올리고 **Commit changes**.
   (`.env.example`는 올려도 되고 안 올려도 됩니다. 실제 `.env`는 만들지 않습니다.)

## ④ Railway에 배포 (10분)

1. https://railway.app 에 **GitHub 계정으로** 로그인합니다.
2. **New Project → Deploy from GitHub repo →** 방금 만든 `kodex-bot` 선택.
3. 배포가 시작되면 서비스 카드를 클릭 → **Variables** 탭에서 아래 3개를 추가합니다.
   - `TELEGRAM_BOT_TOKEN` = ①의 토큰
   - `ANTHROPIC_API_KEY` = ②의 키
   - `TARGET_CHAT_ID` = (아직 비워두거나 생략. ⑤에서 채웁니다.)
4. **Settings** 탭에서 시작 명령(Start Command)이 비어 있으면 `python bot.py` 로 지정합니다.
   (Procfile이 있어 보통 자동 인식됩니다.)
5. 저장하면 자동으로 다시 배포됩니다. **Deploy Logs** 에 `봇 시작` 로그가 보이면 성공.

## ⑤ 채팅 ID 연결 (5분)

자동 발송을 받을 곳(예: 슈카친구들 직원 단톡방)을 봇에게 알려주는 단계입니다.

1. 그 단톡방에 ①에서 만든 봇을 **멤버로 초대**합니다. (1:1로 받을 거면 봇과 직접 대화)
2. 그 방에서 `/chatid` 라고 보냅니다. 봇이 `이 채팅 ID: -100xxxxxxxxxx` 처럼 답합니다.
3. 그 숫자를 복사 → Railway **Variables** 의 `TARGET_CHAT_ID` 에 붙여넣고 저장 → 자동 재배포.

## ⑥ 테스트 & 자동 발송

- 아무 방에서 `/brief` 를 보내면 **즉시** 브리핑을 만들어 줍니다. (웹 검색 때문에 30초~1분)
- 이상 없으면 끝! 이후 **평일 오전 9시**(한국시간)에 ⑤의 방으로 자동 발송됩니다.
- 내일이 월요일이라면, 9시 브리핑이 자동으로 주말 이슈까지 포함해 다룹니다.

---

## 나중에 수정하는 법 (코딩 아님)

- **밀고 있는 상품 변경**: GitHub에서 `settings.py` 를 열고(연필 아이콘) `FOCUS_PRODUCTS` 줄을
  추가/수정 → **Commit**. Railway가 자동 재배포하며 반영됩니다.
- **후보 개수·발송 시각·모델 변경**: 같은 `settings.py` 의 해당 값만 수정.
- **브리핑 형식·말투·규칙 변경**: `briefing_prompt.md` 를 수정. (이 대화에서 저와 먼저 다듬은 뒤
  최종본을 붙여넣는 걸 추천합니다.)

## 자주 나는 오류

- 봇이 자동 발송을 안 함 → `TARGET_CHAT_ID` 가 비었는지 확인(⑤). 그룹은 ID가 `-100...` 으로 시작.
- `/brief` 가 오류 → Railway Variables 의 `ANTHROPIC_API_KEY` 오타/크레딧 잔액 확인.
- 로그에 401/Unauthorized → 키가 잘못됨. 키를 다시 발급해 Variables 만 교체(코드 수정 불필요).
