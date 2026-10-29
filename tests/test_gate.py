"""Stage 6 验收：门禁判定逻辑（纯函数，不依赖 hrp 与被测服务）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import gate          # noqa: E402
import runner        # noqa: E402

CASES = ROOT / "demo" / "testcases"
P, F, E = runner.PASSED, runner.FAILED, runner.ERROR


def pair(name: str, status: str):
    return (CASES / name, runner.CaseResult(case=name, status=status))


def green_pairs(n: int = 5):
    """P0 两条 + 其余若干，全部通过。"""
    names = ["1_login.yml", "3_order.yml"] + [f"x{i}.yml" for i in range(n - 2)]
    return [pair(nm, P) for nm in names]


def test_all_green_passes():
    assert gate.judge(green_pairs()) == []


def test_fail_rate_blocks():
    pairs = green_pairs(10)
    pairs[9] = pair("x7.yml", F)          # 1/10 = 10% > 5%
    reasons = gate.judge(pairs)
    assert reasons and "失败率" in reasons[0]


def test_p0_failure_blocks():
    pairs = green_pairs(5)
    pairs[0] = pair("1_login.yml", F)
    reasons = gate.judge(pairs)
    assert any("P0 未通过：1_login.yml" in r for r in reasons)


def test_p0_missing_blocks():
    pairs = [p for p in green_pairs(5) if p[0].name != "3_order.yml"]
    reasons = gate.judge(pairs)
    assert any("P0 用例缺失：3_order.yml" in r for r in reasons)


def test_error_blocks_even_if_rate_ok():
    pairs = green_pairs(5)
    pairs[4] = pair("x2.yml", E)
    reasons = gate.judge(pairs)
    assert any("error=" in r for r in reasons)


def test_empty_case_set_blocks():
    assert gate.judge([]) == ["没有可执行的用例"]