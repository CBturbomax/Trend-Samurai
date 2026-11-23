import base64
import calendar
import csv
import io
import os
import secrets
import time
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from . import config as C
from . import db, variants, worker
from .db import q, x
from .sources import arctic


@asynccontextmanager
async def lifespan(app):
    db.init()
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


@app.get("/")
def index():
    return FileResponse(C.ROOT / "static" / "index.html")


# ── 1) 키워드 입력 → 후보 ─────────────────────────────────
class SuggestIn(BaseModel):
    keyword: str


@app.post("/api/suggest")
def suggest(body: SuggestIn):
    vs = variants.suggest(body.keyword)
    if not vs:
        raise HTTPException(400, "키워드를 입력하세요")
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
    return {"variants": vs, "subs": subs, "error": err}


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
    for kind, subs in (("own", body.own_subs), ("mention", body.mention_subs)):
        for s in filter(None, map(_clean_sub, subs)):
            x("INSERT OR REPLACE INTO subreddits VALUES(?,?,?)", (kid, s, kind))
    worker.enqueue_keyword(kid)
    if body.own_subs:
        worker._add(kid, "discover", 1)
    return {"id": kid}


class SubsIn(BaseModel):
    subs: list[str]


@app.post("/api/keywords/{kid}/mention_subs")
def add_mention_subs(kid: int, body: SubsIn):
    for s in filter(None, map(_clean_sub, body.subs)):
        x("INSERT OR IGNORE INTO subreddits VALUES(?,?,'mention')", (kid, s))
        x("DELETE FROM candidates WHERE keyword_id=? AND lower(name)=lower(?)", (kid, s))
    worker.enqueue_keyword(kid)
    return {"ok": True}


@app.delete("/api/keywords/{kid}/subs/{sub}")
def remove_sub(kid: int, sub: str):
    x("DELETE FROM subreddits WHERE keyword_id=? AND name=?", (kid, sub))
    x("DELETE FROM jobs WHERE keyword_id=? AND sub=? AND status!='done'", (kid, sub))
    return {"ok": True}


@app.delete("/api/keywords/{kid}")
def delete_keyword(kid: int):
    for t in ("variants", "subreddits", "candidates", "jobs"):
        x(f"DELETE FROM {t} WHERE keyword_id=?", (kid,))
    x("DELETE FROM trends WHERE batch LIKE ?", (f"{kid}:%",))
    x("DELETE FROM keywords WHERE id=?", (kid,))
    return {"ok": True}


@app.post("/api/keywords/{kid}/refresh")
def refresh(kid: int):
    worker.enqueue_keyword(kid, refresh=True)
    return {"ok": True}


@app.get("/api/keywords")
def keywords():
    return [dict(r) for r in q("SELECT id,name FROM keywords ORDER BY id")]


# ── 2) 진행 상황 ─────────────────────────────────────────
@app.get("/api/status")
def status(kid: int | None = None):
    cur = worker.state["current"]
    where, args = ("WHERE keyword_id=?", (kid,)) if kid else ("", ())
    counts = {r["status"]: r["n"] for r in q(f"SELECT status, COUNT(*) n FROM jobs {where} GROUP BY status", args)}
    holds = [dict(r) for r in q(
        f"SELECT sub, term, COUNT(*) n, MIN(start) first, MAX(end) last, MAX(msg) msg, MIN(next_at) next_at "
        f"FROM jobs {where + ' AND' if where else 'WHERE'} status IN ('hold','failed') GROUP BY kind, sub, term", args)]
    return {
        "current": worker.describe(cur) if cur else None,
        "current_kid": cur["keyword_id"] if cur else None,
        "note": worker.state["note"],
        "cooldown": max(0, int(worker.state["cooldown_until"] - time.time())),
        "counts": counts, "holds": holds, "now": time.time(),
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

    pending = {(r["sub"], r["month"]) for r in q(
        "SELECT DISTINCT sub, month FROM jobs WHERE kind='mention' AND status IN ('queued','running','hold')")}
    mentions = {}
    for s in (s["name"] for s in subs if s["kind"] == "mention"):
        roots, cnt = set(), {}
        for r in q("SELECT term,start,end,count,status FROM mention_windows WHERE sub=?", (s,)):
            if r["term"] not in terms:
                continue
            m = r["start"][:7]
            if (r["start"], r["end"]) == worker.month_bounds(m):
                roots.add((r["term"], m))
            if r["status"] != "split":
                cnt[(r["term"], m)] = cnt.get((r["term"], m), 0) + (r["count"] or 0)
        mentions[s] = {m: {tm: cnt.get((tm, m), 0) for tm in terms} for m in months
                       if (s, m) not in pending and all((tm, m) in roots for tm in terms)}

    tr = {}
    for r in q("SELECT * FROM trends WHERE batch LIKE ? ORDER BY month", (f"{kid}:%",)):
        key = (r["gprop"], r["geo"], r["term"])
        tr.setdefault(key, {})[r["month"]] = [r["value"], r["partial"]]
    trends = [{"gprop": g, "geo": geo, "term": term, "points": pts}
              for (g, geo, term), pts in tr.items() if term in terms]

    return {
        "id": kid, "keyword": k[0]["name"], "variants": terms, "months": months,
        "cur_month": months[-1], "days_elapsed": t.day, "today": t.isoformat(),
        "own": own, "mentions": mentions, "trends": trends,
        "subs": [dict(s) for s in subs],
        "candidates": [dict(r) for r in q(
            "SELECT name,score FROM candidates WHERE keyword_id=? ORDER BY score DESC", (kid,))],
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
    for s in d["trends"]:
        for m, (v, partial) in s["points"].items():
            w.writerow([d["keyword"], f"search_interest_{s['gprop']}", "Google Trends", "", s["term"],
                        s["geo"], m, v, "", "", partial, "0~100 지수, 같은 요청 안에서만 비교 가능"])
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="keyword-{kid}.csv"'})
