"""Stage 5 验收：首页能渲染，且不影响已有的 JSON 接口。"""
from fastapi.testclient import TestClient

import server

client = TestClient(server.app)


def test_index_renders():
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "运行列表" in r.text
    assert 'id="run-rows"' in r.text and 'id="result-rows"' in r.text


def test_index_polls_json_api():
    """页面靠 fetch 轮询已有的 JSON 接口，后端不需要为页面开专线。"""
    assert "/runs?limit=" in client.get("/").text


def test_root_does_not_shadow_runs_api():
    assert isinstance(client.get("/runs").json(), list)