# pilot-api · 接口测试平台（简要说明）

> 把「一个人对着命令行跑 YAML」变成「一个服务：提交任务 → 排队执行 → 出报告 → 拦合并」。
> HttpRunner（`hrp`）只当命令行工具用：不读 Go 源码、不 fork、不改内核，所有自有代码在 Python。

---

## 1. 它解决什么问题

| 没有平台 | 有这个平台 |
|---|---|
| 本地敲 `hrp run`，结果在终端里滚过去 | 提交即返回 `run_id`，进度可轮询 |
| 失败只剩一个 panic 栈 | 失败项可展开看用例 / exit code / 归因依据 |
| 昨天挂过今天过，没人知道 | 最近 5 次「有通过有失败」→ 自动标 `flaky` |
| 合并靠自觉 | 失败率 > 5% 或 P0 用例失败 → CI 变红，拦住合并 |

被测对象是本仓库的 `main.py`（一个带登录 / 列表 / 下单的示例接口服务），用例在 `demo/testcases/`。

---

## 2. 架构

```
浏览器（Jinja2 单页，800ms 轮询）
        │
        ▼
server.py  FastAPI :8100
        ├── queue.Queue ──► 4 × worker 线程
        │                       │
        │                       ▼
        │                runner.py ── subprocess ──► hrp run case.yml -s
        │                       │                        │
        │                cwd = demo/runs/<uuid>    results/<ts>/summary.json
        ├── store.py       SQLite（runs / results）
        └── analysis.py    flaky 识别 + 规则归因
```

---

## 3. 目录

| 路径 | 作用 |
|---|---|
| `main.py` | 被测服务（FastAPI，:8000） |
| `runner.py` | hrp 适配器：三态结果、报告隔离、stdout 兜底 |
| `store.py` | SQLite 持久化（`runs` 汇总 + `results` 明细） |
| `server.py` | 队列 + 4 worker + 超时 + 中断恢复 + HTTP 接口 |
| `analysis.py` | flaky 等级判定 + 失败归因（规则，不用 LLM） |
| `scripts/gate.py` | 质量门禁（阈值 + P0 清单 + error 拦截） |
| `templates/index.html` | 看板：运行列表 / 进度条 / 失败详情 / 用例健康度 |
| `demo/` | hrp 工程（`.env`、`proj.json`、`testcases`） |
| `tests/` | 31 个测试 |
| `.github/workflows/` | CI：装 hrp → 起服务 → 单测 → 门禁 → 上传产物 |

---

## 4. 怎么跑

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

python main.py            # 终端 1：被测服务 :8000
python server.py          # 终端 2：平台 :8100
start http://127.0.0.1:8100/

# 提交一轮
$body = '{"cases":["1_login.yml","2_list.yml","3_order.yml","4_login_error.yml","5_list_boundary.yml"]}'
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8100/runs -ContentType application/json -Body $body

# 质量门禁（CI 用同一条命令）
python scripts/gate.py
```

---

## 5. 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/runs` | 提交任务，202 + `run_id`（支持 `request_id` 幂等） |
| GET | `/runs` | 运行列表 |
| GET | `/runs/{id}` | 进度 + 每条用例结果 |
| GET | `/cases` | 用例健康度（flaky 等级 + 归因） |
| GET | `/health` | worker 数 / 队列积压 / DB 路径 |

---

## 6. 关键设计

| 决策 | 原因 |
|---|---|
| 报告优先、stdout 兜底 | hrp v4.3.6 断言失败会 panic 且不写报告（上游 #1724），只有 exit 0/1 才有 `summary.json` |
| 每次运行独占 cwd | hrp 报告落在 `<cwd>/results/<秒级时间戳>`，并发会撞车；报告归属必须由调用方决定 |
| 队列粒度 = 单条用例 | 一个 20 条的 run 串行要 20×t，拆到 case 级 4 并发约为 1/4 |
| 端点用 `def` 而非 `async def` | `subprocess.run` 阻塞，写进 `async def` 会卡死事件循环 |
| 三态 `passed / failed / error` | 「挂了」和「错了」要分开：`error` 时结果不可信，门禁单独拦 |
| 门禁 = 阈值 + P0 清单 | 全绿门禁会被一条 flaky 拖死，最终没人看 |
| flaky = 有通过且有失败 | 全失败是 `broken`，不是 flaky |

---

## 7. 演示路径

1. 打开 `http://127.0.0.1:8100/`，提交 5 条用例 → 进度条实时走完，显示 `5/5 passed`；
2. 提交 `9_always_fail.yml` → 该行变红，展开能看到失败依据；
3. 停掉 `server.py` 再启动 → `GET /runs` 历史仍在（SQLite）；跑到一半杀掉 → 该 run 标记 `interrupted`；
4. GitHub 上故意改坏 P0 用例开 PR → CI 红 ❌，门禁日志指出 `P0 未通过：1_login.yml`。

---

## 8. 实测踩过的坑

1. `hrp run` **没有** `--report-dir`；报告靠 `-s`，落在 `<cwd>/results/<ts>/summary.json`；
2. `.env` 的 `base_url` **优先级高于** yaml 的 `config.base_url` —— 切环境就换 `.env`；
3. 无 query 时 hrp 会把路径补成 `/path/`，且 **POST 的 307 不跟随** → FastAPI 每个路由要注册带斜杠和不带斜杠两份；
4. 断言失败 = panic + exit 2 + 无报告（v4.3.6 已是 2023 年之后的最新 release）；
5. `debugtalk.py` 存在会触发 funppy 依赖检查，网络不通时直接 `exit 9`。

---

## 9. 测试与 CI

```powershell
python -m pytest tests -q      # 31 passed
```

CI：`.github/workflows/api-test.yml`，装 hrp → 起被测服务 → 31 个单测 → `gate.py` → 上传 `gate-result.json`，全流程约 22 秒。
