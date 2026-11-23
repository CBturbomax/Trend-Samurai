"""구글 트렌드. KT_TRENDS_PROVIDER=pytrends(기본) | serpapi 로 교체 가능.

fetch(terms, gprop, geo, start, end) → {term: [(month 'YYYY-MM', value, partial), ...]}
- 한 번에 최대 5개 검색어. 같이 요청한 검색어끼리만 높이 비교 가능.
- 5년 넘는 기간은 구글이 월 단위로 줌 → 그대로 사용.
"""
import warnings

import requests

from ..config import SERPAPI_KEY, TRENDS_PROVIDER

SOURCE_NAME = "Google Trends"


class TrendsBlocked(Exception):
    pass


def _pytrends(terms, gprop, geo, start, end):
    warnings.simplefilter("ignore")
    from pytrends.request import TrendReq
    try:
        p = TrendReq(hl="en-US", tz=0, timeout=(10, 30))
        p.build_payload(terms, timeframe=f"{start} {end}", geo=geo, gprop=gprop)
        df = p.interest_over_time()
    except Exception as e:  # pytrends는 429 등을 여러 예외로 던짐
        raise TrendsBlocked(f"{type(e).__name__}: {str(e)[:120]}")
    out = {t: [] for t in terms}
    if df.empty:
        return out
    for ts, row in df.iterrows():
        for t in terms:
            out[t].append((ts.strftime("%Y-%m"), float(row[t]), bool(row.get("isPartial", False))))
    return out


def _serpapi(terms, gprop, geo, start, end):
    """유료 대안. 실제 호출 시험은 못 함(키 없음)."""
    if not SERPAPI_KEY:
        raise TrendsBlocked("SERPAPI_KEY 없음")
    params = {"engine": "google_trends", "q": ",".join(terms), "date": f"{start} {end}",
              "data_type": "TIMESERIES", "api_key": SERPAPI_KEY}
    if geo:
        params["geo"] = geo
    if gprop:
        params["gprop"] = gprop
    try:
        r = requests.get("https://serpapi.com/search.json", params=params, timeout=60)
        r.raise_for_status()
        timeline = r.json()["interest_over_time"]["timeline_data"]
    except Exception as e:
        raise TrendsBlocked(f"{type(e).__name__}: {str(e)[:120]}")
    import datetime as dt
    out = {t: [] for t in terms}
    for row in timeline:
        m = dt.datetime.fromtimestamp(int(row["timestamp"]), dt.timezone.utc).strftime("%Y-%m")
        for v in row["values"]:
            if v["query"] in out:
                out[v["query"]].append((m, float(v.get("extracted_value", 0)), bool(row.get("partial_data"))))
    return out


def fetch(terms, gprop, geo, start, end):
    fn = _serpapi if TRENDS_PROVIDER == "serpapi" else _pytrends
    return fn(terms[:5], gprop, geo, start, end)
