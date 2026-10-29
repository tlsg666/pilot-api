"""Stage 6 · 质量门禁：跑用例 → 判定 → 用退出码说话。

三条规则，任一不满足就拦截（退出码 1）：
  R1 失败率 ≤ FAIL_RATE_THRESHOLD
  R2 P0 清单里的用例必须全部通过（缺一条也算拦截）
  R3 error 必须为 0 —— 环境不可信时禁止放行（error 和 failed 是两回事）

用法：
    python scripts/gate.py           # 日常门禁（排除 9_* 演示用例）
    python scripts/gate.py --all     # 连演示失败用例一起跑，本地演示红灯
    python scripts/gate.py --json-out gate-result.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEMO_DIR = BASE_DIR / "demo"
CASES_DIR = DEMO_DIR / "testcases"

sys.path.insert(0, str(BASE_DIR))       # 让 scripts/ 里能 import 项目根下的 runner

import runner  # noqa: E402

FAIL_RATE_THRESHOLD = 0.05              # 失败率红线
P0_CASES = ("1_login.yml", "3_order.yml")   # 核心链路，必须全绿
EXCLUDE_PREFIXES = ("9_",)              # 演示用故意失败的用例，不进日常门禁
MAX_ERROR = 0


def collect_cases(include_all: bool = False) -> list[Path]:
    cases = sorted(CASES_DIR.glob("*.yml"))
    if not include_all:
        cases = [c for c in cases if not c.name.startswith(EXCLUDE_PREFIXES)]
    return cases


def judge(pairs: list[tuple[Path, runner.CaseResult]]) -> list[str]:
    """纯函数：只看结果不做 IO。返回不通过的原因（空列表 = 放行）。"""
    total = len(pairs)
    if total == 0:
        return ["没有可执行的用例"]

    failed = sum(1 for _, r in pairs if r.status == runner.FAILED)
    error = sum(1 for _, r in pairs if r.status == runner.ERROR)
    rate = failed / total

    reasons: list[str] = []

    if rate > FAIL_RATE_THRESHOLD:
        reasons.append(f"失败率 {rate:.1%} 超过阈值 {FAIL_RATE_THRESHOLD:.0%}（{failed}/{total}）")

    for p0 in P0_CASES:
        hit = [r for c, r in pairs if c.name == p0]
        if not hit:
            reasons.append(f"P0 用例缺失：{p0}")
        elif hit[0].status != runner.PASSED:
            extra = f" - {hit[0].message[:80]}" if hit[0].message else ""
            reasons.append(f"P0 未通过：{p0}（{hit[0].status}）{extra}")

    if error > MAX_ERROR:
        reasons.append(f"error={error}，环境/工具不可信，禁止放行")

    return reasons


def main() -> int:
    ap = argparse.ArgumentParser(description="接口测试质量门禁")
    ap.add_argument("--all", action="store_true", help="包含 9_* 演示失败用例")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    cases = collect_cases(args.all)
    print(f"[门禁] 用例集 {len(cases)} 条：{', '.join(c.name for c in cases)}")
    if not cases:
        print("[门禁] 没有可跑的用例")
        return 1

    results = runner.run_cases(cases, DEMO_DIR, timeout=args.timeout)
    pairs = list(zip(cases, results))

    print()
    print(f"{'用例':<26}{'状态':<10}{'耗时(ms)':>10}  说明")
    for case, r in pairs:
        print(f"{case.name:<26}{r.status:<10}{r.duration_ms:>10}  {r.message.replace(chr(10), ' ')[:60]}")

    passed = sum(1 for _, r in pairs if r.status == runner.PASSED)
    failed = sum(1 for _, r in pairs if r.status == runner.FAILED)
    error = sum(1 for _, r in pairs if r.status == runner.ERROR)
    reasons = judge(pairs)

    print()
    print(f"[汇总] total={len(pairs)} passed={passed} failed={failed} error={error} "
          f"失败率={(failed / len(pairs)):.1%}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps({
            "ok": not reasons, "total": len(pairs), "passed": passed, "failed": failed,
            "error": error, "fail_rate": failed / len(pairs),
            "threshold": FAIL_RATE_THRESHOLD, "p0": list(P0_CASES), "reasons": reasons,
            "cases": [{"case": c.name, "status": r.status, "duration_ms": r.duration_ms,
                       "message": r.message} for c, r in pairs],
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    if reasons:
        print("\n[拦截] 门禁不通过：")
        for x in reasons:
            print(f"  - {x}")
        return 1

    print("\n[放行] 门禁通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())