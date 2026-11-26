import base64
import calendar
import datetime as dt
from datetime import datetime, timezone
import csv
import io
import json
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config as C
from . import autotune, db, seed, variants, worker
from .db import q, x
from .sources import arctic, youtube


@asynccontextmanager
async def lifespan(app):
    db.init()
    seed.load()   # seed/ 폴더의 키워드·틱톡 과거 추이를 채움
    if not C.PULLPUSH_ENABLED:
        x("DELETE FROM jobs WHERE kind='global' AND status!='done'")
    # 범위 축소: 유튜브는 YT_WEB_START부터, 레딧 댓글 언급은 최근 COMMENT_MONTHS개월만 — 범위 밖 대기 작업 정리
    full = "(SELECT CAST(substr(key,6) AS INTEGER) FROM meta WHERE key LIKE 'full:%')"
    x(f"DELETE FROM jobs WHERE kind='yt' AND status!='done' AND start<? AND keyword_id NOT IN {full}",
      (C.YT_WEB_START + "-01",))
    x(f"DELETE FROM jobs WHERE kind='mention' AND term NOT LIKE 'c:%' AND status!='done' AND start<? "
      f"AND keyword_id NOT IN {full}", (C.MENTION_POST_START + "-01",))
    x(f"DELETE FROM jobs WHERE kind='mention' AND term LIKE 'c:%' AND status!='done' AND start<? "
      f"AND keyword_id NOT IN {full}", (worker.all_months()[-C.COMMENT_MONTHS] + "-01",))
    # 언급 커뮤니티가 아직 하나도 없는 키워드는 선별을 맨 앞에서(이게 끝나야 언급 차트가 생김), 재선별은 뒤로
    x("UPDATE jobs SET priority=CASE WHEN keyword_id IN (SELECT keyword_id FROM subreddits WHERE kind='mention') "
      "THEN 4 ELSE 1 END WHERE kind='discover' AND status IN ('queued','hold')")
    for (g, r), pr in worker.TREND_PRIO.items():
        x("UPDATE jobs SET priority=? WHERE kind='trends' AND gprop=? AND geo=? AND status IN ('queued','hold')", (pr, g, r))
    x("UPDATE jobs SET priority=5 WHERE kind='mention' AND term LIKE 'c:%' AND status IN ('queued','hold')")
    x("DELETE FROM jobs WHERE kind='tiktok' AND status!='done'")   # 매일 누적 기록은 안 함
    x("DELETE FROM jobs WHERE kind='ytstats' AND status!='done'")   # 유튜브 매일 누적 기록도 안 함
    x("DELETE FROM jobs WHERE kind='ytc' AND status!='done'")   # 유튜브 댓글 수는 수집하지 않음
    # 글 언급 기준이 바뀌면(제목만 → 제목+본문) 이전 집계를 지우고 다시 셈
    if (q("SELECT value FROM meta WHERE key='mention_field'") or [{"value": "title"}])[0]["value"] != C.MENTION_FIELD:
        x("DELETE FROM mention_windows WHERE term NOT LIKE 'c:%'")
        x("DELETE FROM jobs WHERE kind='mention' AND term NOT LIKE 'c:%' AND status!='done'")
        x("INSERT OR REPLACE INTO meta VALUES('mention_field',?,?)", (C.MENTION_FIELD, time.time()))
    # '새 키워드 우선'은 추가 후 6시간까지만 → 지난 키워드의 앞당김은 되돌림(중요도 순서가 먹히게)
    x("UPDATE jobs SET priority=priority+10 WHERE priority<0 AND status IN ('queued','hold') AND keyword_id IN "
      "(SELECT id FROM keywords WHERE created_at < ?)", ((datetime.now(timezone.utc) - __import__('datetime').timedelta(hours=6)).isoformat(),))
    # 지금 쓰지 않는 검색어의 대기 작업 정리
    used = set()
    for k in q("SELECT id FROM keywords"):
        used.update(worker.reddit_terms(k["id"]))
    for r in q("SELECT DISTINCT term FROM jobs WHERE kind='mention' AND status!='done' AND term NOT LIKE 'c:%'"):
        if r["term"] not in used:
            x("DELETE FROM jobs WHERE kind='mention' AND status!='done' AND term=?", (r["term"],))
    # 거대 커뮤니티는 제목만 검색하도록 바뀜 → 그 커뮤니티의 본문 기준 집계는 지우고 다시(한 번만)
    if not q("SELECT 1 FROM meta WHERE key='giant_title_v1'"):
        gs = list(C.GIANT_SUBS)
        x(f"DELETE FROM mention_windows WHERE term NOT LIKE 'c:%' AND lower(sub) IN ({','.join('?'*len(gs))})", gs)
        x(f"DELETE FROM jobs WHERE kind='mention' AND status!='done' AND lower(sub) IN ({','.join('?'*len(gs))})", gs)
        x("INSERT OR REPLACE INTO meta VALUES('giant_title_v1','1',?)", (time.time(),))
    if not C.MENTION_COMMENTS:   # 댓글 언급 검색 대기분 정리(글 언급에 집중)
        x("DELETE FROM jobs WHERE kind='mention' AND term LIKE 'c:%' AND status!='done'")
    if not C.AUTO_DISCOVER:
        x("DELETE FROM jobs WHERE kind='discover' AND status IN ('queued','hold')")
    x("UPDATE jobs SET status='queued', next_at=0, tries=0 WHERE kind='yt' AND status='hold'")   # 유튜브 보류분은 재시작 때 다시
    # 레딧 글: 해마다 따로 묻던 대기 작업을 지우고 여러 해를 한 번에 묻는 작업으로 다시 넣음
    x("DELETE FROM jobs WHERE kind='mention' AND term NOT LIKE 'c:%' AND status='queued' AND tries=0 "
      "AND julianday(end)-julianday(start) BETWEEN 300 AND 400")   # 틱톡 해시태그 일일 기록은 그만둠(의미 없음)
    # 레딧 댓글: 달마다 따로 묻던 대기 작업을 지우고, 빈 달 구간을 한 번에 묻는 작업으로 다시 넣음(요청 수 대폭 감소)
    x("DELETE FROM jobs WHERE kind='mention' AND term LIKE 'c:%' AND status IN ('queued','hold') "
      "AND julianday(end)-julianday(start)<=31")
    x("DELETE FROM jobs WHERE kind='mention' AND term LIKE 'c:%' AND status='queued' AND tries=0")
    if not C.MENTION_MULTIYEAR:   # 여러 해짜리 대기분은 지우고 해 단위로 다시
        x("DELETE FROM jobs WHERE kind='mention' AND status IN ('queued','hold') AND julianday(end)-julianday(start)>400")   # 해마다 나뉜 댓글 대기분도 합쳐서 다시
    x("DELETE FROM meta WHERE key LIKE 'pin_none:%' AND key NOT LIKE 'pin_none:%:%'")
    x("DELETE FROM jobs WHERE kind='pin' AND status='done' AND msg LIKE '%자료 없음%'")
    for k in q("SELECT id FROM keywords"):   # 새로 생긴 수집 항목이 있으면 기존 키워드에도 채움
        worker.enqueue_keyword(k["id"])
    worker.start()
    sched = BackgroundScheduler()
    sched.add_job(worker.daily_refresh, "cron", hour=C.DAILY_HOUR, minute=10)
    sched.add_job(refresh_rank, "interval", hours=1)
    threading.Thread(target=refresh_rank, daemon=True).start()
    sched.start()
    yield
    sched.shutdown(wait=False)


