"""arctic-shift (제3자 레딧 아카이브) 호출. 다른 소스로 바꾸려면 이 파일만 교체."""
import collections
import datetime as dt

import requests

from ..config import ARCTIC_BASE, START_DATE

SOURCE_NAME = "arctic-shift"


class SlowDown(Exception):
    """422 시간 초과 / 429 / 연결 오류. wait = 서버가 알려준 대기 시간(초) 또는 None."""

    def __init__(self, msg, wait=None):
        super().__init__(msg)
        self.wait = wait


class BadRequest(Exception):
    """다시 시도해도 소용없는 오류(400, 없는 서브레딧 등)."""


_s = requests.Session()
_s.headers["User-Agent"] = "keyword-tracker (personal research)"


def _get(path, params, timeout=60):
    try:
        r = _s.get(ARCTIC_BASE + path, params=params, timeout=timeout)
    except requests.RequestException as e:
        raise SlowDown(f"연결 오류: {type(e).__name__}")
    if r.status_code == 200:
        return r.json().get("data") or []
    if r.status_code == 429:
        wait = r.headers.get("X-RateLimit-Reset")
        raise SlowDown("429 요청 제한", float(wait) if wait else None)
    if r.status_code == 400:
        raise BadRequest(r.text[:200])
    raise SlowDown(f"HTTP {r.status_code}")


def monthly_totals(sub, what):
    """what = posts | comments. → {'2024-01': 49, ...} (서브레딧 생기기 전 달은 없음)"""
    data = _get("/api/time_series", {
        "key": f"r/{sub}/{what}/count", "precision": "month", "after": START_DATE})
    out = {}
    for d in data:
        m = dt.datetime.fromtimestamp(d["date"], dt.timezone.utc).strftime("%Y-%m")
        out[m] = int(d["value"])
    return out


def search_posts(sub, term, after, before, field="title"):
    """제목에 term이 든 글의 created_utc 목록(최대 100개, 오래된 순)."""
    data = _get("/api/posts/search", {
        "subreddit": sub, field: term, "after": after, "before": before,
        "limit": 100, "sort": "asc", "fields": "created_utc"})
    return [d["created_utc"] for d in data]


def find_subreddits(prefix):
    data = _get("/api/subreddits/search", {
        "subreddit_prefix": prefix, "limit": 10,
        "fields": "display_name,created_utc,subscribers"})
    return [{"name": d["display_name"], "subscribers": d.get("subscribers") or 0,
             "created_utc": d.get("created_utc")} for d in data]


def top_authors(sub, n=8):
    """최근 글 100개 기준, 글을 많이 쓴 사람."""
    data = _get("/api/posts/search", {
        "subreddit": sub, "limit": 100, "sort": "desc", "fields": "author"})
    c = collections.Counter(d["author"] for d in data
                            if d.get("author") not in (None, "[deleted]", "AutoModerator"))
    return [a for a, _ in c.most_common(n)]


def author_subreddits(author, n=20):
    data = _get("/api/users/interactions/subreddits", {
        "author": author, "limit": n, "after": "1year"})
    return [(d["subreddit"], d["count"]) for d in data]
