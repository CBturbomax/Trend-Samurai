"""PullPush (제3자 레딧 아카이브) — 서브레딧 지정 없이 레딧 전체에서 단어 검색.

- 글은 제목+본문, 댓글은 본문에서 찾음.
- 실제 레딧보다 몇 주 늦음 → latest() 로 어디까지 들어와 있는지 확인해서 씀.
- 요청 제한이 빡빡함(429). 막힌 동안 계속 두드리면 더 오래 막히므로 한참 쉬어야 함.
"""
import requests

from .arctic import BadRequest, SlowDown

SOURCE_NAME = "PullPush"
BASE = "https://api.pullpush.io/reddit/search/"
_s = requests.Session()
_s.headers["User-Agent"] = "keyword-tracker (personal research)"


def _get(kind, params):
    try:
        r = _s.get(f"{BASE}{kind}/", params=params, timeout=60)
    except requests.RequestException as e:
        raise SlowDown(f"연결 오류: {type(e).__name__}")
    if r.status_code == 200:
        try:
            return r.json().get("data") or []
        except ValueError:
            raise SlowDown("응답 형식 오류")
    if r.status_code == 400:
        raise BadRequest(r.text[:200])
    raise SlowDown(f"HTTP {r.status_code}" + (" 요청 제한" if r.status_code == 429 else ""))


def search(kind, term, after, before):
    """kind = submission | comment. after/before = epoch 초. → [(id, created_utc, subreddit)] 오래된 순, 최대 100개"""
    q = f'"{term}"' if " " in term else term
    data = _get(kind, {"q": q, "after": after, "before": before, "size": 100,
                       "sort": "asc", "sort_type": "created_utc"})
    return [(d["id"], int(d["created_utc"]), d.get("subreddit") or "?") for d in data]


def latest(kind):
    """아카이브에 들어온 가장 최근 글/댓글 시각(epoch 초)."""
    data = _get(kind, {"size": 1, "sort": "desc", "sort_type": "created_utc"})
    if not data:
        raise SlowDown("빈 응답")
    return int(data[0]["created_utc"])
