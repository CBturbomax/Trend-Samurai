"""핀터레스트 트렌드(trends.pinterest.com) 주간 검색 지수 · 최근 2년 · 로그인 없이 공개된 값."""
import datetime as dt

import requests

URL = "https://trends.pinterest.com/metrics/"
HEAD = {"User-Agent": "Mozilla/5.0 (TrendSamurai personal research)", "Accept": "application/json, text/plain, */*",
        "Referer": "https://trends.pinterest.com/"}


class PinError(Exception):
    pass


def _sundays():
    d = dt.date.today()
    d -= dt.timedelta(days=(d.weekday() + 1) % 7)   # 이번 주 일요일(오늘이 일요일이면 오늘)
    for i in range(4):
        yield (d - dt.timedelta(weeks=i)).isoformat()


def fetch(term, country="US"):
    """{주 시작일: 지수} — 값이 없으면 {}. 마지막 일요일 자료가 아직 안 나왔으면 한 주씩 앞당겨 재시도."""
    last = None
    for end in _sundays():
        try:
            r = requests.get(URL, params={"terms": term.lower(), "country": country, "end_date": end, "days": 730,
                                          "aggregation": 2, "shouldMock": "false", "normalize_against_group": "true",
                                          "predicted_days": 0}, headers=HEAD, timeout=30)
        except requests.RequestException as e:
            raise PinError(type(e).__name__)
        if r.status_code == 400:
            last = 400
            continue
        if r.status_code != 200:
            raise PinError(f"HTTP {r.status_code}")
        out = {}
        for t in r.json() or []:
            for c in t.get("counts") or []:
                v = c.get("normalizedCount", c.get("count"))
                if c.get("date") and v is not None:
                    out[c["date"][:10]] = float(v)
        return out
    return {} if last == 400 else {}
