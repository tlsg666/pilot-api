"""Stage 3 · 服务化：提交即返回 run_id，轮询看进度。

三态状态机：queued → running → finished（Stage 4 会加 interrupted）
存储先用内存（Stage 4 换 SQLite），但读写一律加锁。
"""
from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Literal

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

import runner

BASE_DIR = Path(__file__).resolve().parent
DEMO_DIR = BASE_DIR / "demo"          # hrp 工程根：.env / proj.json / testcases 都在这
MAX_WORKERS = 4                       # 全局并发上限：同时最多 4 个 hrp 进程

app = FastAPI(title="接口测试平台", version="0.3")


class RunReq(BaseModel):
    cases: list[str] = Field(..., min_length=1, description="用例文件名，如 1_login.yml")
    request_id: str | None = Field(default=None, description="客户端幂等键，重试不会重复执行")


class RunState(BaseModel):
    id: str
    status: Literal["queued", "running", "finished"]
    total: int
    finished: int = 0
    passed: int = 0
    failed: int = 0
    error: int = 0
    current: str | None = None
    started_at: str = ""
    finished_at: str | None = None
    results: list[dict] = Field(default_factory=list)


RUNS: dict[str, RunState] = {}         # run_id -> 状态
CLIENT_KEYS: dict[str, str] = {}       # request_id -> run_id（幂等）
LOCK = threading.Lock()                # 多线程写同一份字典，必须加锁
EXECUTOR = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="hrp-run")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _resolve_case(name: str) -> Path:
    """允许传 '1_login.yml' 或 'testcases/1_login.yml'，在 demo 下找得到就行。"""
    p = Path(name)
    for cand in ([p] if p.is_absolute() else [DEMO_DIR / p, DEMO_DIR / "testcases" / p]):
        if cand.is_file():
            return cand.resolve()
    raise HTTPException(status_code=400, detail=f"用例不存在：{name}")


def _execute(run_id: str, cases: list[Path]) -> None:
    """在 worker 线程里顺序跑完这个 run 的所有用例。"""
    with LOCK:
        RUNS[run_id].status = "running"
        RUNS[run_id].started_at = _now()

    for i, case in enumerate(cases, start=1):
        with LOCK:
            RUNS[run_id].current = case.name

        res = runner.run_case(case, DEMO_DIR)      # 阻塞调用，跑在线程里没问题

        with LOCK:
            st = RUNS[run_id]
            st.results.append(res.as_dict())
            st.finished = i
            if res.status == runner.PASSED:
                st.passed += 1
            elif res.status == runner.FAILED:
                st.failed += 1
            else:
                st.error += 1

    with LOCK:
        st = RUNS[run_id]
        st.status = "finished"
        st.finished_at = _now()
        st.current = None


@app.get("/health")
def health():
    return {"ok": True, "runs": len(RUNS)}


@app.post("/runs", response_model=RunState, status_code=202)
def create_run(req: RunReq):
    """提交任务：立刻返回 202 + run_id，不等用例跑完。"""
    cases = [_resolve_case(c) for c in req.cases]      # 先校验，非法直接 400

    with LOCK:
        if req.request_id:
            old_id = CLIENT_KEYS.get(req.request_id)
            if old_id and old_id in RUNS:
                return RUNS[old_id]                     # 幂等：同一个 request_id 不重复跑
        run_id = uuid.uuid4().hex[:8]
        RUNS[run_id] = RunState(id=run_id, status="queued", total=len(cases))
        if req.request_id:
            CLIENT_KEYS[req.request_id] = run_id

    EXECUTOR.submit(_execute, run_id, cases)            # 丢进线程池，立刻返回
    return RUNS[run_id]


@app.get("/runs")
def list_runs(limit: int = 20):
    with LOCK:
        items = sorted(RUNS.values(), key=lambda r: (r.started_at or "", r.id), reverse=True)
    return [r.model_dump() for r in items[:limit]]


@app.get("/runs/{run_id}")
def get_run(run_id: str):
    with LOCK:
        st = RUNS.get(run_id)
    if st is None:
        raise HTTPException(status_code=404, detail=f"run 不存在：{run_id}")
    return st


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8100)