"""작업 큐. 워커 1개가 순서대로 처리: ① 월별 총계 → ② 구글 트렌드 → ③ 언급 검색(최신 달부터)."""
import datetime as dt
import threading
import time

from . import config as C
from .db import q, x
from .sources import arctic, pullpush, tiktok, trends, youtube, youtube_web

state = {"current": None, "cooldown_until": 0.0, "streak": 0, "next_ok": 0.0,
         "sub_streak": {}, "note": "", "pp_next": 0.0, "pp_streak": 0}
ACTIVE = "('queued','running','hold')"


# ── 날짜 도우미 ─────────────────────────────────────────
def today():
    return dt.datetime.now(dt.timezone.utc).date()


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def all_months():
    out, d, t = [], dt.date.fromisoformat(C.START_DATE), today()
    while (d.year, d.month) <= (t.year, t.month):
        out.append(f"{d.year}-{d.month:02d}")
        d = dt.date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return out


def month_bounds(m):
    y, mo = int(m[:4]), int(m[5:7])
    return f"{y}-{mo:02d}-01", dt.date(y + (mo == 12), mo % 12 + 1, 1).isoformat()


# ── 대기열에 넣기 ────────────────────────────────────────
def _add(kid, kind, priority, **kw):
    cols = ["sub", "term", "month", "start", "end", "gprop", "geo"]
    where = " AND ".join(f"IFNULL({c},'')=?" for c in cols)
    vals = [kw.get(c) or "" for c in cols]
    own = "" if kind == "mention" else f" AND keyword_id={int(kid)}"
    if q(f"SELECT 1 FROM jobs WHERE kind=? AND status IN {ACTIVE} AND {where}{own} LIMIT 1", [kind] + vals):
        return
    x(f"INSERT INTO jobs(keyword_id,kind,priority,{','.join(cols)}) VALUES(?,?,?,?,?,?,?,?,?,?)",
      [kid, kind, priority] + [kw.get(c) for c in cols])


def enqueue_keyword(kid, refresh=False):
    terms = [r["term"] for r in q("SELECT term FROM variants WHERE keyword_id=?", (kid,))]
    subs = q("SELECT name,kind FROM subreddits WHERE keyword_id=?", (kid,))
    x("BEGIN")
    # 최근 20시간 안에 받은 것은 다시 받지 않음(재시작할 때마다 구글 트렌드를 두드리면 429가 남)
    fresh = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=20)).isoformat(timespec="seconds")
    for s in subs:
        if s["kind"] == "own" and not q(
                "SELECT 1 FROM sub_monthly WHERE sub=? AND updated_at>? LIMIT 1", (s["name"], fresh)):
            _add(kid, "totals", 0, sub=s["name"])
    for gprop, geo in C.TRENDS_COMBOS:
        g, r = gprop or "web", geo or "WW"
        if not q("SELECT 1 FROM trends WHERE batch=? AND updated_at>? LIMIT 1", (f"{kid}:{g}:{r}", fresh)):
            _add(kid, "trends", 1, gprop=g, geo=r)
    done = {(r["kind"], r["month"]) for r in q(
        "SELECT kind, month FROM global_months WHERE keyword_id=? AND status='done'", (kid,))}
    for m in reversed(all_months() if C.PULLPUSH_ENABLED else []):
        for kind in ("comment", "submission"):
            if (kind, m) not in done:
                _add(kid, "global", 2, term=kind, month=m)
    # 틱톡: 오늘 기록이 없으면 받음
    if not q("SELECT 1 FROM tt_snapshots WHERE keyword_id=? AND day=? LIMIT 1", (kid, today().isoformat())):
        _add(kid, "tiktok", 1)
    if True:
        t0 = today()
        if yt_key():   # 공식 API: 지난 해는 1년 단위, 올해는 월 단위
            wins = [(f"{y}-01-01", f"{y + 1}-01-01") for y in range(int(C.START_DATE[:4]), t0.year)]
            wins += [month_bounds(f"{t0.year}-{mo:02d}") for mo in range(1, t0.month + 1)]
        else:          # 키 없는 방식: 전부 월 단위, 최신 달부터
            wins = [month_bounds(m) for m in reversed(all_months())]
        if refresh:  # 덜 끝난 기간(이번 달 등)은 다시 받음
            x("DELETE FROM yt_windows WHERE keyword_id=? AND status='open'", (kid,))
            if not yt_key():  # 조회수는 계속 늘므로 최근 3개월 + 가장 오래전에 받은 6개 달을 매일 다시 받음
                recent = all_months()[-3:]
                old = [r["start"][:7] for r in q(
                    "SELECT start FROM yt_windows WHERE keyword_id=? ORDER BY updated_at LIMIT 6", (kid,))]
                for m in set(recent + old):
                    x("DELETE FROM yt_windows WHERE keyword_id=? AND substr(start,1,7)=?", (kid, m))
        have = {(r["start"], r["end"]) for r in q("SELECT start, end FROM yt_windows WHERE keyword_id=?", (kid,))}
        for a, b in wins:
            if (a, b) not in have:
                _add(kid, "yt", 2, month=a[:7], start=a, end=b)
        _add(kid, "ytstats", 2, month="0000-00")
        if not yt_key():
            _add(kid, "ytc", 4, month="0000-00")
    cur = all_months()[-1]
    for s in subs:
        if s["kind"] != "mention":
            continue
        for t in terms:
            if refresh:  # 진행 중이던 달은 지우고 다시 셈
                redo = {r["m"] for r in q(
                    "SELECT DISTINCT substr(start,1,7) m FROM mention_windows "
                    "WHERE sub=? AND term=? AND status='open'", (s["name"], t))} | {cur}
                for m in redo:
                    x("DELETE FROM mention_windows WHERE sub=? AND term=? AND substr(start,1,7)=?",
                      (s["name"], t, m))
            have = {r["start"] for r in q(
                "SELECT start FROM mention_windows WHERE sub=? AND term=?", (s["name"], t))}
            for m in reversed(all_months()):
                a, b = month_bounds(m)
                if a not in have:
                    _add(kid, "mention", 3, sub=s["name"], term=t, month=m, start=a, end=b)
    if refresh:
        x("UPDATE jobs SET next_at=0 WHERE status='hold'")
    x("COMMIT")


