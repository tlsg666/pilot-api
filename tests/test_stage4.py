"""Stage 4 验收：持久化、中断恢复、用例级并发、worker 容错。

前置：main.py（被测服务）在 127.0.0.1:8000 运行。
运行：python -m pytest tests -q
"""
import threading
import time

from fastapi.testclient import TestClient

import runner
import server
import store

client = TestClient(server.app)


def wait_finished(run_id: str, timeout: float = 120.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/runs/{run_id}").json()
        if data["status"] in ("finished", "interrupted"):
            return data
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} 没有结束")


def test_result_survives_db_reopen():
    """跑完 → 断开连接（模拟进程重启）→ 数据还在。"""
    run_id = client.post("/runs", json={"cases": ["1_login.yml"]}).json()["id"]
    done = wait_finished(run_id)
    assert done["status"] == "finished" and done["passed"] == 1

    store.close()                                  # 断开，模拟重启
    again = store.get_run(run_id)                  # 重新打开数据库文件
    assert again is not None
    assert again["status"] == "finished"
    assert again["results"][0]["case_name"] == "1_login.yml"


def test_interrupted_recovery():
    """上次没跑完的 run 要被标成 interrupted，而不是永远 running。"""
    rid = "interrupted-case"
    store.create_run(rid, 3)
    store.mark_running(rid)
    assert store.get_run(rid)["status"] == "running"

    assert store.recover_interrupted() >= 1
    assert store.get_run(rid)["status"] == "interrupted"


def test_case_level_parallelism(monkeypatch):
    """关键：一个 run 里的 8 条用例要被 4 个 worker 同时吃，而不是排队串行。"""
    active = {"now": 0, "max": 0}
    lock = threading.Lock()

    def fake_run_case(case_path, project_dir, timeout=120, run_dir=None):
        with lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        time.sleep(0.4)
        with lock:
            active["now"] -= 1
        return runner.CaseResult(case=str(case_path), status=runner.PASSED, duration_ms=400)

    monkeypatch.setattr(server.runner, "run_case", fake_run_case)

    t0 = time.perf_counter()
    run_id = client.post("/runs", json={"cases": ["1_login.yml"] * 8}).json()["id"]
    assert (time.perf_counter() - t0) < 1.0, "提交不该等用例跑完"

    done = wait_finished(run_id)
    assert done["finished"] == 8 and done["passed"] == 8
    assert active["max"] >= 3, f"最大并发只有 {active['max']}，说明 worker 没用上"


def test_worker_crash_is_contained(monkeypatch):
    """单条用例跑到一半抛异常，只能记成 error，不能让 worker 或平台挂掉。"""
    def boom(*args, **kwargs):
        raise RuntimeError("模拟崩溃")

    monkeypatch.setattr(server.runner, "run_case", boom)
    run_id = client.post("/runs", json={"cases": ["1_login.yml"]}).json()["id"]
    done = wait_finished(run_id)

    assert done["status"] == "finished"
    assert done["error"] == 1 and done["passed"] == 0
    assert "worker 异常" in done["results"][0]["message"]


def test_queue_is_visible(monkeypatch):
    """提交量大于并发能力时，队列里应该有排队，状态是 queued/running 而不是 finished。"""
    def slow(*args, **kwargs):
        time.sleep(0.3)
        return runner.CaseResult(case="slow", status=runner.PASSED, duration_ms=300)

    monkeypatch.setattr(server.runner, "run_case", slow)
    run_id = client.post("/runs", json={"cases": ["1_login.yml"] * 6}).json()["id"]
    body = client.get(f"/runs/{run_id}").json()

    assert body["status"] in ("queued", "running")
    assert body["finished"] < 6
    wait_finished(run_id)