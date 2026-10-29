"""Stage 7 验收：flaky 判定、归因规则、以及从数据库到接口的闭环。"""
import time

from fastapi.testclient import TestClient

import analysis
import runner
import server

client = TestClient(server.app)


# ---------- 纯逻辑 ----------

def test_flaky_level():
    assert analysis.flaky_level(["passed", "passed"]) == "insufficient"
    assert analysis.flaky_level(["passed"]) == "insufficient"
    assert analysis.flaky_level(["passed"] * 5) == "stable"
    assert analysis.flaky_level(["failed"] * 5) == "broken"
    assert analysis.flaky_level(["passed", "failed", "passed", "passed", "failed"]) == "flaky"


def test_classify_env():
    r = analysis.classify("do request failed: dial tcp 127.0.0.1:9: connectex: No connection could be made", [])
    assert r["category"] == analysis.ENV
    assert "环境特征" in r["reason"]


def test_classify_product():
    r = analysis.classify("do request failed: HTTP 502 Bad Gateway", [])
    assert r["category"] == analysis.PRODUCT


def test_classify_case_invalid():
    r = analysis.classify("extract failed: body.data.token KeyError", [])
    assert r["category"] == analysis.CASE


def test_classify_flaky_needs_mixed_history():
    msg = "断言失败（hrp panic，上游不写报告） last_step=正常登录"
    assert analysis.classify(msg, ["failed", "passed", "failed"])["category"] == analysis.FLAKY
    assert analysis.classify(msg, ["failed", "failed", "failed"])["category"] == analysis.UNKNOWN


def test_summarize_sorts_flaky_first():
    rows = [
        {"case_name": "b.yml", "status": "failed", "message": "dial tcp 1.2.3.4: connection refused",
         "created_at": "t3"},
        {"case_name": "b.yml", "status": "passed", "message": "", "created_at": "t2"},
        {"case_name": "b.yml", "status": "failed", "message": "dial tcp 1.2.3.4: connection refused",
         "created_at": "t1"},
        {"case_name": "a.yml", "status": "passed", "message": "", "created_at": "t0"},
    ]
    out = analysis.summarize_cases(rows)
    assert out[0]["case"] == "b.yml"
    assert out[0]["flaky"] == "flaky"
    assert out[0]["category"] == analysis.ENV


# ---------- 接口闭环 ----------

def _wait(run_id: str, timeout: float = 90.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if client.get(f"/runs/{run_id}").json()["status"] == "finished":
            return
        time.sleep(0.1)
    raise AssertionError("run 没结束")


def test_case_marked_flaky_via_api(monkeypatch):
    """同一条用例交替通过/失败 → /cases 里应判成 flaky。"""
    state = {"n": 0}

    def fake_run_case(case_path, project_dir, timeout=120, run_dir=None):
        state["n"] += 1
        ok = state["n"] % 2 == 1
        return runner.CaseResult(
            case=str(case_path),
            status=runner.PASSED if ok else runner.FAILED,
            message="" if ok else "断言失败（hrp panic，上游不写报告） last_step=演示",
        )

    # 关键：连"用例是否存在"的校验一起绕开，否则 400，拿不到 run_id
    monkeypatch.setattr(server, "_resolve_case", lambda name: (server.DEMO_DIR / name).resolve())
    monkeypatch.setattr(server.runner, "run_case", fake_run_case)

    for _ in range(3):
        resp = client.post("/runs", json={"cases": ["flaky_demo.yml"]})
        assert resp.status_code == 202, resp.text
        _wait(resp.json()["id"])

    rows = {c["case"]: c for c in client.get("/cases").json()}
    assert "flaky_demo.yml" in rows
    assert rows["flaky_demo.yml"]["flaky"] == "flaky"
    assert rows["flaky_demo.yml"]["category"] == analysis.FLAKY