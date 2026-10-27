"""Stage 2 验收测试：4 种状态 + 报告隔离。

前置：main.py 必须已在 127.0.0.1:8000 运行。
运行：在项目根执行  python -m pytest tests -q   （必须是 python -m pytest）
"""
import textwrap
from pathlib import Path

import pytest

import runner

DEMO = Path(__file__).resolve().parent.parent / "demo"
TOKEN = "this_is_a_fake_token_123456789"
BASE = "http://127.0.0.1:8000"


def make_case(tmp_path: Path, expect_status: int, url: str = f"{BASE}/api/list") -> Path:
    """造一条最小用例：请求列表接口，断言 status_code。"""
    path = tmp_path / "case.yml"
    path.write_text(textwrap.dedent(f"""\
        config:
          name: "stage2-case"
        teststeps:
          - name: "查询列表"
            request:
              method: GET
              url: "{url}"
              headers:
                token: "{TOKEN}"
            validate:
              - eq: [status_code, {expect_status}]
        """), encoding="utf-8")
    return path


def test_passed(tmp_path):
    """断言通过 → exit 0 + 有报告 + 步骤全绿。"""
    r = runner.run_case(make_case(tmp_path, 200), DEMO)
    assert r.status == runner.PASSED, r.message + "\n" + r.stdout_tail
    assert r.exit_code == 0
    assert r.report_dir, "通过的用例应当有报告"
    assert r.steps and r.steps[0].success
    assert r.duration_ms > 0


def test_assert_failed_has_no_report(tmp_path):
    """断言失败 → exit 2 + 没有报告（上游 panic）。"""
    r = runner.run_case(make_case(tmp_path, 201), DEMO)
    assert r.status == runner.FAILED
    assert r.exit_code == 2
    assert r.report_dir == "", "断言失败不会有报告，这是上游缺陷决定的"
    assert "断言失败" in r.message


def test_request_failed_has_report(tmp_path):
    """连不上服务 → exit 1 + 反而有报告（失败原因在 attachments 里）。"""
    r = runner.run_case(make_case(tmp_path, 200, url="http://127.0.0.1:9/api/list"), DEMO)
    assert r.status == runner.FAILED
    assert r.exit_code == 1
    assert r.report_dir
    assert "do request failed" in r.message or "refused" in r.message


def test_hrp_missing(tmp_path, monkeypatch):
    """找不到 hrp → error，且不抛异常。"""
    monkeypatch.setattr(runner, "HRP", "hrp-does-not-exist-xyz")
    monkeypatch.setattr(runner, "HRP_FALLBACK", Path("no-such-hrp.exe"))
    r = runner.run_case(make_case(tmp_path, 200), DEMO)
    assert r.status == runner.ERROR
    assert "找不到 hrp" in r.message


def test_runs_are_isolated(tmp_path):
    """两次运行各有自己的运行目录与报告目录，不会互相覆盖。"""
    a = runner.run_case(make_case(tmp_path, 200), DEMO)
    b = runner.run_case(make_case(tmp_path, 200), DEMO)
    assert a.run_dir != b.run_dir
    assert a.report_dir != b.report_dir