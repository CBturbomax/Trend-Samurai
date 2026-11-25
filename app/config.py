"""설정. 환경변수로 바꿀 수 있음."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = os.environ.get("KT_DB", str(ROOT / "data" / "tracker.db"))
START_DATE = "2019-01-01"

ARCTIC_BASE = os.environ.get("KT_ARCTIC_BASE", "https://arctic-shift.photon-reddit.com")
ARCTIC_GAP = float(os.environ.get("KT_ARCTIC_GAP", "3"))      # 요청 사이 간격(초)
BACKOFF_FREE = 20                                              # 처음 이만큼의 연속 실패는 2초만 쉬고 바로 재시도(실측: 오래 쉬면 오히려 더 실패)
FAST_RETRY = 2                                                 # 빠른 재시도 간격(초)
BACKOFF_START = 15                                             # 그 뒤 대기(초), 실패마다 2배
BACKOFF_MAX = 600                                              # 최대 대기(초)
TRIES_BEFORE_SHRINK = 25                                       # 같은 창 재시도 횟수
MIN_WINDOW_DAYS = 8                                            # 이보다 짧으면 더 안 쪼개고 보류
SUB_GIVEUP_STREAK = 90                                         # 한 서브레딧이 연속 이만큼 실패하면 통째 보류

# 레딧 전체 검색(PullPush). 무료 자동 수집은 운영자가 막아 둠(429 "no free scraping for agents").
# 운영자와 유료 이용을 협의한 경우에만 KT_PULLPUSH=1 로 켤 것.
PULLPUSH_ENABLED = os.environ.get("KT_PULLPUSH", "") == "1"
PULLPUSH_GAP = float(os.environ.get("KT_PULLPUSH_GAP", "7"))    # 레딧 전체 검색 요청 간격(초)
PULLPUSH_BLOCK_WAIT = 300                                      # 429 뒤 쉬는 시간(초), 연속이면 2배씩 최대 1시간

YT_API_KEY = os.environ.get("YT_API_KEY", "")                   # 없으면 화면에서 입력한 키(DB 저장)를 씀
YT_WEB_GAP = float(os.environ.get("KT_YT_WEB_GAP", "1.5"))      # 키 없는 방식: 요청 간격(초)
YT_WEB_FIRST_PAGES = 3                                         # 1차: 달마다 3페이지만(빨리 전부 채우기)
YT_WEB_MAX_PAGES = 8                                           # 한 달·한 변형당 최대 페이지(약 320개). 넘으면 "이상"으로 표시
YT_COMMENT_BATCH = 40                                          # 댓글 수를 한 번에 몇 영상씩 받을지
YT_COMMENT_STALE_DAYS = 30                                     # 이만큼 지나면 댓글 수를 다시 받음
YT_MAX_PAGES = 10                                              # 검색 한 번에 최대 500개까지만 나옴 → 넘으면 기간을 쪼갬

AUTO_DISCOVER = False                                          # 커뮤니티 자동 선별(끔: 카테고리별로 직접 지정, 수집 속도 우선)
AUTO_ADD_MIN_HITS = 3                                          # 추천 서브레딧: 최근 6개월 언급 글이 이만큼 이상이면 자동 추적
AUTO_ADD_MAX = 12                                              # 키워드당 자동으로 추가하는 서브레딧 수 한도

YT_WEB_START = "2023-12"                                       # 키 없는 유튜브 수집 시작 달(속도 위해 범위 축소)
MENTION_MULTIYEAR = False                                      # 여러 해를 한 번에 묻기(아카이브 시간 초과가 잦아 끔, 해 단위로)
MENTION_POST_START = "2023-01"                                 # 레딧 글 언급 기본 수집 시작 달
MENTION_FIELD = "query"                                        # 글 언급: 제목+본문(query). 제목만이면 "title"
# 글이 아주 많은 일반 커뮤니티는 본문 검색이 너무 무거워(아카이브 시간 초과) 제목만 검색 — 같은 커뮤니티 안에선 기준이 일정해 추세는 유효
GIANT_SUBS = {s.lower() for s in """anime gaming games pcgaming truegaming japan japantravel japantraveltips tokyo japanlife costco traderjoes
femalefashionadvice malefashionadvice streetwear sneakers running watches buyitforlife cooking fastfood edc toys collectibles
nostalgia 90s fishing cycling bicycling camping campinggear hiking loseit gym supplements nutrition nyc foodporn
skincareaddiction makeupaddiction beauty asianbeauty sephora ulta tennis 10s pickleball parenting toddlers daddit
smartglasses gadgets kawaii plushies blindboxes actionfigures tcg anime_irl""".split()}
MENTION_COMMENTS = False                                       # 댓글 본문 언급 검색(무겁고 거의 안 받아짐 → 끔, 글 언급에 집중)
COMMENT_MONTHS = 24                                            # 레딧 댓글 언급은 최근 이만큼의 달만

TRENDS_PROVIDER = os.environ.get("KT_TRENDS_PROVIDER", "pytrends")  # pytrends | serpapi
SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")
TRENDS_COMBOS = [("", "US"), ("youtube", "US"), ("", ""), ("youtube", "")]  # (gprop, geo)

DAILY_HOUR = int(os.environ.get("KT_DAILY_HOUR", "6"))        # 매일 증분 업데이트 시각(로컬)
