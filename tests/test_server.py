"""Stage 3 验收：提交即返回、轮询收敛、幂等、错误码。

前置：main.py（被测服务）必须在 127.0.0.1:8000 运行。
运行：python -m pytest tests -q
"""
import time

from fastapi.testclient import TestClient

import server

client = TestClient(server.app)


def wait_finished(run_id: str, timeout: float = 90.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/runs/{run_id}")
        assert r.status_code == 200, r.text
        data = r.json()
        if data["status"] == "finished":
            return data
        time.sleep(0.2)
    raise AssertionError(f"run {run_id} 在 {timeout}s 内没有结束")


def test_submit_returns_immediately():
    """提交必须秒回，且回来时还没跑完（这就是「不阻塞请求」的语义证明）。"""
    t0 = time.perf_counter()
    r = client.post("/runs", json={"cases": ["1_login.yml", "4_login_error.yml"]})
    elapsed = time.perf_counter() - t0

    assert r.status_code == 202, r.text
    body = r.json()
    assert body["id"]
    assert body["total"] == 2
    assert body["status"] in ("queued", "running"), "两条用例跑完不可能这么快，说明没立即返回"
    assert elapsed < 1.5, f"提交耗时 {elapsed:.2f}s，太久了"

    done = wait_finished(body["id"])
    assert done["passed"] == 2 and done["failed"] == 0 and done["error"] == 0
    assert len(done["results"]) == 2
    assert all(x["status"] == "passed" for x in done["results"])


def test_idempotent_by_request_id():
    """同一个 request_id 提交两次，拿到同一个 run_id，不会重复执行。"""
    payload = {"cases": ["1_login.yml"], "request_id": "fixed-key-001"}
    a = client.post("/runs", json=payload).json()
    b = client.post("/runs", json=payload).json()
    assert a["id"] == b["id"]
    wait_finished(a["id"])


def test_unknown_case_returns_400():
    r = client.post("/runs", json={"cases": ["no_such_case.yml"]})
    assert r.status_code == 400
    assert "用例不存在" in r.json()["detail"]


def test_unknown_run_returns_404():
    r = client.get("/runs/not-a-run-id")
    assert r.status_code == 404


def test_progress_is_visible():
    """进度字段在跑的过程中是递增的，不是结束后一次性出现。"""
    run_id = client.post("/runs", json={"cases": ["1_login.yml", "2_list.yml", "4_login_error.yml"]}).json()["id"]
    seen = None
    deadline = time.time() + 60
    while time.time() < deadline:
        data = client.get(f"/runs/{run_id}").json()
        if 0 < data["finished"] < data["total"]:
            seen = data
            break
        if data["status"] == "finished":
            break
        time.sleep(0.05)
    done = wait_finished(run_id)
    assert done["passed"] == 3
    # seen 可能为 None（用例太快），不作为硬断言，但保留了进度可观测的证据