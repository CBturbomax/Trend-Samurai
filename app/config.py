"""설정. 환경변수로 바꿀 수 있음."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = os.environ.get("KT_DB", str(ROOT / "data" / "tracker.db"))
START_DATE = "2019-01-01"

ARCTIC_BASE = os.environ.get("KT_ARCTIC_BASE", "https://arctic-shift.photon-reddit.com")
ARCTIC_GAP = float(os.environ.get("KT_ARCTIC_GAP", "4"))      # 요청 사이 간격(초)
BACKOFF_FREE = 3                                               # 처음 이만큼의 연속 실패는 5초만 쉬고 재시도
BACKOFF_START = 15                                             # 그 뒤 대기(초), 실패마다 2배
BACKOFF_MAX = 600                                              # 최대 대기(초)
TRIES_BEFORE_SHRINK = 6                                        # 같은 창 재시도 횟수
MIN_WINDOW_DAYS = 8                                            # 이보다 짧으면 더 안 쪼개고 보류
SUB_GIVEUP_STREAK = 30                                         # 한 서브레딧이 연속 이만큼 실패하면 통째 보류

TRENDS_PROVIDER = os.environ.get("KT_TRENDS_PROVIDER", "pytrends")  # pytrends | serpapi
SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")
TRENDS_COMBOS = [("", "US"), ("youtube", "US"), ("", ""), ("youtube", "")]  # (gprop, geo)

DAILY_HOUR = int(os.environ.get("KT_DAILY_HOUR", "6"))        # 매일 증분 업데이트 시각(로컬)
