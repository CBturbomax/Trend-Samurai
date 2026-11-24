import base64
import calendar
from datetime import datetime, timezone
import csv
import io
import json
import os
import secrets
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
    x(f"DELETE FROM jobs WHERE kind='mention' AND term LIKE 'c:%' AND status!='done' AND start<? "
      f"AND keyword_id NOT IN {full}", (worker.all_months()[-C.COMMENT_MONTHS] + "-01",))
    # 언급 커뮤니티가 아직 하나도 없는 키워드는 선별을 맨 앞에서(이게 끝나야 언급 차트가 생김), 재선별은 뒤로
    x("UPDATE jobs SET priority=CASE WHEN keyword_id IN (SELECT keyword_id FROM subreddits WHERE kind='mention') "
      "THEN 4 ELSE 1 END WHERE kind='discover' AND status IN ('queued','hold')")
    for (g, r), pr in worker.TREND_PRIO.items():
        x("UPDATE jobs SET priority=? WHERE kind='trends' AND gprop=? AND geo=? AND status IN ('queued','hold')", (pr, g, r))
    x("UPDATE jobs SET priority=5 WHERE kind='mention' AND term LIKE 'c:%' AND status IN ('queued','hold')")
    x("DELETE FROM jobs WHERE kind='ytc' AND status!='done'")   # 유튜브 댓글 수는 수집하지 않음
    x("DELETE FROM jobs WHERE kind='tiktok' AND status!='done'")   # 틱톡 해시태그 일일 기록은 그만둠(의미 없음)
    # 레딧 댓글: 달마다 따로 묻던 대기 작업을 지우고, 빈 달 구간을 한 번에 묻는 작업으로 다시 넣음(요청 수 대폭 감소)
    x("DELETE FROM jobs WHERE kind='mention' AND term LIKE 'c:%' AND status IN ('queued','hold') "
      "AND julianday(end)-julianday(start)<=31")
    for k in q("SELECT id FROM keywords"):   # 새로 생긴 수집 항목이 있으면 기존 키워드에도 채움
        worker.enqueue_keyword(k["id"])
    worker.start()
    sched = BackgroundScheduler()
    sched.add_job(worker.daily_refresh, "cron", hour=C.DAILY_HOUR, minute=10)
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
    if body.own_subs:
        worker._add(kid, "discover", 0)
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
    return [dict(r) for r in q("SELECT id,name,category,country FROM keywords ORDER BY name")]


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
    "reddit_json": "https://www.reddit.com/r/AsianBeauty/search.json?q=medicube&restrict_sr=1&sort=new&limit=5",
    "reddit_old": "https://old.reddit.com/r/AsianBeauty/search.json?q=medicube&restrict_sr=1&sort=new&limit=5",
    "reddit_site": "https://www.reddit.com/search.json?q=medicube&sort=new&limit=5",
    "yt_rss": "https://www.youtube.com/feeds/videos.xml?channel_id=UC_x5XG1OV2P6uZZ5FSM9Ttw",
}


@app.get("/api/probe")
def probe():
    import requests as rq
    out = {}
    for k, u in PROBES.items():
        try:
            r = rq.get(u, headers={"User-Agent": "TrendSamurai/1.0 (personal research dashboard)"}, timeout=20)
            out[k] = [r.status_code, r.text[:160]]
        except Exception as e:
            out[k] = [0, type(e).__name__]
    return out


@app.get("/api/health")
def health():
    return {"sources": autotune.summary(), "log": list(autotune.LOG)}


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

    mentions = mention_counts(terms) if terms else {}
    mentions_c = mention_counts(["c:" + t for t in terms]) if terms else {}

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
        "tiktok_src": json.loads((q("SELECT value FROM meta WHERE key=?", (f"ttsrc:{kid}",)) or [{"value": "{}"}])[0]["value"]),
        "mentions_c": mentions_c,
        "youtube": yt,
        "global": glob,
        "id": kid, "keyword": k[0]["name"], "category": k[0]["category"], "country": k[0]["country"], "variants": terms, "months": months,
        "cur_month": months[-1], "days_elapsed": t.day, "today": t.isoformat(),
        "own": own, "mentions": mentions, "trends": trends,
        "subs": [dict(s) for s in subs],
        "candidates": [dict(r) for r in q(
            "SELECT name,score,hits FROM candidates WHERE keyword_id=? ORDER BY IFNULL(hits,0) DESC, score DESC", (kid,))],
    }


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
