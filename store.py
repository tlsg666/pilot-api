"""Stage 4 · 持久化层：SQLite 两张表 runs / results。所有 SQL 只出现在这个文件里。

为什么两张表：runs 存"一次任务"的汇总（列表页/进度条要），results 存"每条用例"的明细
（失败详情、Stage 7 的 flaky 统计要）。一张表撑不住这两种粒度。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("PLATFORM_DB") or (BASE_DIR / "platform.db"))

_LOCK = threading.Lock()          # sqlite3 连接不线程安全：单连接 + 全局锁，MVP 够用
_CONN: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
    id          TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    total       INTEGER NOT NULL,
    finished    INTEGER NOT NULL DEFAULT 0,
    passed      INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    error       INTEGER NOT NULL DEFAULT 0,
    request_id  TEXT UNIQUE,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS results(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    case_name   TEXT NOT NULL,
    case_file   TEXT NOT NULL,
    status      TEXT NOT NULL,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    exit_code   INTEGER NOT NULL DEFAULT -1,
    message     TEXT NOT NULL DEFAULT '',
    report_dir  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    UNIQUE(run_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_results_case ON results(case_name, id DESC);
CREATE INDEX IF NOT EXISTS idx_results_run  ON results(run_id, seq);
"""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _conn() -> sqlite3.Connection:
    global _CONN
    if _CONN is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        c.commit()
        _CONN = c
    return _CONN


@contextmanager
def tx():
    """所有读写都走这里：加锁 → 执行 → 提交/回滚。"""
    with _LOCK:
        c = _conn()
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise


def close() -> None:
    """测试用：断开连接，模拟进程重启（数据在文件里，不会丢）。"""
    global _CONN
    with _LOCK:
        if _CONN is not None:
            _CONN.close()
            _CONN = None


# ---------- runs ----------

def create_run(run_id: str, total: int, request_id: str | None = None) -> dict:
    with tx() as c:
        c.execute("INSERT INTO runs(id,status,total,request_id,created_at) VALUES(?,?,?,?,?)",
                  (run_id, "queued", total, request_id, _now()))
    return get_run(run_id)


def mark_running(run_id: str) -> None:
    with tx() as c:
        c.execute("UPDATE runs SET status='running', started_at=COALESCE(started_at,?) "
                  "WHERE id=? AND status='queued'", (_now(), run_id))


def finish_run(run_id: str) -> None:
    with tx() as c:
        c.execute("UPDATE runs SET status='finished', finished_at=? WHERE id=?", (_now(), run_id))


def get_run(run_id: str) -> dict | None:
    with tx() as c:
        run = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            return None
        rows = c.execute("SELECT * FROM results WHERE run_id=? ORDER BY seq", (run_id,)).fetchall()
    data = dict(run)
    data["results"] = [dict(r) for r in rows]
    return data


def get_run_by_request(request_id: str) -> dict | None:
    with tx() as c:
        r = c.execute("SELECT * FROM runs WHERE request_id=?", (request_id,)).fetchone()
    return dict(r) if r else None


def list_runs(limit: int = 20) -> list[dict]:
    with tx() as c:
        rows = c.execute(
            "SELECT * FROM runs ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def count_runs() -> int:
    with tx() as c:
        return c.execute("SELECT COUNT(*) FROM runs").fetchone()[0]


def recover_interrupted() -> int:
    """进程重启后，上次没跑完的 run 不能永远停在 running。"""
    with tx() as c:
        cur = c.execute("UPDATE runs SET status='interrupted' "
                        "WHERE status IN ('queued','running')")
        return cur.rowcount


# ---------- results ----------

def add_result(run_id: str, seq: int, case_name: str, case_file: str, res) -> None:
    col = {"passed": "passed", "failed": "failed", "error": "error"}[res.status]
    with tx() as c:
        c.execute(
            "INSERT OR REPLACE INTO results(run_id,seq,case_name,case_file,status,"
            "duration_ms,exit_code,message,report_dir,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (run_id, seq, case_name, case_file, res.status, res.duration_ms,
             res.exit_code, res.message, res.report_dir, _now()))
        # 计数在同一个事务里更新，避免"明细有了、汇总没加"
        c.execute(f"UPDATE runs SET finished=finished+1, {col}={col}+1 WHERE id=?", (run_id,))


def recent_statuses(case_name: str, limit: int = 5) -> list[str]:
    """Stage 7 的 flaky 统计要用：某用例最近 N 次的结果。"""
    with tx() as c:
        rows = c.execute("SELECT status FROM results WHERE case_name=? ORDER BY id DESC LIMIT ?",
                         (case_name, limit)).fetchall()
    return [r[0] for r in rows]

def recent_case_rows(limit: int = 2000) -> list[dict]:
    """给 Stage 7 用：最近的明细行，按 id 倒序（最新在前）。"""
    with tx() as c:
        rows = c.execute(
            "SELECT id, case_name, status, message, exit_code, duration_ms, created_at "
            "FROM results ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]