app = FastAPI(lifespan=lifespan)

# 공개 주소에 올릴 때: 환경변수 KT_PASSWORD 를 넣으면 비밀번호를 물어봄(아이디는 아무거나)
PASSWORD = os.environ.get("KT_PASSWORD", "")


@app.middleware("http")
async def auth(request, call_next):
    if PASSWORD:
        try:
            given = base64.b64decode(request.headers.get("authorization", "")[6:]).decode().split(":", 1)[1]
        except Exception:
            given = None
        if given is None or not secrets.compare_digest(given, PASSWORD):
            return Response("비밀번호 필요", 401, {"WWW-Authenticate": 'Basic realm="keyword-tracker"'})
    return await call_next(request)


app.mount("/static", StaticFiles(directory=C.ROOT / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(C.ROOT / "static" / "index.html")


# ── 1) 키워드 입력 → 후보 ─────────────────────────────────
class SuggestIn(BaseModel):
    keyword: str


@app.post("/api/suggest")
def suggest(body: SuggestIn):
    sug = variants.suggest(body.keyword)
    if not sug:
        raise HTTPException(400, "키워드를 입력하세요")
    vs = [t for t, _ in sug]
    names = {v.replace(" ", "").replace("-", "") for v in vs}
    subs, seen, err = [], set(), ""
    for prefix in sorted({min(names, key=len)}):
        for attempt in range(3):  # 가끔 422가 나서 몇 번 다시 시도
            try:
                for s in arctic.find_subreddits(prefix):
                    if s["name"].lower() not in seen:
                        seen.add(s["name"].lower())
                        s["exact"] = s["name"].lower() in names
                        subs.append(s)
                err = ""
                break
            except Exception as e:
                err = f"서브레딧 검색 실패: {e}"
                time.sleep(4)
    subs.sort(key=lambda s: (-s["exact"], -s["subscribers"]))
    return {"variants": vs, "checked": [t for t, c in sug if c], "subs": subs, "error": err}


class KeywordIn(BaseModel):
    name: str
    variants: list[str]
    own_subs: list[str] = []
    mention_subs: list[str] = []


def _clean_sub(s):
    return s.strip().removeprefix("/").removeprefix("r/").strip()


@app.post("/api/keywords")
def create_keyword(body: KeywordIn):
    name = body.name.strip().lower()
    terms = [t.strip().lower() for t in body.variants if t.strip()]
    if not name or not terms:
        raise HTTPException(400, "키워드와 변형을 하나 이상 고르세요")
    x("INSERT OR IGNORE INTO keywords(name,created_at) VALUES(?,?)", (name, worker.now_iso()))
    kid = q("SELECT id FROM keywords WHERE name=?", (name,))[0]["id"]
    x("DELETE FROM variants WHERE keyword_id=?", (kid,))
    for t in terms:
        x("INSERT OR IGNORE INTO variants VALUES(?,?)", (kid, t))
    # 뺀 변형의 남은 검색 작업은 취소
    x(f"DELETE FROM jobs WHERE keyword_id=? AND kind='mention' AND status!='done' "
      f"AND term NOT IN ({','.join('?' * len(terms) * 2)})", (kid, *terms, *("c:" + t for t in terms)))
    for kind, subs in (("own", body.own_subs), ("mention", body.mention_subs)):
        for s in filter(None, map(_clean_sub, subs)):
            x("INSERT OR REPLACE INTO subreddits VALUES(?,?,?)", (kid, s, kind))
    worker.enqueue_keyword(kid)
    if body.own_subs and (C.AUTO_DISCOVER or not q("SELECT 1 FROM subreddits WHERE keyword_id=? AND kind='mention'", (kid,))):
        worker._add(kid, "discover", 0)   # 새 키워드라 언급 커뮤니티가 없을 때만 자동 선별
    return {"id": kid}


class SubsIn(BaseModel):
    subs: list[str]


@app.post("/api/keywords/{kid}/mention_subs")
def add_mention_subs(kid: int, body: SubsIn):
    for s in filter(None, map(_clean_sub, body.subs)):
        x("INSERT OR IGNORE INTO subreddits VALUES(?,?,'mention')", (kid, s))
        x("DELETE FROM candidates WHERE keyword_id=? AND lower(name)=lower(?)", (kid, s))
    # 언급 커뮤니티가 생겼으니 자동 선별은 뒤로(언급 수 세기가 먼저)
    x("UPDATE jobs SET priority=4 WHERE keyword_id=? AND kind='discover' AND status IN ('queued','hold')", (kid,))
    worker.enqueue_keyword(kid)
    return {"ok": True}


@app.post("/api/keywords/{kid}/own_subs")
def add_own_subs(kid: int, body: SubsIn):
    """전용 커뮤니티(글 수 전체를 셈)로 추가 — 언급으로 들어가 있던 건 전용으로 바꿈."""
    for s in filter(None, map(_clean_sub, body.subs)):
        x("INSERT OR REPLACE INTO subreddits VALUES(?,?,'own')", (kid, s))
        x("DELETE FROM jobs WHERE keyword_id=? AND kind='mention' AND sub=? AND status!='done'", (kid, s))
    worker.enqueue_keyword(kid)
    return {"ok": True}


@app.post("/api/keywords/{kid}/reddit_terms")
def set_reddit_terms(kid: int, body: SubsIn):
    """레딧 언급 검색에만 쓰는 추가 검색어(예: rhode)."""
    terms = [t.strip().lower() for t in body.subs if t.strip()]
    x("INSERT OR REPLACE INTO meta VALUES(?,?,?)", (f"rterms:{kid}", json.dumps(terms), time.time()))
    worker.enqueue_keyword(kid)
    return {"ok": True, "terms": worker.reddit_terms(kid)}


@app.post("/api/keywords/{kid}/probe")
def probe_subs(kid: int, body: SubsIn):
    """후보 서브레딧을 넣으면, 키워드 언급 수를 세어 많은 곳만 자동으로 추적한다."""
    have = {r["name"].lower() for r in q("SELECT name FROM subreddits WHERE keyword_id=?", (kid,))}
    for s in filter(None, map(_clean_sub, body.subs)):
        if s.lower() not in have:
            x("INSERT OR REPLACE INTO candidates(keyword_id,name,score,hits) VALUES(?,?,0,NULL)", (kid, s))
    x("DELETE FROM meta WHERE key=?", (f"disc_{kid}",))
    worker._add(kid, "discover", 0)
    return {"ok": True}


@app.delete("/api/keywords/{kid}/subs/{sub}")
def remove_sub(kid: int, sub: str):
    x("DELETE FROM subreddits WHERE keyword_id=? AND name=?", (kid, sub))
    x("DELETE FROM jobs WHERE keyword_id=? AND sub=? AND status!='done'", (kid, sub))
    return {"ok": True}


@app.delete("/api/keywords/{kid}")
def delete_keyword(kid: int):
    for t in ("variants", "subreddits", "candidates", "jobs", "global_counts", "global_months",
              "yt_videos", "yt_windows", "yt_snapshots", "tt_snapshots", "tt_history"):
        x(f"DELETE FROM {t} WHERE keyword_id=?", (kid,))
    x("DELETE FROM trends WHERE batch LIKE ?", (f"{kid}:%",))
    x("DELETE FROM keywords WHERE id=?", (kid,))
    return {"ok": True}


@app.post("/api/keywords/{kid}/full")
def full_history(kid: int):
    """이 키워드만 유튜브·레딧 댓글 언급을 2019년부터 다 받기(우선순위는 뒤로)."""
    x("INSERT OR REPLACE INTO meta VALUES(?,?,?)", (f"full:{kid}", "1", time.time()))
    worker.enqueue_keyword(kid)
    x("UPDATE jobs SET priority=MAX(priority,6) WHERE keyword_id=? AND status='queued' AND "
      "((kind='yt' AND start<?) OR (kind='mention' AND term LIKE 'c:%' AND start<?))",
      (kid, C.YT_WEB_START + "-01", worker.all_months()[-C.COMMENT_MONTHS] + "-01"))
    return {"ok": True}


@app.post("/api/keywords/{kid}/refresh")
def refresh(kid: int):
    worker.enqueue_keyword(kid, refresh=True)
    return {"ok": True}


@app.get("/api/keywords")
def keywords():
    """목록 + 인기 점수. 키워드끼리 바로 비교되는 절대값만 씀(최근 12개월):
    유튜브 영상 조회수 합, 유튜브 영상 수, 레딧 글 수(전용 커뮤니티 전체 + 다른 커뮤니티 언급).
    지표마다 키워드 사이 백분위(0~100)를 매기고 평균 → 인기 점수. 구글·틱톡은 키워드마다 따로 정규화된 값이라 제외."""
    import datetime as _dt
    since_d = worker.today().replace(day=1) - _dt.timedelta(days=365)
    since, since_m = since_d.isoformat(), since_d.isoformat()[:7]
    ks = [dict(r) for r in q("SELECT id,name,category,country FROM keywords")]
    ytv = {r["keyword_id"]: (r["v"] or 0, r["n"]) for r in q(
        "SELECT keyword_id, SUM(views) v, COUNT(*) n FROM yt_videos WHERE published>=? GROUP BY keyword_id", (since,))}
    for k in ks:
        kid = k["id"]
        subs = q("SELECT name,kind FROM subreddits WHERE keyword_id=?", (kid,))
        own = [x["name"] for x in subs if x["kind"] == "own"]
        men = [x["name"] for x in subs if x["kind"] == "mention"]
        rp = 0
        if own:
            rp += q(f"SELECT IFNULL(SUM(posts),0) v FROM sub_monthly WHERE month>=? AND sub IN ({','.join('?'*len(own))})", (since_m, *own))[0]["v"]
        terms = [t for t in worker.reddit_terms(kid)]
        if men and terms:
            rp += q(f"SELECT IFNULL(SUM(count),0) v FROM mention_windows WHERE start>=? AND status!='split' "
                    f"AND sub IN ({','.join('?'*len(men))}) AND term IN ({','.join('?'*len(terms))})", (since, *men, *terms))[0]["v"]
        v, n = ytv.get(kid, (0, 0))
        # 아직 받은 데이터가 없는 지표는 '없음'(0점이 아니라 점수 계산에서 뺌)
        has_r = bool(own and q(f"SELECT 1 FROM sub_monthly WHERE sub IN ({','.join('?'*len(own))}) LIMIT 1", own)) or \
            bool(men and terms and q(f"SELECT 1 FROM mention_windows WHERE status!='split' AND start>=? AND sub IN ({','.join('?'*len(men))}) "
                                     f"AND term IN ({','.join('?'*len(terms))}) LIMIT 1", (since, *men, *terms)))
        k["m"] = {"yt_views": v if n else None, "yt_videos": n if n else None, "reddit_posts": rp if has_r else None}
    for key in ("yt_views", "yt_videos", "reddit_posts"):
        vals = sorted(k["m"][key] for k in ks if k["m"][key] is not None)
        for k in ks:   # 백분위: 데이터가 있는 키워드 중 나보다 작은 비율
            x = k["m"][key]
            k["m"][key + "_pct"] = None if x is None else round(100 * sum(1 for y in vals if y < x) / max(1, len(vals) - 1))
    for k in ks:
        ps = [k["m"][key + "_pct"] for key in ("yt_views", "yt_videos", "reddit_posts") if k["m"][key + "_pct"] is not None]
        k["score"] = round(sum(ps) / len(ps)) if ps else 0
        k["n_metrics"] = len(ps)
    f = C.ROOT / "seed" / "importance.txt"
    order = [l.strip().lower() for l in f.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")] if f.exists() else []
    rank = {n: i for i, n in enumerate(order)}
    for k in ks:
        k["rank"] = rank.get(k["name"], 10 ** 6)
    if not _MOM:
        alerts()
    for k in ks:
        k.update(_MOM.get(k["id"]) or {"mom": None, "near": None, "yoy": None})
    return sorted(ks, key=lambda r: (-(r["mom"] if r["mom"] is not None else -1), -r["score"], r["name"]))


class GroupIn(BaseModel):
    category: str = ""
    country: str = ""


@app.post("/api/keywords/{kid}/group")
def set_group(kid: int, body: GroupIn):
    x("UPDATE keywords SET category=?, country=? WHERE id=?",
      (body.category.strip() or None, body.country.strip() or None, kid))
    return {"ok": True}


# ── 유튜브 API 키 등록 ─────────────────────────────────────
class KeyIn(BaseModel):
    key: str


class TTHistIn(BaseModel):
    keyword: str
    rows: list[list]   # [[week 'YYYY-MM-DD', value], ...]
    unit: str = "activity"   # activity(키워드 활동 지수) | views(회사 단위 해시태그 조회수)
    label: str = ""           # views일 때 회사명·종목코드


@app.post("/api/tiktok/history")
def tiktok_history(body: TTHistIn):
    r = q("SELECT id FROM keywords WHERE name=?", (body.keyword.strip().lower(),))
    if not r:
        raise HTTPException(404, "키워드 없음")
    kid = r[0]["id"]
    x("DELETE FROM tt_history WHERE keyword_id=?", (kid,))
    for wk, v in body.rows:
        x("INSERT OR REPLACE INTO tt_history VALUES(?,?,?)", (kid, wk, float(v)))
    x("INSERT OR REPLACE INTO meta VALUES(?,?,?)", (f"ttsrc:{kid}", json.dumps({"unit": body.unit, "label": body.label}), time.time()))
    return {"ok": True, "n": len(body.rows)}


class TrendsIn(BaseModel):
    keyword: str
    gprop: str = "web"        # web | youtube
    geo: str = "US"           # US | WW
    rows: list[list]          # [[week 'YYYY-MM-DD', value], ...] (주간 0~100)


@app.post("/api/trends/import")
def trends_import(body: TrendsIn):
    """다른 경로(TickerTrends 경유 구글 트렌드 5년 주간)로 받은 값을 월평균으로 넣음. 이미 받은 구글 원자료가 있으면 건드리지 않음."""
    r = q("SELECT id FROM keywords WHERE name=?", (body.keyword.strip().lower(),))
    if not r:
        raise HTTPException(404, "키워드 없음")
    kid = r[0]["id"]
    batch = f"{kid}:{body.gprop}:{body.geo}"
    if q("SELECT 1 FROM trends WHERE batch=? AND adjusted=0 LIMIT 1", (batch,)):
        return {"ok": True, "skipped": "이미 있음"}
    term = (q("SELECT term FROM variants WHERE keyword_id=? ORDER BY rowid LIMIT 1", (kid,)) or [{"term": body.keyword}])[0]["term"]
    mon = {}
    for wk, v in body.rows:
        mon.setdefault(str(wk)[:7], []).append(float(v))
    avg = {m: sum(v) / len(v) for m, v in mon.items()}
    top = max(avg.values() or [0]) or 1
    cur = worker.today().isoformat()[:7]
    x("BEGIN")
    x("DELETE FROM trends WHERE batch=?", (batch,))
    for m, v in sorted(avg.items()):
        x("INSERT OR REPLACE INTO trends VALUES(?,?,?,?,?,?,?,1,?)",
          (batch, term, body.gprop, body.geo, m, round(v / top * 100, 1), int(m == cur), worker.now_iso()))
    x("UPDATE jobs SET status='done', msg='TickerTrends 경유로 채움' WHERE keyword_id=? AND kind='trends' AND gprop=? AND geo=? AND status!='done'",
      (kid, body.gprop, body.geo))
    x("COMMIT")
    return {"ok": True, "months": len(avg)}


class JaIn(BaseModel):
    term: str   # 일본어 검색어(예: くら寿司)
    ko: str     # 한글 뜻·읽기(예: 쿠라 스시)


@app.post("/api/keywords/{kid}/ja")
def set_ja(kid: int, body: JaIn):
    """일본 브랜드: 일본어 검색어와 그 한글 뜻(화면에 괄호로 보여 줌)."""
    x("INSERT OR REPLACE INTO meta VALUES(?,?,?)", (f"ja:{kid}", json.dumps({"term": body.term, "ko": body.ko}, ensure_ascii=False), time.time()))
    return {"ok": True}


@app.post("/api/trends/weekly")
def trends_weekly_import(body: TrendsIn):
    """다른 경로(TickerTrends 경유 구글 트렌드 5년 주간)로 받은 주간 값을 그대로 넣음."""
    r = q("SELECT id FROM keywords WHERE name=?", (body.keyword.strip().lower(),))
    if not r:
        raise HTTPException(404, "키워드 없음")
    kid = r[0]["id"]
    x("BEGIN")
    x("DELETE FROM trends_w WHERE keyword_id=? AND gprop=? AND geo=?", (kid, body.gprop, body.geo))
    for wk, v in body.rows:
        x("INSERT OR REPLACE INTO trends_w VALUES(?,?,?,?,?,?,?)", (kid, body.gprop, body.geo, str(wk)[:10], float(v), 0, worker.now_iso()))
    x("UPDATE jobs SET status='done', msg='TickerTrends 경유로 채움' WHERE keyword_id=? AND kind='trends' AND month='W' AND gprop=? AND geo=? AND status!='done'",
      (kid, body.gprop, body.geo))
    x("COMMIT")
    return {"ok": True, "weeks": len(body.rows)}


@app.post("/api/settings/youtube")
def set_youtube_key(body: KeyIn):
    key = body.key.strip()
    try:
        youtube.check_key(key)
    except youtube.QuotaExceeded:
        pass  # 키는 맞는데 오늘 한도만 다 쓴 경우
    except Exception as e:
        raise HTTPException(400, f"키 확인 실패: {e}")
    x("INSERT OR REPLACE INTO meta VALUES('yt_key',?,?)", (key, time.time()))
    x("UPDATE jobs SET status='queued', next_at=0, msg='' WHERE kind IN ('yt','ytstats') AND status='hold'")
    for k in q("SELECT id FROM keywords"):
        worker.enqueue_keyword(k["id"])
    return {"ok": True}


PROBES = {   # 다른 수집 경로가 서버에서 되는지 한 번씩 확인(고정 주소만)
    "pin_metrics": "https://trends.pinterest.com/metrics/?terms=rhode&country=US&end_date=2026-11-23&days=730&aggregation=2&normalize_against_group=true&predicted_days=0",
    "pin_page": "https://trends.pinterest.com/search/?q=rhode&country=US",
}


@app.get("/api/probe")
def probe():
    import requests as rq
    out = {}
    for k, u in PROBES.items():
        try:
            r = rq.get(u, headers={"User-Agent": "TrendSamurai/1.0 (personal research dashboard)"}, timeout=20)
            out[k] = [r.status_code, r.text[:400], r.headers.get("content-type")]
        except Exception as e:
            out[k] = [0, type(e).__name__]
    return out


@app.get("/api/probe_pin")
def probe_pin(path: str = "metrics/", qs: str = ""):
    """핀터레스트 트렌드 접근 확인용(trends.pinterest.com 고정)."""
    import requests as rq
    u = "https://trends.pinterest.com/" + path.lstrip("/") + ("?" + qs if qs else "")
    try:
        r = rq.get(u, headers={"User-Agent": "Mozilla/5.0 (TrendSamurai personal research)", "Accept": "application/json, text/plain, */*",
                               "Referer": "https://trends.pinterest.com/"}, timeout=25)
        return {"url": u, "code": r.status_code, "ct": r.headers.get("content-type"), "body": r.text[:3000]}
    except Exception as e:
        return {"url": u, "err": type(e).__name__}


# 틱톡 회사 해시태그 값에 다른 브랜드가 크게 섞이는 키워드(회사의 대표 브랜드가 아님)
TT_MIXED = set()   # 모두 그대로 씀(사용자 결정)


@app.get("/api/probe_amz")
def probe_amz(asin: str = "B0DY11WJ3R", host: str = "www.amazon.com"):
    """아마존 상품 페이지의 '지난달 구매' 표시가 서버에서 받아지는지 확인(공개 페이지, 한 번)."""
    import re as _re
    import requests as rq
    if host not in ("www.amazon.com", "www.amazon.co.jp"):
        raise HTTPException(400, "host")
    try:
        r = rq.get(f"https://{host}/dp/{asin}", headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9"}, timeout=25)
        t = r.text
        return {"code": r.status_code, "len": len(t), "captcha": "captcha" in t.lower(),
                "bought": _re.findall(r"([0-9][0-9.,]*[KkMm]?\+?)\s*bought in past month", t)[:3],
                "title": (_re.search(r'id="productTitle"[^>]*>\s*([^<]{0,120})', t) or [None, None])[1]}
    except Exception as e:
        return {"err": type(e).__name__}


@app.get("/api/probe_ttcc")
def probe_ttcc(tag: str = "medicube", period: int = 1095, country: str = "US"):
    """틱톡 크리에이티브 센터 해시태그 추이가 서버에서 받아지는지 확인."""
    from .sources import tiktok as tk
    try:
        return tk.creative_center(tag, period, country)
    except Exception as e:
        return {"err": f"{type(e).__name__}: {e}"[:300]}


@app.get("/api/health")
def health():
    return {"sources": autotune.summary(), "log": list(autotune.LOG), "yt_errs": worker.state.get("yt_errs", {}),
            "yt_pause_left": max(0, int(autotune.PAUSE.get("youtube", 0) - time.time()))}


# ── 2) 진행 상황 ─────────────────────────────────────────
@app.get("/api/status")
def status(kid: int | None = None):
    running = list(worker.state.get("running", {}).values())
    mine = [j for j in running if j["keyword_id"] == kid] if kid else running
    cur = (mine or running or [None])[0]
    where, args = ("WHERE keyword_id=?", (kid,)) if kid else ("", ())
    counts = {r["status"]: r["n"] for r in q(f"SELECT status, COUNT(*) n FROM jobs {where} GROUP BY status", args)}
    holds = [dict(r) for r in q(
        f"SELECT sub, term, COUNT(*) n, MIN(start) first, MAX(end) last, MAX(msg) msg, MIN(next_at) next_at "
        f"FROM jobs {where + ' AND' if where else 'WHERE'} status IN ('hold','failed') GROUP BY kind, sub, term", args)]
    waiting = [dict(r) for r in q(
        f"SELECT kind, COUNT(*) n, MIN(next_at) next_at, MAX(msg) msg FROM jobs "
        f"{where + ' AND' if where else 'WHERE'} status='queued' AND next_at>? GROUP BY kind", (*args, time.time()))]
    return {
        "waiting": waiting,
        "current": " · ".join(worker.describe(j) for j in (mine or running)) or None,
        "current_kid": cur["keyword_id"] if cur else None,
        "note": worker.state["note"],
        "cooldown": max(0, int(worker.state["cooldown_until"] - time.time())),
        "counts": counts, "holds": holds, "now": time.time(),
        "by_kind": [dict(r) for r in q(f"SELECT kind, status, COUNT(*) n FROM jobs {where} GROUP BY kind, status", args)],
    }


# ── 3) 결과 데이터 ────────────────────────────────────────
def build(kid: int):
    k = q("SELECT * FROM keywords WHERE id=?", (kid,))
    if not k:
        raise HTTPException(404)
    terms = [r["term"] for r in q("SELECT term FROM variants WHERE keyword_id=? ORDER BY rowid", (kid,))]
    subs = q("SELECT name,kind FROM subreddits WHERE keyword_id=? ORDER BY rowid", (kid,))
    months = worker.all_months()
    t = worker.today()

    own = {}
    for s in (s["name"] for s in subs if s["kind"] == "own"):
        rows = {r["month"]: r for r in q("SELECT * FROM sub_monthly WHERE sub=?", (s,))}
        first = min(rows) if rows else None
        own[s] = {m: ([rows[m]["posts"], rows[m]["comments"]] if m in rows else [0, 0])
                  for m in months if first and m >= first}  # 생기기 전 달은 '없음'

    def mention_counts(tms):
        """서브레딧 → 달 → 검색어별 건수. 아직 덜 받은 달은 뺌."""
        ph = ",".join("?" * len(tms))
        pending = {(r["sub"], r["month"]) for r in q(
            f"SELECT DISTINCT sub, month FROM jobs WHERE kind='mention' AND term IN ({ph}) "
            "AND status IN ('queued','running','hold')", tms)}
        out = {}
        for s in (s["name"] for s in subs if s["kind"] == "mention"):
            roots, cnt = set(), {}
            for r in q(f"SELECT term,start,end,count,status FROM mention_windows WHERE sub=? AND term IN ({ph})",
                       (s, *tms)):
                m = r["start"][:7]
                if (r["start"], r["end"]) == worker.month_bounds(m):
                    roots.add((r["term"], m))
                if r["status"] != "split":
                    cnt[(r["term"], m)] = cnt.get((r["term"], m), 0) + (r["count"] or 0)
            out[s] = {m: {tm.removeprefix("c:"): cnt.get((tm, m), 0) for tm in tms} for m in months
                      if (s, m) not in pending and all((tm, m) in roots for tm in tms)}
        return out

    rterms = worker.reddit_terms(kid)
    mentions = mention_counts(rterms) if rterms else {}
    mentions_c = mention_counts(["c:" + t for t in rterms]) if rterms else {}

    tr = {}
    for r in q("SELECT * FROM trends WHERE batch LIKE ? ORDER BY month", (f"{kid}:%",)):
        key = (r["gprop"], r["geo"], r["term"])
        tr.setdefault(key, {})[r["month"]] = [r["value"], r["partial"]]
    trends = [{"gprop": g, "geo": geo, "term": term, "points": pts}
              for (g, geo, term), pts in tr.items() if term in terms]

    # 레딧 전체 언급
    own_names = {s["name"].lower() for s in subs if s["kind"] == "own"}
    glob = {"enabled": C.PULLPUSH_ENABLED, "submission": {}, "comment": {}, "coverage": {}, "top_subs": []}
    for r in q("SELECT kind, month, status, total FROM global_months WHERE keyword_id=?", (kid,)):
        glob[r["kind"]][r["month"]] = {"total": r["total"], "own": 0, "partial": int(r["status"] != "done")}
    top = {}
    recent = set(months[-14:])
    for r in q("SELECT kind, month, sub, count FROM global_counts WHERE keyword_id=?", (kid,)):
        cell = glob[r["kind"]].get(r["month"])
        if cell and r["sub"].lower() in own_names:
            cell["own"] += r["count"]
        if r["month"] in recent:
            row = top.setdefault(r["sub"], {"sub": r["sub"], "submission": 0, "comment": 0})
            row[r["kind"]] += r["count"]
    glob["top_subs"] = sorted(top.values(), key=lambda v: -(v["submission"] + v["comment"]))[:20]
    for kind in ("submission", "comment"):
        c = q("SELECT value FROM meta WHERE key=?", (f"pp_cov_{kind}",))
        if c:
            glob["coverage"][kind] = datetime.fromtimestamp(int(c[0]["value"]), timezone.utc).strftime("%Y-%m-%d")

    # 유튜브
    n_c = q("SELECT COUNT(*) n, SUM(comments_at IS NOT NULL) c FROM yt_videos WHERE keyword_id=?", (kid,))[0]
    yt = {"enabled": True, "official": bool(worker.yt_key()), "with_comments": n_c["c"] or 0, "months": {}, "top": [], "snapshots": [], "total": 0, "capped": 0}
    covered = set()
    for r in q("SELECT start, end, status, capped FROM yt_windows WHERE keyword_id=?", (kid,)):
        if r["status"] != "split":
            covered |= {m for m in months if r["start"][:7] <= m < r["end"][:7] or m == r["start"][:7]}
            yt["capped"] += r["capped"]
    agg = {r["m"]: r for r in q(
        "SELECT substr(published,1,7) m, COUNT(*) n, SUM(views) v, SUM(comments) c, SUM(likes) l, "
        "SUM(comments_at IS NOT NULL) cn "
        "FROM yt_videos WHERE keyword_id=? GROUP BY 1", (kid,))}
    for m in sorted(covered):
        r = agg.get(m)
        # 댓글 수는 그 달 영상의 95% 이상을 받았을 때만 보여 줌(덜 받은 달은 '없음')
        yt["months"][m] = [r["n"], r["v"], r["c"] if (r["cn"] or 0) >= 0.95 * r["n"] else None] if r else [0, 0, 0]
    yt["top"] = [dict(r) for r in q(
        "SELECT video_id, title, channel, published, views, comments, likes FROM yt_videos "
        "WHERE keyword_id=? ORDER BY views DESC LIMIT 20", (kid,))]
    yt["channels"] = [dict(r) for r in q(
        "SELECT channel, COUNT(*) n, SUM(views) views FROM yt_videos WHERE keyword_id=? "
        "GROUP BY channel ORDER BY views DESC LIMIT 10", (kid,))]
    yt["snapshots"] = [dict(r) for r in q(
        "SELECT day, videos, views, comments FROM yt_snapshots WHERE keyword_id=? ORDER BY day DESC LIMIT 60", (kid,))]
    yt["total"] = q("SELECT COUNT(*) n FROM yt_videos WHERE keyword_id=?", (kid,))[0]["n"]

    tt_hist = [{"week": r["week"], "value": r["value"]} for r in q(
        "SELECT week, value FROM tt_history WHERE keyword_id=? ORDER BY week", (kid,))]
    tt = {}
    for r in q("SELECT tag, day, videos, views FROM tt_snapshots WHERE keyword_id=? ORDER BY day DESC", (kid,)):
        tt.setdefault(r["tag"], []).append({"day": r["day"], "videos": r["videos"], "views": r["views"]})

    return {
        "tiktok": [{"tag": t, "rows": rows[:90]} for t, rows in tt.items()],
        "tiktok_history": tt_hist,
        "full": bool(q("SELECT 1 FROM meta WHERE key=?", (f"full:{kid}",))),
        "tiktok_mixed": k[0]["name"] in TT_MIXED,
        "tiktok_src": json.loads((q("SELECT value FROM meta WHERE key=?", (f"ttsrc:{kid}",)) or [{"value": "{}"}])[0]["value"]),
        "mentions_c": mentions_c,
        "youtube": yt,
        "global": glob,
        "id": kid, "keyword": k[0]["name"], "category": k[0]["category"], "country": k[0]["country"], "variants": terms, "months": months,
        "cur_month": months[-1], "days_elapsed": t.day, "today": t.isoformat(),
        "own": own, "mentions": mentions, "trends": trends,
        "ja": json.loads((q("SELECT value FROM meta WHERE key=?", (f"ja:{kid}",)) or [{"value": "null"}])[0]["value"]),
        "trends_w": [{"gprop": g, "geo": geo, "points": {r["week"]: [r["value"], r["partial"]] for r in q(
            "SELECT week,value,partial FROM trends_w WHERE keyword_id=? AND gprop=? AND geo=? ORDER BY week", (kid, g, geo))}}
            for g, geo in (("web", "WW"), ("youtube", "WW"), ("web", "US"), ("youtube", "US"), ("web", "JA"), ("web", "JP"), ("youtube", "JP"), ("froogle", "WW"), *(("pinterest", c) for c in worker.PIN_GEOS))],
        "subs": [dict(s) for s in subs],
        "candidates": [dict(r) for r in q(
            "SELECT name,score,hits FROM candidates WHERE keyword_id=? ORDER BY IFNULL(hits,0) DESC, score DESC", (kid,))],
    }


_ALERT = {"t": 0.0, "v": None}


def _highs(name, pts, cur, min_n=12):
    """pts: {기간: 값}. 마지막 완료 기간 값이 이전 최고를 넘으면 신고가, 최고의 95% 이상이면 근접."""
    ks = sorted(k for k, v in pts.items() if v is not None and k != cur)
    if len(ks) < min_n:
        return None
    last, v = ks[-1], pts[ks[-1]]
    prev = max(pts[k] for k in ks[:-1])
    top = max(prev, v)
    if top <= 0 or v <= 0:
        return None
    if sum(1 for k in ks if pts[k] == top) >= 2:   # 최고값이 여러 달 같음 = 수집 한도에 걸린 값(진짜 신고가 아님)
        return None
    if v > prev:
        return {"metric": name, "kind": "high", "at": last, "value": v, "pct": 100}
    if v >= 0.95 * top:
        return {"metric": name, "kind": "near", "at": last, "value": v, "pct": round(v / top * 100)}
    return None


def keyword_alerts(d):
    cur = d["cur_month"]
    out = []
    tw = {(t["gprop"], t["geo"]): t["points"] for t in d.get("trends_w", []) if t["points"]}
    for gp, label in (("web", "구글 검색"), ("youtube", "유튜브 검색")):
        w = tw.get((gp, "WW"))
        if w:   # 주간: 마지막 완료 주가 최근 2년(104주) 최고를 넘었나. 일본 브랜드는 일본(일본어 검색)도 보고 더 강한 쪽
            cands = []
            for pts in (w, tw.get((gp, "JP")) or (tw.get(("web", "JA")) if gp == "web" else None)):
                if pts:
                    done = {k: v[0] for k, v in sorted(pts.items()) if not v[1]}
                    cands.append(_highs(label, {k: done[k] for k in sorted(done)[-104:]}, None, 26))
            cands = [c for c in cands if c]
            if cands:
                out.append(max(cands, key=lambda c: (c["kind"] == "high", c["pct"])))
            continue
        s = {}
        for t in d["trends"]:
            if t["gprop"] == gp and t["geo"] == "WW":
                for m, (v, _p) in t["points"].items():
                    s[m] = s.get(m, 0) + v
        out.append(_highs(label, s, cur))
    pw = next((t["points"] for t in d.get("trends_w", []) if t["gprop"] == "pinterest" and t["geo"] == "US" and t["points"]), None)
    if pw:   # 핀터레스트 주간(미국, 최근 2년): 마지막 주가 지난 최고를 넘었나
        out.append(_highs("핀터레스트 검색", {k: v[0] for k, v in sorted(pw.items())}, None, 26))
    # 유튜브 영상 수는 아직 달마다 받은 깊이(1차 3페이지/2차 8페이지)가 달라 최근 달이 높게 나옴 → 2차가 끝날 때까지 알림에서 제외
    rp = {}
    for ms in d["own"].values():
        for m, v in ms.items():
            rp[m] = rp.get(m, 0) + v[0]
    for ms in d["mentions"].values():
        for m, by in ms.items():
            rp[m] = rp.get(m, 0) + sum(by.values())
    out.append(_highs("레딧 글 수", rp, cur))
    # 틱톡: 회사 해시태그 조회수(TickerTrends) — 키워드가 그 회사의 대표 브랜드일 때만(여러 브랜드가 섞이면 제외)
    tt = {r["week"]: r["value"] for r in d["tiktok_history"]}
    if (d.get("tiktok_src") or {}).get("unit") == "views" and d["keyword"] not in TT_MIXED:
        out.append(_highs("틱톡 조회수", tt, None, 26))
    return [a for a in out if a]


def _wk_mom(pts):
    """주간 값 → (최고 대비 %, 전년 대비 %). 4주 평균으로 매끄럽게 한 뒤 최근 4주를 2년 최고·1년 전과 비교."""
    vals = [v for _, v in sorted(pts.items())]
    if len(vals) < 60:
        return None
    r4 = [sum(vals[i - 3:i + 1]) / 4 for i in range(3, len(vals))]
    last, mx = r4[-1], max(r4[-104:])
    if mx <= 0:
        return None
    prev = r4[-53] if len(r4) >= 53 else 0
    return round(last / mx * 100), (round((last / prev - 1) * 100) if prev > 0 else None)


def momentum(d):
    """기세 점수(0~100): 구글·유튜브 검색, 핀터레스트(미국), 틱톡, 일본 브랜드는 일본어 검색까지. 지표마다 0.6×(최근이 2년 최고의 몇 %) + 0.4×(전년 대비: +100%↑=100, 0%=50, −100%=0) → 평균."""
    series = []
    for t in d.get("trends_w", []):
        if t["points"] and (t["gprop"], t["geo"]) in (("web", "WW"), ("youtube", "WW"), ("pinterest", "US"), ("web", "JP"), ("youtube", "JP")):
            series.append({k: v[0] for k, v in t["points"].items() if not v[1]})
    if not any(t["gprop"] == "web" and t["geo"] == "JP" and t["points"] for t in d.get("trends_w", [])):
        ja = next((t for t in d.get("trends_w", []) if t["gprop"] == "web" and t["geo"] == "JA" and t["points"]), None)
        if ja:
            series.append({k: v[0] for k, v in ja["points"].items() if not v[1]})
    if (d.get("tiktok_src") or {}).get("unit") == "views" and d.get("tiktok_history"):
        series.append({r["week"]: r["value"] for r in d["tiktok_history"]})
    sc, nears, yoys = [], [], []
    for pts in series:
        m = _wk_mom(pts)
        if not m:
            continue
        near, yoy = m
        ys = 50 if yoy is None else max(0, min(100, 50 + yoy / 2))
        sc.append(0.6 * min(100, near) + 0.4 * ys)
        nears.append(near)
        if yoy is not None:
            yoys.append(yoy)
    if not sc:
        return {"mom": None, "near": None, "yoy": None, "n": 0}
    return {"mom": round(sum(sc) / len(sc)), "near": round(sum(nears) / len(nears)),
            "yoy": round(sum(yoys) / len(yoys)) if yoys else None, "n": len(sc)}


_MOM = {}


@app.get("/api/alerts")
def alerts():
    """지표별 신고가(최근 완료 기간이 역대 최고 초과) 또는 근접(최고의 95% 이상) 키워드. 10분 캐시."""
    if _ALERT["v"] is not None and time.time() - _ALERT["t"] < 600:
        return _ALERT["v"]
    res = []
    for k in q("SELECT id FROM keywords"):
        try:
            d = build(k["id"])
        except Exception:
            continue
        _MOM[d["id"]] = momentum(d)
        a = keyword_alerts(d)
        if a:
            res.append({"id": d["id"], "keyword": d["keyword"], "category": d["category"], "alerts": a,
                        "highs": sum(x["kind"] == "high" for x in a)})
    res.sort(key=lambda r: (-r["highs"], -len(r["alerts"]), r["keyword"]))
    _ALERT.update(t=time.time(), v=res)
    return res


def refresh_rank():
    """수집 순서: 점수 50 초과 키워드 먼저(그 안에서 신고가 → 근접 → 점수 순), 그다음 나머지."""
    try:
        ks = keywords()
        al = {r["id"]: r for r in alerts()}
        # 점수 50 초과 키워드 먼저(그 안에서 신고가 → 근접 → 점수 순), 그다음 나머지
        ks.sort(key=lambda k: (k["score"] <= 50, -(al.get(k["id"], {}).get("highs", 0) > 0), -(k["id"] in al), -k["score"]))
        x("BEGIN")
        x("DELETE FROM krank")
        for i, k in enumerate(ks):
            x("INSERT INTO krank VALUES(?,?)", (k["id"], i))
        x("COMMIT")
    except Exception:
        pass


@app.get("/api/keywords/{kid}/data")
def data(kid: int):
    return build(kid)


# ── 5) 내보내기: CSV (속성은 별도 열) ───────────────────────
@app.get("/api/keywords/{kid}/export.csv")
def export_csv(kid: int):
    d = build(kid)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["keyword", "metric", "source", "subreddit", "variant", "region", "month",
                "value", "days", "daily_avg", "in_progress", "note"])

    def days(m):
        return d["days_elapsed"] if m == d["cur_month"] else calendar.monthrange(int(m[:4]), int(m[5:]))[1]

    for sub, ms in d["own"].items():
        for m, (p, c) in ms.items():
            for metric, v in (("comments", c), ("posts", p)):
                w.writerow([d["keyword"], metric, "arctic-shift", sub, "", "", m, v, days(m),
                            round(v / days(m), 2), int(m == d["cur_month"]), "하루 평균은 자체 계산"])
    for sub, ms in d["mentions"].items():
        for m, by in ms.items():
            for term, v in by.items():
                w.writerow([d["keyword"], "mention_posts", "arctic-shift", sub, term, "", m, v, days(m),
                            round(v / days(m), 3), int(m == d["cur_month"]), "제목에 단어 포함된 글"])
    for kind, metric in (("comment", "reddit_wide_mention_comments"), ("submission", "reddit_wide_mention_posts")):
        for m, c in sorted(d["global"][kind].items()):
            w.writerow([d["keyword"], metric, "PullPush", "(전체)", "", "", m, c["total"], "", "", c["partial"],
                        "레딧 전체, 변형 합산(중복 제거)"])
            w.writerow([d["keyword"], metric, "PullPush", "(전용 서브레딧 제외)", "", "", m, c["total"] - c["own"],
                        "", "", c["partial"], "레딧 전체에서 전용 서브레딧을 뺀 값"])
    for m, (n, v, c) in sorted(d["youtube"]["months"].items()):
        for metric, val, note in (("youtube_videos_uploaded", n, "그 달에 올라온 영상 수"),
                                  ("youtube_views_of_uploads", v, "그 달에 올라온 영상의 현재 누적 조회수")):
            w.writerow([d["keyword"], metric, "YouTube", "", "", "", m, "" if val is None else val, "", "",
                        int(m == d["cur_month"]), note])
    for t in d["tiktok"]:
        for r in t["rows"]:
            w.writerow([d["keyword"], "tiktok_hashtag_posts_total", "TikTok", "", "#" + t["tag"], "", r["day"],
                        r["videos"], "", "", "", "그날까지의 누적 게시물 수"])
            w.writerow([d["keyword"], "tiktok_hashtag_views_total", "TikTok", "", "#" + t["tag"], "", r["day"],
                        r["views"], "", "", "", "그날까지의 누적 조회수"])
    for s in d["trends"]:
        for m, (v, partial) in s["points"].items():
            w.writerow([d["keyword"], f"search_interest_{s['gprop']}", "Google Trends", "", s["term"],
                        s["geo"], m, v, "", "", partial, "0~100 지수, 같은 요청 안에서만 비교 가능"])
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="keyword-{kid}.csv"'})
