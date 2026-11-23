"""유튜브 웹사이트의 내부 검색을 직접 호출(키 불필요, 비공식).

- 검색어에 after:/before: 를 붙여 기간별 영상 목록과 조회수를 받음.
- 댓글 수와 정확한 게시일은 영상마다 한 번씩 따로 조회.
- 비공식이라 언제든 막히거나 형식이 바뀔 수 있음. 공식 API 키가 있으면 youtube.py 쪽을 씀.
"""
import datetime as dt
import re

import requests

from .youtube import YTError

SOURCE_NAME = "YouTube 검색(비공식)"
BASE = "https://www.youtube.com/youtubei/v1/"
CTX = {"client": {"clientName": "WEB", "clientVersion": "2.20250101.00.00", "hl": "en", "gl": "US"}}
_s = requests.Session()
_s.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/126.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9"})
SORT_DATE_VIDEOS = "CAISAhAB"   # 정렬: 업로드 날짜, 유형: 동영상


def _post(path, body):
    try:
        r = _s.post(f"{BASE}{path}?prettyPrint=false", json={"context": CTX, **body}, timeout=40)
    except requests.RequestException as e:
        raise YTError(f"연결 오류: {type(e).__name__}")
    if r.status_code != 200:
        raise YTError(f"HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError:
        raise YTError("응답 형식 오류(차단됐을 수 있음)")


def _walk(o, key, out):
    if isinstance(o, dict):
        for k, v in o.items():
            if k == key:
                out.append(v)
            else:
                _walk(v, key, out)
    elif isinstance(o, list):
        for v in o:
            _walk(v, key, out)
    return out


def _txt(o):
    if not o:
        return ""
    if isinstance(o, str):
        return o
    return o.get("simpleText") or "".join(r.get("text", "") for r in o.get("runs", []))


def _num(s):
    m = re.search(r"[\d,]+", s or "")
    return int(m.group().replace(",", "")) if m else 0


def search_page(term, start, end, token=None):
    """→ (영상 목록, 다음 페이지 토큰). 영상 = {id, title, channel, views, text}"""
    q = f'"{term}"' if " " in term else term
    body = {"continuation": token} if token else {
        "query": f"{q} after:{start} before:{end}", "params": SORT_DATE_VIDEOS}
    d = _post("search", body)
    vids = []
    for v in _walk(d, "videoRenderer", []):
        if not v.get("videoId"):
            continue
        title, channel = _txt(v.get("title")), _txt(v.get("ownerText"))
        snip = " ".join(_txt(s.get("snippetText")) for s in v.get("detailedMetadataSnippets", []))
        vids.append({"id": v["videoId"], "title": title, "channel": channel,
                     "views": _num(_txt(v.get("viewCountText"))),
                     "text": f"{title} {snip} {channel}".lower()})
    toks = [c["continuationCommand"]["token"] for c in _walk(d, "continuationEndpoint", [])
            if "continuationCommand" in c]
    return vids, (toks[0] if toks else None)


def details(video_id):
    """영상 한 개의 댓글 수·조회수·정확한 게시일. 댓글이 꺼져 있거나 0이면 comments=0."""
    d = _post("next", {"videoId": video_id})
    views = [_txt(x.get("viewCount")) for x in _walk(d, "videoViewCountRenderer", [])]
    if not views and not _walk(d, "dateText", []):
        raise YTError("영상 정보를 못 받음(차단됐을 수 있음)")
    info = [_txt(x) for x in _walk(d, "contextualInfo", [])]
    date = None
    for t in (_txt(x) for x in _walk(d, "dateText", [])):
        m = re.search(r"([A-Z][a-z]{2}) (\d{1,2}), (\d{4})", t)
        if m:
            try:
                date = dt.datetime.strptime(" ".join(m.groups()), "%b %d %Y").date().isoformat()
            except ValueError:
                pass
            break
    return {"comments": _num(info[0]) if info else 0, "views": _num(views[0]) if views else None, "published": date}