def daily_refresh():
    for k in q("SELECT id FROM keywords"):
        enqueue_keyword(k["id"], refresh=True)


# ── arctic-shift 호출: 간격 지키기 + 실패 시 대기 ─────────────
def call(fn, *a):
    wait = max(state["cooldown_until"], state["next_ok"]) - time.time()
    if wait > 0:
        time.sleep(wait)
    try:
        r = fn(*a)
        state["streak"] = 0
        state["next_ok"] = time.time() + C.ARCTIC_GAP
        return r
    except arctic.SlowDown as e:
        # 422는 1초 안에 거절되는 가벼운 응답. 실측상 2초 뒤 바로 다시 보내는 편이 훨씬 잘 됨.
        # 그래도 계속 실패하면 15초→30초→…최대 10분으로 늘림.
        state["streak"] += 1
        n = state["streak"] - C.BACKOFF_FREE
        w = e.wait or (C.FAST_RETRY if n <= 0 else min(C.BACKOFF_START * 2 ** (n - 1), C.BACKOFF_MAX))
        state["cooldown_until"] = time.time() + w
        state["next_ok"] = 0
        raise


def _set(job, status, msg="", next_at=0.0, tries=None):
    x("UPDATE jobs SET status=?, msg=?, next_at=?, tries=? WHERE id=?",
      (status, msg, next_at, job["tries"] if tries is None else tries, job["id"]))


def _save_window(job, start, end, count, status):
    x("INSERT OR REPLACE INTO mention_windows VALUES(?,?,?,?,?,?,?)",
      (job["sub"], job["term"], start, end, count, status, now_iso()))


