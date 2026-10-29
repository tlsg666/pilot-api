"""Stage 7 · flaky 识别 + 规则归因。

两条独立的判断：
  1. flaky_level(history)  —— 这条用例「稳不稳」，只看历史
  2. classify(message, history) —— 这次失败「算谁的」，看失败信息 + 历史
先规则，规则覆盖不了再考虑 LLM；规则的好处是可解释、可回归测试。
"""
from __future__ import annotations

import re

ENV = "env"
PRODUCT = "product"
CASE = "case_invalid"
FLAKY = "flaky"
UNKNOWN = "unknown"

CATEGORY_LABEL = {
    ENV: "环境问题（不要提单）",
    PRODUCT: "产品缺陷",
    CASE: "用例失效（契约变了）",
    FLAKY: "flaky（同断言时好时坏）",
    UNKNOWN: "待人工判断",
}

ENV_PATTERNS = (
    r"connection refused", r"dial tcp", r"no such host", r"timed out", r"timeout",
    r"超时", r"wsarecv", r"connection reset", r"network is unreachable",
    r"name or service not known", r"tls handshake",
)
PRODUCT_PATTERNS = (
    r"\b5\d\d\b", r"internal server error", r"traceback", r"panic:",
)
CASE_PATTERNS = (
    r"\b404\b", r"keyerror", r"does not exist", r"字段不存在", r"not found",
)


def _matched(patterns: tuple[str, ...], text: str) -> str:
    for p in patterns:
        if re.search(p, text, re.I):
            return p
    return ""


def flaky_level(history: list[str], window: int = 5) -> str:
    """history 是同一用例最近若干次的状态，新的在前。"""
    hs = history[:window]
    if len(hs) < 3:
        return "insufficient"
    kinds = set(hs)
    if len(kinds) == 1:
        return "stable" if hs[0] == "passed" else "broken"
    return "flaky"


def classify(message: str, history: list[str]) -> dict:
    """返回 {category, reason}，reason 就是可以贴进报告的依据。"""
    text = message or ""

    p = _matched(ENV_PATTERNS, text)
    if p:
        return {"category": ENV, "reason": f"命中环境特征：{p}"}

    p = _matched(PRODUCT_PATTERNS, text)
    if p:
        return {"category": PRODUCT, "reason": f"命中服务端错误特征：{p}"}

    p = _matched(CASE_PATTERNS, text)
    if p:
        return {"category": CASE, "reason": f"命中契约变更特征：{p}"}

    low = text.lower()
    if "断言" in text or "assert" in low:
        has_pass = any(s == "passed" for s in history)
        has_fail = any(s != "passed" for s in history)
        if has_pass and has_fail:
            return {"category": FLAKY, "reason": "断言失败，但最近历史里既有通过也有失败"}
        return {"category": UNKNOWN, "reason": "断言失败且历史无通过记录，需人工确认"}

    return {"category": UNKNOWN, "reason": "未命中任何规则"}


def summarize_cases(rows: list[dict], window: int = 5) -> list[dict]:
    """rows：按 id 倒序的结果行（最新在前）。返回每条用例的健康度摘要。"""
    by_case: dict[str, list[dict]] = {}
    for r in rows:
        by_case.setdefault(r["case_name"], []).append(r)

    out: list[dict] = []
    for case, items in by_case.items():
        history = [i["status"] for i in items[:window]]
        last = items[0]
        # 归因看「最近一次失败」：flaky 用例最新一次可能是通过的，
        # 但它的失败原因仍然需要展示出来
        last_fail = next((i for i in items[:window] if i["status"] != "passed"), None)
        diag = ({"category": "", "reason": ""} if last_fail is None
                else classify(last_fail["message"], history))
        out.append({
            "case": case,
            "history": history,
            "runs": len(items),
            "flaky": flaky_level(history, window),
            "last_status": last["status"],
            "last_message": (last_fail or last)["message"],
            "category": diag["category"],
            "category_label": CATEGORY_LABEL.get(diag["category"], ""),
            "reason": diag["reason"],
            "last_at": last["created_at"],
        })

    order = {"flaky": 0, "broken": 1, "insufficient": 2, "stable": 3}
    return sorted(out, key=lambda x: (order.get(x["flaky"], 9), x["case"]))