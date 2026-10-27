"""Stage 2 · 执行适配器：把 hrp CLI 变成 Python 对象（纯黑盒，只消费 CLI 与产物）。

四条设计约束，全部来自实测：
1. hrp v4.3.6 没有 --report-dir；结构化报告靠 -s，落在 <cwd>/results/<秒级时间戳>/summary.json
2. .env 由工程根（proj.json 所在目录）向上查找决定，与 cwd 无关
3. 断言失败时 hrp 会 panic（上游 issue #1724）：退出码 2，且不写任何报告
4. 报告是权威、stdout 是兜底；每次运行独占一个 cwd，报告归属由调用方决定
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

HRP = "hrp"                                                     # 优先走 PATH
HRP_FALLBACK = Path(r"D:\tool\hrp-v4.3.6-windows-amd64\hrp.exe")  # 再退到固定路径

PASSED, FAILED, ERROR = "passed", "failed", "error"

EXIT_ASSERT_FAILED = 2        # 断言失败 → panic，无报告
EXIT_ENV_BROKEN = {9, 18}     # 9: 插件/venv 准备失败   18: plugin 执行失败


@dataclass
class StepResult:
    name: str = ""
    success: bool = False
    elapsed_ms: int = 0
    error: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "success": self.success,
                "elapsed_ms": self.elapsed_ms, "error": self.error}


@dataclass
class CaseResult:
    case: str
    status: str
    duration_ms: int = 0
    exit_code: int = -1
    message: str = ""
    steps: list[StepResult] = field(default_factory=list)
    report_dir: str = ""
    run_dir: str = ""
    stdout_tail: str = ""

    @property
    def ok(self) -> bool:
        return self.status == PASSED

    def as_dict(self) -> dict:
        return {"case": self.case, "status": self.status, "duration_ms": self.duration_ms,
                "exit_code": self.exit_code, "message": self.message,
                "steps": [s.as_dict() for s in self.steps],
                "report_dir": self.report_dir, "run_dir": self.run_dir}


def _resolve_hrp() -> str | None:
    found = shutil.which(HRP)
    if found:
        return found
    return str(HRP_FALLBACK) if HRP_FALLBACK.is_file() else None


def _find_report(run_dir: Path) -> Path | None:
    """run_dir 由本次运行独占，所以这里不会捞到别人的报告（不会被并发覆盖）。"""
    root = run_dir / "results"
    if not root.is_dir():
        return None
    dirs = sorted((d for d in root.iterdir() if (d / "summary.json").is_file()),
                  key=lambda d: d.name)          # 时间戳目录按名字排序即时间序
    return dirs[-1] if dirs else None


def _tail(text: str, lines: int = 15) -> str:
    return "\n".join(text.splitlines()[-lines:])


def _last_step_name(out: str) -> str:
    hits = re.findall(r"run step start step=(.+?) type=", out)
    return hits[-1].strip('"') if hits else ""


def run_case(case_path: str | Path, project_dir: str | Path,
             timeout: int = 120, run_dir: str | Path | None = None) -> CaseResult:
    """跑一条 yml，返回结构化结果。

    project_dir：hrp 工程根（有 .env / proj.json 的那层），决定变量与执行环境
    run_dir    ：本次运行的独占工作目录，决定报告落在哪；不传则自动生成
    timeout    ：单条用例超时（秒），会同时传给 hrp 与外层 subprocess
    """
    case_path = Path(case_path).resolve()      # cwd 会变，路径必须先绝对化
    project_dir = Path(project_dir).resolve()
    run_dir = (Path(run_dir) if run_dir else project_dir / "runs" / uuid.uuid4().hex[:8]).resolve()

    hrp = _resolve_hrp()
    if hrp is None:
        return CaseResult(case=case_path.stem, status=ERROR, run_dir=str(run_dir),
                          message=f"找不到 hrp：PATH 里没有，{HRP_FALLBACK} 也不存在")
    if not case_path.is_file():
        return CaseResult(case=case_path.stem, status=ERROR, run_dir=str(run_dir),
                          message=f"用例文件不存在：{case_path}")

    run_dir.mkdir(parents=True, exist_ok=True)
    cmd = [hrp, "run", str(case_path), "-s", "--case-timeout", str(timeout)]

    t0 = time.perf_counter()
    try:
        p = subprocess.run(
            cmd, cwd=str(run_dir), capture_output=True, text=True,
            encoding="utf-8", errors="replace",   # Windows 控制台是 GBK，必须显式指定
            timeout=timeout + 30,                 # 外层兜底，必须比 --case-timeout 大
        )
    except subprocess.TimeoutExpired:
        return CaseResult(case=case_path.stem, status=ERROR, duration_ms=timeout * 1000,
                          run_dir=str(run_dir),
                          message=f"hrp 进程整体超时（>{timeout + 30}s），按 error 处理")
    except OSError as e:
        return CaseResult(case=case_path.stem, status=ERROR, run_dir=str(run_dir),
                          message=f"启动 hrp 失败：{e}")

    elapsed = int((time.perf_counter() - t0) * 1000)
    out, code = f"{p.stdout or ''}\n{p.stderr or ''}", p.returncode

    report = _find_report(run_dir)
    if report is not None:                        # exit 0 / 1 的路径：有报告，报告说了算
        return _from_summary(report, case_path, code, elapsed, out, run_dir)

    step = _last_step_name(out)                   # 无报告的路径
    if code == EXIT_ASSERT_FAILED:
        return CaseResult(case=case_path.stem, status=FAILED, duration_ms=elapsed,
                          exit_code=code, run_dir=str(run_dir), stdout_tail=_tail(out),
                          message=f"断言失败（hrp panic，上游不写报告） last_step={step or '未知'}",
                          steps=[StepResult(step or "未知", False, 0, "assert failed")])
    if code in EXIT_ENV_BROKEN:
        return CaseResult(case=case_path.stem, status=ERROR, duration_ms=elapsed,
                          exit_code=code, run_dir=str(run_dir), stdout_tail=_tail(out),
                          message=f"环境/插件问题 exit={code}：检查 debugtalk.py 与 funppy")
    if code == 0:
        return CaseResult(case=case_path.stem, status=ERROR, duration_ms=elapsed,
                          exit_code=code, run_dir=str(run_dir), stdout_tail=_tail(out),
                          message="exit=0 但没有报告：确认带了 -s，且 cwd 可写")
    return CaseResult(case=case_path.stem, status=ERROR, duration_ms=elapsed,
                      exit_code=code, run_dir=str(run_dir), stdout_tail=_tail(out),
                      message=f"执行异常 exit={code} last_step={step or '未知'}")


def _from_summary(report: Path, case_path: Path, code: int,
                  elapsed: int, out: str, run_dir: Path) -> CaseResult:
    data = json.loads((report / "summary.json").read_text(encoding="utf-8"))
    detail = (data.get("details") or [{}])[0]
    steps = [StepResult(r.get("name", ""), bool(r.get("success")),
                        int(r.get("elapsed_ms") or 0), r.get("attachments") or "")
             for r in detail.get("records", [])]
    duration = int(float((detail.get("time") or {}).get("duration") or 0) * 1000) or elapsed
    failed = next((s for s in steps if not s.success and s.error), None)
    return CaseResult(
        case=detail.get("name") or case_path.stem,
        status=PASSED if data.get("success") else FAILED,
        duration_ms=duration, exit_code=code,
        message=failed.error if failed else "",
        steps=steps, report_dir=str(report), run_dir=str(run_dir),
        stdout_tail=_tail(out))


def run_cases(cases, project_dir, timeout: int = 120) -> list[CaseResult]:
    """顺序跑多条（Stage 3 的调度器会替换掉这个朴素实现）。"""
    return [run_case(c, project_dir, timeout) for c in cases]