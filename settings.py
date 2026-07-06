# =====================================================================
#  [운영자 수정 영역]  ← 코딩 몰라도 됩니다. 이 파일의 값만 고치세요.
#  수정 후 GitHub에 저장하면 봇이 자동으로 다시 배포되며 반영됩니다.
# =====================================================================

# 1) 현재 집중 상품 (이슈와 매칭할 KODEX 라인업)
#    name = 상품명, code = 종목코드.  줄을 추가/삭제하면 됩니다.
FOCUS_PRODUCTS = [
    {"name": "KODEX 삼성전자SK하이닉스채권혼합50", "code": "0177N0"},
    {"name": "KODEX 전고체배터리ESS TOP2플러스", "code": "0209D0"},
    {"name": "KODEX AI반도체TOP2플러스", "code": "395160"},
]

# 2) 연동 상품 세트 (함께 제안하면 자연스러운 조합) — 없으면 [] 로 비워둡니다.
#    members = 위 종목코드들, note = 어떻게 묶을지 설명.
PRODUCT_SETS = []

# 3) 하루 소재 후보 개수
CANDIDATE_COUNT = 3

# 4) 사용 모델  (정리 품질이 더 좋은 상위 모델)
#    모델/요금 최신정보: https://docs.claude.com/en/docs/about-claude/models
MODEL = "claude-sonnet-4-6"

# 5) 매일 발송 시각 (24시간 표기, 한국시간 기준)
SCHEDULE_HOUR = 9
SCHEDULE_MINUTE = 0
# 오후 장 마감 브리핑 시각 (장 마감 후 데이터가 올라올 시간을 두어 15:40)
SCHEDULE_PM_HOUR = 15
SCHEDULE_PM_MINUTE = 45
TIMEZONE = "Asia/Seoul"

# 5-1) 추정 비용 계산용 단가 (USD per 1M tokens). 참고용이며 실제 청구는 Anthropic Console 기준.
#      최신 단가: https://www.anthropic.com/pricing  (모델 변경 시 이 값도 함께 맞추세요)
PRICE_INPUT_PER_MTOK = 15.0
PRICE_OUTPUT_PER_MTOK = 75.0

# 6) 응답 최대 길이 (토큰). 보통 그대로 두면 됩니다.
MAX_TOKENS = 4000

# =====================================================================
#  여기 아래는 건드리지 마세요 (코드가 사용하는 부분)
# =====================================================================
