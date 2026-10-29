"""Stage 4 · 队列 + 4 并发 worker + 单用例超时 + SQLite 持久化 + 中断恢复。

接口契约和 Stage 3 保持一致（POST /runs、GET /runs、GET /runs/{id}），
换掉的是内部：内存 dict → SQLite；线程池 → 显式队列 + worker 线程；队列粒度 run → case。
"""
from __future__ import annotations

import os
import queue
import threading
import uuid
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

import runner
import store

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent
DEMO_DIR = BASE_DIR / "demo"

MAX_WORKERS = int(os.environ.get("PLATFORM_WORKERS", "4"))
DEFAULT_TIMEOUT = int(os.environ.get("PLATFORM_CASE_TIMEOUT", "120"))

app = FastAPI(title="接口测试平台", version="0.4")

TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "templates"))

QUEUE: "queue.Queue" = queue.Queue()          # 队列粒度：一条用例一个任务
_REMAINING: dict[str, int] = {}               # run_id -> 还剩几条没跑完
_STATE_LOCK = threading.Lock()
_WORKERS: list[threading.Thread] = []


class RunReq(BaseModel):
    cases: list[str] = Field(..., min_length=1)
    request_id: str | None = None             # 幂等键
    case_timeout: int = Field(default=DEFAULT_TIMEOUT, ge=1, le=3600)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _resolve_case(name: str) -> Path:
    p = Path(name)
    for cand in ([p] if p.is_absolute() else [DEMO_DIR / p, DEMO_DIR / "testcases" / p]):
        if cand.is_file():
            return cand.resolve()
    raise HTTPException(status_code=400, detail=f"用例不存在：{name}")


def _worker(name: str) -> None:
    """一个 worker 就是一条死循环：取任务 → 跑 → 记录 → 判断整个 run 是否跑完。"""
    while True:
        task = QUEUE.get()
        if task is None:                      # 哨兵：优雅退出
            QUEUE.task_done()
            return

        run_id, seq, label, path, timeout = task
        left = None
        try:
            store.mark_running(run_id)        # 只有第一次会真的改（WHERE status='queued'）
            res = runner.run_case(path, DEMO_DIR, timeout=timeout)
            store.add_result(run_id, seq, label, path.name, res)
        except Exception as e:                # worker 绝不能死：单条用例出错只记 error
            store.add_result(run_id, seq, label, path.name,
                             runner.CaseResult(case=label, status=runner.ERROR,
                                               message=f"worker 异常：{e}"))
        finally:
            with _STATE_LOCK:
                left = _REMAINING.get(run_id, 1) - 1
                if left <= 0:
                    _REMAINING.pop(run_id, None)
                else:
                    _REMAINING[run_id] = left
            if left <= 0:
                store.finish_run(run_id)
            QUEUE.task_done()


def start_workers(n: int = MAX_WORKERS) -> None:
    if _WORKERS:
        return
    for i in range(n):
        t = threading.Thread(target=_worker, args=(f"worker-{i}",),
                             name=f"worker-{i}", daemon=True)
        t.start()
        _WORKERS.append(t)

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return TEMPLATES.TemplateResponse(request=request, name="index.html", context={})

@app.get("/health")
def health():
    return {"ok": True, "workers": len(_WORKERS), "queue": QUEUE.qsize(),
            "runs": store.count_runs(), "db": str(store.DB_PATH)}


@app.post("/runs", status_code=202)
def create_run(req: RunReq):
    cases = [(c, _resolve_case(c)) for c in req.cases]     # 先校验，非法直接 400

    if req.request_id:                                     # 幂等：老请求直接回原 run
        old = store.get_run_by_request(req.request_id)
        if old:
            return store.get_run(old["id"]) or old

    run_id = uuid.uuid4().hex[:8]
    store.create_run(run_id, len(cases), req.request_id)
    with _STATE_LOCK:
        _REMAINING[run_id] = len(cases)

    for seq, (label, path) in enumerate(cases, start=1):
        QUEUE.put((run_id, seq, label, path, req.case_timeout))

    return store.get_run(run_id)


@app.get("/runs")
def list_runs(limit: int = 20):
    return store.list_runs(limit)


@app.get("/runs/{run_id}")
def get_run(run_id: str):
    run = store.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run 不存在：{run_id}")
    return run


# ---- 进程启动时：恢复中断状态 + 拉起 worker ----
store.recover_interrupted()
start_workers()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8100)