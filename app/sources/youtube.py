"""YouTube Data API v3 (공식). 키워드가 든 영상을 기간별로 찾고 조회수·댓글 수를 가져옴.

- 하루 무료 한도 10,000 단위. 검색 1회 = 100 단위(영상 50개), 통계 조회 1회 = 1 단위(영상 50개).
- 과거의 '월별 조회수 증가'는 API가 주지 않음. 줄 수 있는 건 영상별 '현재 누적' 조회수뿐.
"""
import requests

BASE = "https://www.googleapis.com/youtube/v3/"
SOURCE_NAME = "YouTube Data API"
_s = requests.Session()


class QuotaExceeded(Exception):
    pass


class KeyInvalid(Exception):
    pass


class YTError(Exception):
    pass


def _get(path, params):
    try:
        r = _s.get(BASE + path, params=params, timeout=40)
    except requests.RequestException as e:
        raise YTError(f"연결 오류: {type(e).__name__}")
    if r.status_code == 200:
        return r.json()
    try:
        err = r.json()["error"]
        reason = (err.get("errors") or [{}])[0].get("reason", "")
        msg = err.get("message", "")
    except Exception:
        reason, msg = "", r.text[:120]
    if reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
        raise QuotaExceeded(reason)
    if r.status_code in (400, 401, 403) and ("API key" in msg or reason in (
            "keyInvalid", "badRequest", "forbidden", "accessNotConfigured", "ipRefererBlocked")):
        raise KeyInvalid(msg[:160] or reason)
    raise YTError(f"HTTP {r.status_code} {reason} {msg[:100]}")


def query_for(terms):
    """변형들을 OR(|)로 묶음. 띄어쓰기가 있으면 따옴표."""
    return "|".join(f'"{t}"' if " " in t else t for t in terms)


def search(key, q, start, end, page_token=None):
    """start/end = 'YYYY-MM-DD'. → (영상 id 목록, 다음 페이지 토큰)"""
    p = {"part": "id", "type": "video", "q": q, "maxResults": 50, "order": "date",
         "publishedAfter": f"{start}T00:00:00Z", "publishedBefore": f"{end}T00:00:00Z", "key": key}
    if page_token:
        p["pageToken"] = page_token
    d = _get("search", p)
    ids = [i["id"]["videoId"] for i in d.get("items", []) if i.get("id", {}).get("videoId")]
    return ids, d.get("nextPageToken")


def videos(key, ids):
    """영상 50개까지의 제목·채널·게시일·조회수·좋아요·댓글 수."""
    d = _get("videos", {"part": "snippet,statistics", "id": ",".join(ids), "maxResults": 50, "key": key})
    out = []
    for i in d.get("items", []):
        sn, st = i.get("snippet", {}), i.get("statistics", {})
        out.append({
            "id": i["id"], "published": sn.get("publishedAt", "")[:10], "title": sn.get("title", ""),
            "channel": sn.get("channelTitle", ""),
            "text": " ".join([sn.get("title", ""), sn.get("description", ""), " ".join(sn.get("tags", []))]).lower(),
            "views": int(st.get("viewCount", 0)), "likes": int(st.get("likeCount", 0)),
            "comments": int(st.get("commentCount", 0)),
        })
    return out


def check_key(key):
    _get("videos", {"part": "id", "id": "jNQXAC9IVRw", "key": key})
