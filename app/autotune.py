"""자동 조절·자가 점검.

- 사이트별 최근 성공·실패를 기록하고, 5분마다 요청 간격을 스스로 조절한다(성공이 많으면 빠르게, 막히면 느리게).
- 댓글 검색이 계속 안 되는 커뮤니티는 스스로 빼서 헛수고를 줄인다.
- 조절 내역은 /api/health 와 화면의 '자동 조절' 칸에서 볼 수 있다.
차단을 피하려고 IP를 바꾸는 식의 우회는 하지 않는다. 간격·순서·불필요한 요청 줄이기로만 조절한다.
"""
import collections
import threading
import time

from . import config as C
from .db import q, x

STATS = collections.defaultdict(lambda: collections.deque(maxlen=3000))   # source → (시각, 성공여부)
LOG = collections.deque(maxlen=60)
PAUSE = {}   # source → 이 시각까지 쉼
BOUNDS = {"arctic": (1.5, 4.0), "trends": (40.0, 1200.0), "youtube": (4.0, 15.0)}
GAPS = {"arctic": C.ARCTIC_GAP, "trends": 180.0, "youtube": C.YT_WEB_GAP}
NAMES = {"arctic": "레딧", "trends": "구글 트렌드", "youtube": "유튜브", "tiktok": "틱톡", "pinterest": "핀터레스트"}


def record(src, ok):
    STATS[src].append((time.time(), bool(ok)))


def gap(src):
    return GAPS[src]


def _window(src, sec):
    t0 = time.time() - sec
    v = [ok for t, ok in STATS[src] if t >= t0]
    return len(v), sum(v)


def note(msg):
    LOG.appendleft({"t": time.strftime("%m-%d %H:%M"), "msg": msg})


def _set_gap(src, v, why):
    lo, hi = BOUNDS[src]
    v = round(min(hi, max(lo, v)), 2)
    if abs(v - GAPS[src]) >= 0.05:
        note(f"{NAMES[src]} 요청 간격 {GAPS[src]:g}초 → {v:g}초 ({why})")
        GAPS[src] = v
        if src == "arctic":
            C.ARCTIC_GAP = v
        if src == "youtube":
            C.YT_WEB_GAP = v
        x("INSERT OR REPLACE INTO meta VALUES(?,?,?)", (f"tune:{src}", str(v), time.time()))


LAST = {"t": 0.0}


def _since(src, t0):
    v = [ok for t, ok in STATS[src] if t >= t0]
    return len(v), sum(v)


def tune():
    """직전 조절 이후 '새로 생긴' 결과만 보고 판단(같은 실패를 매번 다시 세지 않음)."""
    t0, LAST["t"] = LAST["t"] or time.time() - 300, time.time()
    # 레딧(아카이브): 422는 서버 부하라 오래 쉬어도 안 나아짐(실측) → 1.5~4초 사이에서만 조절
    n, ok = _since("arctic", t0)
    if n >= 15:
        r = ok / n
        if r >= 0.6:
            _set_gap("arctic", GAPS["arctic"] * 0.85, f"최근 5분 성공 {r:.0%}")
        elif r < 0.25:
            _set_gap("arctic", GAPS["arctic"] * 1.2, f"최근 5분 성공 {r:.0%}")
    # 구글 트렌드: 새로 막혔으면 느리게, 1시간 동안 막힘 없이 5번 넘게 성공하면 25% 당김
    n_new, ok_new = _since("trends", t0)
    n_h, ok_h = _window("trends", 3600)
    if n_new > ok_new:
        _set_gap("trends", GAPS["trends"] * 1.5, "방금 구글이 막음")
    elif n_h >= 5 and ok_h == n_h:
        _set_gap("trends", GAPS["trends"] * 0.75, f"최근 1시간 {n_h}번 연속 성공")
    # 유튜브: 403 등 실패가 15% 넘으면 느리게, 20번 넘게 전부 성공하면 조금 당김
    n, ok = _since("youtube", t0)
    if n >= 8:   # 간격이 길면 5분에 20번도 못 하므로 '전부 성공'이 아니라 비율로 판단
        r = ok / n
        if False and r < 0.8 and GAPS["youtube"] >= BOUNDS["youtube"][1]:   # 쉬기는 효과 없음(쉬고 나서도 성공률 그대로, 실측) → 끔   # 간격을 최대로 늘려도 막힘 → 45분 쉬고 재개(제한이 풀리길 기다림)
            PAUSE["youtube"] = time.time() + 2700
            note(f"유튜브 45분 쉬기 (최대 간격에서도 성공 {r:.0%})")
            _set_gap("youtube", 3.0, "쉬고 난 뒤 다시 시작")
        elif r < 0.85:   # 실패가 15%를 넘으면 재시도 대기로 오히려 느려짐 → 간격을 늘림
            _set_gap("youtube", GAPS["youtube"] * 1.3, f"최근 5분 성공 {r:.0%}")
        elif r >= 0.95:
            _set_gap("youtube", GAPS["youtube"] * 0.85, f"최근 5분 성공 {r:.0%}")
    # 댓글 검색이 한 번도 안 된 커뮤니티는 빼기
    for r in q("SELECT key, value FROM meta WHERE key LIKE 'cfail:%' AND CAST(value AS INTEGER) >= 25"):
        sub = r["key"][6:]
        if q("SELECT 1 FROM meta WHERE key IN (?,?)", (f"cok:{sub}", f"nocomment:{sub}")):
            continue
        x("INSERT OR REPLACE INTO meta VALUES(?,?,?)", (f"nocomment:{sub}", "1", time.time()))
        n_del = x("DELETE FROM jobs WHERE kind='mention' AND sub=? AND term LIKE 'c:%' AND status!='done'", (sub,)).rowcount
        note(f"r/{sub} 댓글 검색이 {r['value']}번 연속 실패(아카이브 미지원) → 댓글 검색 {n_del}건 빼고 글 검색만 유지")


def summary():
    out = {}
    for src in ("arctic", "youtube", "trends", "tiktok", "pinterest"):
        n, ok = _window(src, 1800)
        out[src] = {"name": NAMES[src], "n30": n, "ok30": ok, "gap": GAPS.get(src)}
    return out


def _loop():
    while True:
        time.sleep(300)
        try:
            tune()
        except Exception as e:
            note(f"자동 조절 오류: {type(e).__name__}")


def start():
    for src in GAPS:   # 지난번에 맞춰 둔 값으로 시작
        r = q("SELECT value FROM meta WHERE key=?", (f"tune:{src}",))
        if r:   # 지난번 값으로 시작하되 허용 범위 안으로
            lo, hi = BOUNDS[src]
            GAPS[src] = min(hi, max(lo, float(r[0]["value"])))
    for src, (lo, hi) in BOUNDS.items():   # 저장값이 없어도 허용 범위 안으로
        GAPS[src] = min(hi, max(lo, GAPS[src]))
    GAPS["trends"] = min(GAPS["trends"], 180.0)   # 예전 버그로 부풀었던 값은 3분으로 되돌림
    C.ARCTIC_GAP, C.YT_WEB_GAP = GAPS["arctic"], GAPS["youtube"]
    note(f"시작: 레딧 {GAPS['arctic']:g}초 · 유튜브 {GAPS['youtube']:g}초 · 구글 {GAPS['trends']:g}초 간격")
    threading.Thread(target=_loop, daemon=True, name="autotune").start()
