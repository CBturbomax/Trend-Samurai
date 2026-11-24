import sqlite3
import threading
from pathlib import Path

from .config import DB_PATH

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS keywords(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE, created_at TEXT);
CREATE TABLE IF NOT EXISTS variants(
  keyword_id INTEGER, term TEXT, PRIMARY KEY(keyword_id, term));
CREATE TABLE IF NOT EXISTS subreddits(
  keyword_id INTEGER, name TEXT, kind TEXT,          -- own | mention
  PRIMARY KEY(keyword_id, name));
CREATE TABLE IF NOT EXISTS candidates(
  keyword_id INTEGER, name TEXT, score INTEGER, PRIMARY KEY(keyword_id, name));
CREATE TABLE IF NOT EXISTS sub_monthly(
  sub TEXT, month TEXT, posts INTEGER, comments INTEGER, updated_at TEXT,
  PRIMARY KEY(sub, month));
CREATE TABLE IF NOT EXISTS mention_windows(
  sub TEXT, term TEXT, start TEXT, end TEXT,
  count INTEGER, status TEXT,                        -- done | open(진행 중인 달) | split
  updated_at TEXT, PRIMARY KEY(sub, term, start, end));
CREATE TABLE IF NOT EXISTS trends(
  batch TEXT, term TEXT, gprop TEXT, geo TEXT, month TEXT,
  value REAL, partial INTEGER, adjusted INTEGER DEFAULT 0, updated_at TEXT,
  PRIMARY KEY(batch, term, month));
CREATE TABLE IF NOT EXISTS global_counts(             -- 레딧 전체 언급: 서브레딧별 건수
  keyword_id INTEGER, kind TEXT, month TEXT, sub TEXT, count INTEGER,
  PRIMARY KEY(keyword_id, kind, month, sub));
CREATE TABLE IF NOT EXISTS global_months(
  keyword_id INTEGER, kind TEXT, month TEXT, status TEXT,   -- done | open(아카이브가 이 달 중간까지만 있음)
  total INTEGER, updated_at TEXT, PRIMARY KEY(keyword_id, kind, month));
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS yt_videos(
  keyword_id INTEGER, video_id TEXT, published TEXT, title TEXT, channel TEXT,
  views INTEGER, likes INTEGER, comments INTEGER, updated_at TEXT,
  PRIMARY KEY(keyword_id, video_id));
CREATE TABLE IF NOT EXISTS yt_windows(
  keyword_id INTEGER, start TEXT, end TEXT, status TEXT,    -- done | open | split
  n INTEGER, capped INTEGER DEFAULT 0, updated_at TEXT, PRIMARY KEY(keyword_id, start, end));
CREATE TABLE IF NOT EXISTS yt_snapshots(                    -- 매일 기록하는 누적값 → 앞으로의 진짜 증가 추세
  keyword_id INTEGER, day TEXT, videos INTEGER, views INTEGER, comments INTEGER,
  PRIMARY KEY(keyword_id, day));
CREATE TABLE IF NOT EXISTS tt_history(                       -- 틱톡 활동 지수(주간, 과거 2년) — MCP(TickerTrends) 경유로 채움
  keyword_id INTEGER, week TEXT, value REAL, PRIMARY KEY(keyword_id, week));
CREATE TABLE IF NOT EXISTS tt_snapshots(                    -- 틱톡 해시태그 누적값(매일 기록)
  keyword_id INTEGER, tag TEXT, day TEXT, videos INTEGER, views INTEGER,
  PRIMARY KEY(keyword_id, tag, day));
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY, keyword_id INTEGER, kind TEXT,   -- totals | trends | mention | discover
  sub TEXT, term TEXT, month TEXT, start TEXT, end TEXT, gprop TEXT, geo TEXT,
  status TEXT DEFAULT 'queued',                      -- queued | running | done | hold | failed
  tries INTEGER DEFAULT 0, next_at REAL DEFAULT 0, msg TEXT DEFAULT '',
  priority INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS jobs_pick ON jobs(status, priority, month);
"""


def conn() -> sqlite3.Connection:
    c = getattr(_local, "c", None)
    if c is None:
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        _local.c = c
    return c


def init():
    conn().executescript(SCHEMA)
    try:  # 추천 서브레딧에서 최근 1년간 키워드가 실제로 언급된 글 수
        conn().execute("ALTER TABLE candidates ADD COLUMN hits INTEGER")
    except sqlite3.OperationalError:
        pass
    try:  # 댓글 수를 마지막으로 받은 시각(키 없는 방식에서 영상별로 따로 받음)
        conn().execute("ALTER TABLE yt_videos ADD COLUMN comments_at TEXT")
    except sqlite3.OperationalError:
        pass
    # 서버가 꺼질 때 돌던 작업은 다시 대기열로
    conn().execute("UPDATE jobs SET status='queued' WHERE status='running'")


def q(sql, args=()):
    return conn().execute(sql, args).fetchall()


def x(sql, args=()):
    return conn().execute(sql, args)
