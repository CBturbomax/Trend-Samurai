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
BOUNDS = {"arctic": (1.5, 12.0), "trends": (90.0, 1200.0), "youtube": (1.0, 12.0)}
GAPS = {"arctic": C.ARCTIC_GAP, "trends": 180.0, "youtube": C.YT_WEB_GAP}
NAMES = {"arctic": "레딧", "trends": "구글 트렌드", "youtube": "유튜브", "tiktok": "틱톡"}


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


def tune():
    # 레딧(아카이브): 성공률을 보고 간격 조절
    n, ok = _window("arctic", 900)
    if n >= 20:
        r = ok / n
        if r >= 0.7:
            _set_gap("arctic", GAPS["arctic"] * 0.85, f"최근 15분 성공 {r:.0%}")
        elif r < 0.35:
            _set_gap("arctic", GAPS["arctic"] * 1.3, f"최근 15분 성공 {r:.0%}")
    # 구글 트렌드: 최근 1시간에 막힌 적 있으면 느리게, 계속 잘 되면 조금씩 빠르게
    n, ok = _window("trends", 3600)
    if n:
        if ok < n:
            _set_gap("trends", GAPS["trends"] * 1.5, f"최근 1시간 {n - ok}번 막힘")
        elif n >= 6:
            _set_gap("trends", GAPS["trends"] * 0.85, f"최근 1시간 {n}번 연속 성공")
    # 유튜브
    n, ok = _window("youtube", 900)
    if n >= 10:
        r = ok / n
        if r < 0.7:
            _set_gap("youtube", GAPS["youtube"] * 1.5, f"최근 15분 성공 {r:.0%}")
        elif r > 0.97:
            _set_gap("youtube", GAPS["youtube"] * 0.9, f"최근 15분 성공 {r:.0%}")
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
    for src in ("arctic", "youtube", "trends", "tiktok"):
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
        if r:
            GAPS[src] = float(r[0]["value"])
    C.ARCTIC_GAP, C.YT_WEB_GAP = GAPS["arctic"], GAPS["youtube"]
    note(f"시작: 레딧 {GAPS['arctic']:g}초 · 유튜브 {GAPS['youtube']:g}초 · 구글 {GAPS['trends']:g}초 간격")
    threading.Thread(target=_loop, daemon=True, name="autotune").start()