def _split(job, why):
    a, b = dt.date.fromisoformat(job["start"]), dt.date.fromisoformat(job["end"])
    mid = (a + dt.timedelta(days=(b - a).days // 2)).isoformat()
    _save_window(job, job["start"], job["end"], None, "split")
    for s, e in ((job["start"], mid), (mid, job["end"])):
        _add(job["keyword_id"], "mention", 3, sub=job["sub"], term=job["term"],
             month=job["month"], start=s, end=e)
    _set(job, "done", why)


def do_mention(job):
    sub = job["sub"]
    a, b = dt.date.fromisoformat(job["start"]), dt.date.fromisoformat(job["end"])
    days = (b - a).days
    was_hold = job["status_before"] == "hold"
    try:
        ts = call(arctic.search_posts, sub, job["term"], job["start"], job["end"])
        n = len(ts)
        if n >= 100 and days > 1:
            return _split(job, "100건 꽉 참 → 반으로 나눔")
        while len(ts) == 100:  # 하루짜리 창인데도 100건: 이어서 받기
            ts = call(arctic.search_posts, sub, job["term"], ts[-1] + 1, job["end"])
            n += len(ts)
    except arctic.BadRequest as e:
        return _set(job, "failed", str(e)[:150])
    except arctic.SlowDown as e:
        tries = job["tries"] + 1
        st = state["sub_streak"][sub] = state["sub_streak"].get(sub, 0) + 1
        later = time.time() + 6 * 3600
        if was_hold:  # 보류 재시도도 실패 → 이 서브레딧 보류분 전부 뒤로
            x("UPDATE jobs SET next_at=? WHERE sub=? AND kind='mention' AND status IN ('hold','running')", (later, sub))
            return _set(job, "hold", f"{e} (보류 재시도 실패)", later, tries)
        if st >= C.SUB_GIVEUP_STREAK:
            state["sub_streak"][sub] = 0
            x("UPDATE jobs SET status='hold', next_at=?, msg='연속 실패 → 서브레딧 전체 보류(검색 미지원일 수 있음)' "
              "WHERE sub=? AND kind='mention' AND status='queued'", (later, sub))
            return _set(job, "hold", "연속 실패 → 서브레딧 전체 보류(검색 미지원일 수 있음)", later, tries)
        if tries < C.TRIES_BEFORE_SHRINK:
            return _set(job, "queued", f"{e} · 재시도 {tries}회", 0, tries)
        if days > C.MIN_WINDOW_DAYS:
            return _split(job, f"{e} → 기간 축소")
        return _set(job, "hold", f"{e} · {tries}회 실패", time.time() + 1800, tries)
    state["sub_streak"][sub] = 0
    if was_hold:  # 풀렸으면 같은 서브레딧 보류분도 다시 진행
        x("UPDATE jobs SET status='queued', next_at=0 WHERE sub=? AND kind='mention' AND status='hold'", (sub,))
    _save_window(job, job["start"], job["end"], n, "done" if today() >= b else "open")
    _set(job, "done")


def do_totals(job):
    try:
        posts = call(arctic.monthly_totals, job["sub"], "posts")
        comments = call(arctic.monthly_totals, job["sub"], "comments")
    except arctic.BadRequest as e:
        return _set(job, "failed", str(e)[:150])
    except arctic.SlowDown as e:
        t = job["tries"] + 1
        return _set(job, "hold" if t >= 6 else "queued", f"{e} · 재시도 {t}회",
                    time.time() + 3600 if t >= 6 else 0, t)
    for m in sorted(set(posts) | set(comments)):
        x("INSERT OR REPLACE INTO sub_monthly VALUES(?,?,?,?,?)",
          (job["sub"], m, posts.get(m, 0), comments.get(m, 0), now_iso()))
    _set(job, "done", "" if posts or comments else "데이터 없음")


def do_trends(job):
    kid = job["keyword_id"]
    terms = [r["term"] for r in q("SELECT term FROM variants WHERE keyword_id=? ORDER BY rowid", (kid,))][:5]
    gprop = "" if job["gprop"] == "web" else job["gprop"]
    geo = "" if job["geo"] == "WW" else job["geo"]
    try:
        data = trends.fetch(terms, gprop, geo, C.START_DATE, today().isoformat())
    except trends.TrendsBlocked as e:
        # 구글이 429로 막으면 계속 두드릴수록 오래 막힘 → 트렌드 작업 전체를 30분, 1시간, 2시간… 뒤로 미룸
        t = job["tries"] + 1
        wait = min(1800 * 2 ** (t - 1), 6 * 3600)
        x("UPDATE jobs SET next_at=? WHERE kind='trends' AND status IN ('queued','hold')", (time.time() + wait,))
        return _set(job, "queued", f"구글이 요청 과다(429)로 잠시 막음 · {int(wait/60)}분 뒤 재시도 ({t}회째)", time.time() + wait, t)
    batch = f"{kid}:{job['gprop']}:{job['geo']}"
    x("BEGIN")
    x("DELETE FROM trends WHERE batch=?", (batch,))
    for term, pts in data.items():
        for m, v, partial in pts:
            x("INSERT OR REPLACE INTO trends VALUES(?,?,?,?,?,?,?,0,?)",
              (batch, term, job["gprop"], job["geo"], m, v, int(partial), now_iso()))
    x("COMMIT")
    _set(job, "done", "" if any(data.values()) else "데이터 없음(검색량 부족)")
    # 구글 트렌드는 연달아 부르면 막힘 → 다음 트렌드 요청은 1분 뒤에
    x("UPDATE jobs SET next_at=? WHERE kind='trends' AND status='queued' AND next_at<?", (time.time() + 60,) * 2)


def do_discover(job):
    kid = job["keyword_id"]
    mine = {r["name"].lower() for r in q("SELECT name FROM subreddits WHERE keyword_id=?", (kid,))}
    score = {}
    try:
        for own in q("SELECT name FROM subreddits WHERE keyword_id=? AND kind='own'", (kid,)):
            for author in call(arctic.top_authors, own["name"]):
                try:
                    for sub, _ in call(arctic.author_subreddits, author):
                        if sub.lower() not in mine and not sub.startswith("u_"):
                            score[sub] = score.get(sub, 0) + 1
                except arctic.SlowDown:
                    continue
    except (arctic.SlowDown, arctic.BadRequest) as e:
        if not score:
            return _set(job, "failed", str(e)[:150])
    x("DELETE FROM candidates WHERE keyword_id=?", (kid,))
    for sub, sc in sorted(score.items(), key=lambda kv: -kv[1])[:15]:
        x("INSERT OR REPLACE INTO candidates VALUES(?,?,?)", (kid, sub, sc))
    _set(job, "done")


# ── 틱톡 ──────────────────────────────────────────────
def do_tiktok(job):
    kid = job["keyword_id"]
    tags = list(dict.fromkeys(filter(None, (tiktok.tag_for(r["term"]) for r in q(
        "SELECT term FROM variants WHERE keyword_id=? ORDER BY rowid", (kid,))))))
    try:
        stats = tiktok.hashtag_stats(tags)
    except tiktok.TikTokError as e:
        t = job["tries"] + 1
        wait = 1800 * t if t < 4 else 20 * 3600
        return _set(job, "hold" if t >= 4 else "queued", f"{e} · 재시도 {t}회"[:150], time.time() + wait, t)
    for tag, st in stats.items():
        if st:
            x("INSERT OR REPLACE INTO tt_snapshots VALUES(?,?,?,?,?)", (kid, tag, today().isoformat(), st["videos"], st["views"]))
    _set(job, "done", "" if any(stats.values()) else "해시태그 없음")


# ── 유튜브 ──────────────────────────────────────────────
def yt_key():
    if C.YT_API_KEY:
        return C.YT_API_KEY
    r = q("SELECT value FROM meta WHERE key='yt_key'")
    return r[0]["value"] if r else ""


def _yt_fail(job, e):
    if isinstance(e, youtube.QuotaExceeded):   # 한도는 태평양 시간 자정에 초기화 → 그때까지 유튜브 작업만 미룸
        pt = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=8)
        reset = (pt.replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(days=1, minutes=10)
                 + dt.timedelta(hours=8)).timestamp()
        x("UPDATE jobs SET next_at=?, msg='유튜브 하루 한도 소진 → 내일 이어서' "
          "WHERE kind IN ('yt','ytstats','ytc') AND status IN ('queued','running')", (reset,))
        return _set(job, "queued", "유튜브 하루 한도 소진 → 내일 이어서", reset)
    if isinstance(e, youtube.KeyInvalid):
        x("UPDATE jobs SET status='hold', next_at=?, msg=? WHERE kind IN ('yt','ytstats') AND status='queued'",
          (time.time() + 86400, f"API 키 문제: {e}"[:150]))
        return _set(job, "hold", f"API 키 문제: {e}"[:150], time.time() + 86400)
    t = job["tries"] + 1
    wait = 6 * 3600 if t >= 5 else 60 * 2 ** t
    x("UPDATE jobs SET next_at=? WHERE kind IN ('yt','ytc') AND status='queued' AND keyword_id=?",
      (time.time() + min(wait, 1800), job["keyword_id"]))
    return _set(job, "hold" if t >= 5 else "queued", f"{e} · 재시도 {t}회"[:150], time.time() + wait, t)


def _yt_store(kid, key, ids, terms):
    """통계를 받아 저장. 제목·설명·태그에 키워드가 실제로 든 영상만 남김."""
    kept = 0
    for i in range(0, len(ids), 50):
        for v in youtube.videos(key, ids[i:i + 50]):
            if not any(t in v["text"] or t.replace(" ", "") in v["text"] for t in terms):
                continue
            kept += 1
            x("INSERT OR REPLACE INTO yt_videos VALUES(?,?,?,?,?,?,?,?,?,?)",
              (kid, v["id"], v["published"], v["title"], v["channel"], v["views"], v["likes"], v["comments"],
               now_iso(), now_iso()))
    return kept


def _ytw(fn, *a):
    wait = state.get("ytw_next", 0) - time.time()
    if wait > 0:
        time.sleep(wait)
    try:
        return fn(*a)
    finally:
        state["ytw_next"] = time.time() + C.YT_WEB_GAP


def do_yt_web(job):
    """키 없는 방식: 한 달 기간으로 검색해 영상 목록·조회수를 받음."""
    kid = job["keyword_id"]
    terms = [r["term"] for r in q("SELECT term FROM variants WHERE keyword_id=?", (kid,))]
    b = dt.date.fromisoformat(job["end"])
    found, capped = {}, 0
    try:
        for t in terms:
            token, pages = None, 0
            while True:
                vids, token = _ytw(youtube_web.search_page, t, job["start"], job["end"], token)
                pages += 1
                for v in vids:   # 검색은 비슷한 단어도 끌고 오므로, 제목·설명·채널명에 키워드가 실제로 든 것만
                    if any(k in v["text"] or k.replace(" ", "") in v["text"] for k in terms):
                        found[v["id"]] = v
                if not token or not vids:
                    break
                if pages >= C.YT_WEB_MAX_PAGES:
                    capped = 1
                    break
    except youtube.YTError as e:
        return _yt_fail(job, e)
    x("BEGIN")
    for v in found.values():   # 이미 있는 영상은 조회수만 갱신(댓글 수·정확한 게시일은 유지)
        x("INSERT INTO yt_videos(keyword_id,video_id,published,title,channel,views,likes,comments,updated_at) "
          "VALUES(?,?,?,?,?,?,0,0,?) ON CONFLICT(keyword_id,video_id) DO UPDATE SET "
          "views=excluded.views, title=excluded.title, channel=excluded.channel, updated_at=excluded.updated_at",
          (kid, v["id"], job["start"], v["title"], v["channel"], v["views"], now_iso()))
    x("INSERT OR REPLACE INTO yt_windows VALUES(?,?,?,?,?,?,?)",
      (kid, job["start"], job["end"], "done" if today() >= b else "open", len(found), capped, now_iso()))
    x("COMMIT")
    _set(job, "done")


def do_ytc(job):
    """키 없는 방식: 영상별 댓글 수·정확한 게시일을 조회수 많은 순으로 조금씩 받음."""
    kid = job["keyword_id"]
    stale = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=C.YT_COMMENT_STALE_DAYS)).isoformat(timespec="seconds")
    rows = q("SELECT video_id FROM yt_videos WHERE keyword_id=? AND (comments_at IS NULL OR comments_at<?) "
             "ORDER BY comments_at IS NOT NULL, views DESC LIMIT ?", (kid, stale, C.YT_COMMENT_BATCH))
    try:
        for r in rows:
            d = _ytw(youtube_web.details, r["video_id"])
            x("UPDATE yt_videos SET comments=?, views=IFNULL(?,views), published=IFNULL(?,published), comments_at=? "
              "WHERE keyword_id=? AND video_id=?", (d["comments"], d["views"], d["published"], now_iso(), kid, r["video_id"]))
    except youtube.YTError as e:
        return _yt_fail(job, e)
    _snapshot(kid)
    _set(job, "done")
    if len(rows) == C.YT_COMMENT_BATCH:   # 아직 남았으면 이어서
        _add(kid, "ytc", 4, month="0000-00")


