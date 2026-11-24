"""저장소에 넣어 둔 키워드·틱톡 과거 추이(seed/ 폴더)를 서버 시작 때 DB에 채운다.

- seed/keywords.txt : 한 줄에 키워드 하나(# 뒤는 메모). DB에 없으면 새로 추가한다.
  변형은 화면에서 기본 체크되는 것, 서브레딧은 이름이 키워드와 똑같은 것만 자동으로 붙인다.
  화면에서 지운 키워드는 다시 만들지 않는다(meta 에 기록).
- seed/tiktok/<키워드>.csv : week,value — TickerTrends 틱톡 활동 지수(주간, 최근 2년).
  Claude 채팅의 MCP로 받아 파일로 넣는다. 파일명은 띄어쓰기를 _ 로.
"""
import threading
import time

from . import config as C
from . import variants, worker
from .db import q, x
from .sources import arctic

SEED = C.ROOT / "seed"


def _keywords():
    f = SEED / "keywords.txt"
    if not f.exists():
        return []
    out = []
    for line in f.read_text(encoding="utf-8").splitlines():
        k = line.split("#", 1)[0].strip().lower()
        if k:
            out.append(k)
    return out


def _add_own_subs(kids):
    """이름이 키워드(변형)와 똑같은 서브레딧이 있으면 전용 서브레딧으로 붙인다. 네트워크라 백그라운드에서."""
    for kid in kids:
        terms = [r["term"] for r in q("SELECT term FROM variants WHERE keyword_id=?", (kid,))]
        names = {t.replace(" ", "").replace("-", "") for t in terms}
        found = []
        for attempt in range(3):
            try:
                found = [s["name"] for s in arctic.find_subreddits(min(names, key=len))
                         if s["name"].lower() in names]
                break
            except Exception:
                time.sleep(4)
        for s in found:
            x("INSERT OR IGNORE INTO subreddits VALUES(?,?,'own')", (kid, s))
        if found:
            worker.enqueue_keyword(kid)
            worker._add(kid, "discover", 0)
        time.sleep(C.ARCTIC_GAP)


def load():
    new = []
    for name in _keywords():
        if q("SELECT 1 FROM keywords WHERE name=?", (name,)) or q(
                "SELECT 1 FROM meta WHERE key=?", (f"seeded:{name}",)):
            continue
        x("INSERT INTO keywords(name,created_at) VALUES(?,?)", (name, worker.now_iso()))
        kid = q("SELECT id FROM keywords WHERE name=?", (name,))[0]["id"]
        for t, checked in variants.suggest(name):
            if checked:
                x("INSERT OR IGNORE INTO variants VALUES(?,?)", (kid, t))
        x("INSERT OR REPLACE INTO meta VALUES(?,?,?)", (f"seeded:{name}", "1", time.time()))
        new.append(kid)

    d = SEED / "tiktok"
    if d.exists():
        for f in sorted(d.glob("*.csv")):
            name = f.stem.replace("_", " ").lower()
            r = q("SELECT id FROM keywords WHERE name=?", (name,))
            if not r:
                continue
            rows = [ln.split(",") for ln in f.read_text(encoding="utf-8").split() if "," in ln]
            for wk, v in rows:
                x("INSERT OR REPLACE INTO tt_history VALUES(?,?,?)", (r[0]["id"], wk, float(v)))

    if new:
        threading.Thread(target=_add_own_subs, args=(new,), daemon=True).start()