def _snapshot(kid):
    r = q("SELECT COUNT(*) n, IFNULL(SUM(views),0) v, IFNULL(SUM(comments),0) c FROM yt_videos WHERE keyword_id=?", (kid,))[0]
    x("INSERT OR REPLACE INTO yt_snapshots VALUES(?,?,?,?,?)", (kid, today().isoformat(), r["n"], r["v"], r["c"]))


def do_yt(job):
    kid, key = job["keyword_id"], yt_key()
    if not key:
        return do_yt_web(job)
    terms = [r["term"] for r in q("SELECT term FROM variants WHERE keyword_id=?", (kid,))]
    a, b = dt.date.fromisoformat(job["start"]), dt.date.fromisoformat(job["end"])
    try:
        ids, token, pages, capped = [], None, 0, 0
        while True:
            page, token = youtube.search(key, youtube.query_for(terms), job["start"], job["end"], token)
            ids += page
            pages += 1
            if not token or not page:
                break
            if pages >= C.YT_MAX_PAGES:
                if (b - a).days > 1:    # 500개를 넘김 → 기간을 반으로 나눠 다시
                    mid = (a + dt.timedelta(days=(b - a).days // 2)).isoformat()
                    x("INSERT OR REPLACE INTO yt_windows VALUES(?,?,?,?,?,?,?)",
                      (kid, job["start"], job["end"], "split", None, 0, now_iso()))
                    for s, e in ((job["start"], mid), (mid, job["end"])):
                        _add(kid, "yt", 2, month=s[:7], start=s, end=e)
                    return _set(job, "done", "500개 초과 → 기간을 나눔")
                capped = 1
                break
        ids = list(dict.fromkeys(ids))
        kept = _yt_store(kid, key, ids, terms)
    except (youtube.QuotaExceeded, youtube.KeyInvalid, youtube.YTError) as e:
        return _yt_fail(job, e)
    x("INSERT OR REPLACE INTO yt_windows VALUES(?,?,?,?,?,?,?)",
      (kid, job["start"], job["end"], "done" if today() >= b else "open", kept, capped, now_iso()))
    _set(job, "done")


def do_ytstats(job):
    """모든 영상의 조회수·댓글 수를 새로 받고, 오늘의 누적값을 기록."""
    kid, key = job["keyword_id"], yt_key()
    if not key:   # 키 없는 방식에서는 검색 때 받은 조회수로 오늘 누적값만 기록
        _snapshot(kid)
        return _set(job, "done")
    terms = [r["term"] for r in q("SELECT term FROM variants WHERE keyword_id=?", (kid,))]
    ids = [r["video_id"] for r in q("SELECT video_id FROM yt_videos WHERE keyword_id=?", (kid,))]
    try:
        _yt_store(kid, key, ids, terms)
    except (youtube.QuotaExceeded, youtube.KeyInvalid, youtube.YTError) as e:
        return _yt_fail(job, e)
    r = q("SELECT COUNT(*) n, IFNULL(SUM(views),0) v, IFNULL(SUM(comments),0) c FROM yt_videos WHERE keyword_id=?", (kid,))[0]
    x("INSERT OR REPLACE INTO yt_snapshots VALUES(?,?,?,?,?)", (kid, today().isoformat(), r["n"], r["v"], r["c"]))
    _set(job, "done")


# ── 레딧 전체 언급 (PullPush) ───────────────────────────────
def _pp(fn, *a):
    wait = state["pp_next"] - time.time()
    if wait > 0:
        time.sleep(min(wait, C.PULLPUSH_GAP))
    try:
        r = fn(*a)
        state["pp_streak"] = 0
        return r
    finally:
        state["pp_next"] = time.time() + C.PULLPUSH_GAP


def _coverage(kind):
    """아카이브가 어디까지 들어와 있는지(epoch). 12시간마다 다시 확인."""
    r = q("SELECT value, updated_at FROM meta WHERE key=?", (f"pp_cov_{kind}",))
    if r and time.time() - r[0]["updated_at"] < 12 * 3600:
        return int(r[0]["value"])
    cov = _pp(pullpush.latest, kind)
    x("INSERT OR REPLACE INTO meta VALUES(?,?,?)", (f"pp_cov_{kind}", str(cov), time.time()))
    return cov


def _epoch(iso):
    return int(dt.datetime.fromisoformat(iso).replace(tzinfo=dt.timezone.utc).timestamp())


def do_global(job):
    kid, kind, m = job["keyword_id"], job["term"], job["month"]
    a, b = (_epoch(d) for d in month_bounds(m))
    terms = [r["term"] for r in q("SELECT term FROM variants WHERE keyword_id=?", (kid,))]
    try:
        cov = _coverage(kind)
        if cov < a:
            return _set(job, "done", "아카이브에 아직 없는 달")
        seen, subs = set(), {}
        for t in terms:                      # 변형마다 검색하고, 같은 글·댓글은 한 번만 셈
            after = a - 1
            while True:
                page = _pp(pullpush.search, kind, t, after, b)
                new = [p for p in page if p[0] not in seen]
                for pid, _, sub in new:
                    seen.add(pid)
                    subs[sub] = subs.get(sub, 0) + 1
                if len(page) < 100:
                    break
                after = page[-1][1] - 1 if new else page[-1][1]
    except arctic.BadRequest as e:
        return _set(job, "failed", str(e)[:150])
    except arctic.SlowDown as e:
        # 막힌 동안 계속 두드리면 더 오래 막힘 → 레딧 전체 검색 작업을 통째로 미루고 다른 작업 진행
        state["pp_streak"] += 1
        wait = min(C.PULLPUSH_BLOCK_WAIT * 2 ** (state["pp_streak"] - 1), 3600)
        x("UPDATE jobs SET next_at=? WHERE kind='global' AND status IN ('queued','running')", (time.time() + wait,))
        return _set(job, "queued", f"{e} · {int(wait/60)}분 쉬고 재시도 {job['tries']+1}회", time.time() + wait, job["tries"] + 1)
    x("BEGIN")
    x("DELETE FROM global_counts WHERE keyword_id=? AND kind=? AND month=?", (kid, kind, m))
    for sub, n in subs.items():
        x("INSERT INTO global_counts VALUES(?,?,?,?,?)", (kid, kind, m, sub, n))
    x("INSERT OR REPLACE INTO global_months VALUES(?,?,?,?,?,?)",
      (kid, kind, m, "done" if cov >= b else "open", len(seen), now_iso()))
    x("COMMIT")
    _set(job, "done")


HANDLERS = {"tiktok": do_tiktok, "yt": do_yt, "ytc": do_ytc, "ytstats": do_ytstats, "global": do_global, "totals": do_totals, "trends": do_trends, "mention": do_mention, "discover": do_discover}


def _next():
    now = time.time()
    r = q("SELECT * FROM jobs WHERE status='queued' AND next_at<=? "
          "ORDER BY priority, month DESC, id LIMIT 1", (now,))
    if not r:
        r = q("SELECT * FROM jobs WHERE status='hold' AND next_at<=? ORDER BY next_at, id LIMIT 1", (now,))
    return dict(r[0]) if r else None


def describe(j):
    if j["kind"] == "mention":
        span = j["month"] if (j["start"], j["end"]) == month_bounds(j["month"]) else f"{j['start']}~{j['end']}"
        s = f"r/{j['sub']} '{j['term']}' {span} 언급 검색"
    elif j["kind"] == "tiktok":
        s = "틱톡 해시태그 통계"
    elif j["kind"] == "yt":
        s = f"유튜브 영상 검색 {j['start'][:7]}" + ("" if (j["start"], j["end"]) == month_bounds(j["start"][:7]) else f" ~ {j['end'][:7]}")
    elif j["kind"] == "ytc":
        s = "유튜브 댓글 수 받는 중"
    elif j["kind"] == "ytstats":
        s = "유튜브 조회수·댓글 수 갱신"
    elif j["kind"] == "global":
        s = f"레딧 전체 {'글' if j['term'] == 'submission' else '댓글'} {j['month']} 검색"
    elif j["kind"] == "totals":
        s = f"r/{j['sub']} 월별 총계"
    elif j["kind"] == "trends":
        s = f"구글 트렌드 {j['gprop']}·{j['geo']}"
    else:
        s = "관련 서브레딧 찾기"
    return s + (f" (재시도 {j['tries']}회)" if j["tries"] else "")


def loop():
    while True:
        try:
            job = _next()
            if not job:
                state["current"] = None
                time.sleep(2)
                continue
            job["status_before"] = job["status"]
            x("UPDATE jobs SET status='running' WHERE id=?", (job["id"],))
            state["current"] = job
            HANDLERS[job["kind"]](job)
        except Exception as e:  # 예상 못 한 오류로 워커가 죽지 않게
            if state["current"]:
                x("UPDATE jobs SET status='failed', msg=? WHERE id=?",
                  (f"{type(e).__name__}: {e}"[:200], state["current"]["id"]))
            time.sleep(2)


def start():
    threading.Thread(target=loop, daemon=True, name="worker").start()
