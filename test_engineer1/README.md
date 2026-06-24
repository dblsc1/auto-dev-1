# test_engineer1 — E2E 集成测试工程师

## 定位

`test_engineer1` 是独立于 `programmer1` 的测试专员模块，负责：
- 读懂 backend + frontend 的真实代码
- 读取共享 API 契约和每个模块自己的 execution_contract 并做硬检查
- 通过 Python Playwright 执行 E2E
- 起服务 → 浏览器测试 → kill 进程 → 报告

```
programmer1/   ← 写代码
test_engineer1/ ← 端对端测试，看代码改的对不对
```

---

## 子图结构

```
START
  │
  ▼
plan_tests   ← Codex（level 5）
  │            ① 先执行 api_contract 硬闸
  │            ② 读 backend + frontend 源码 + shared state + 各模块 execution_contract
  │            输出结构化测试计划 JSON
  ▼
run_mcp_tests ← Python Playwright
  │            ① Python 启 uvicorn 和同源静态代理
  │            ② 按 Codex 生成的 JSON 测试计划操作浏览器
  │            ③ Python finally 停止两个服务
  │            ④ 返回 pass/fail + phase/failures/artifacts
  ▼
END
```

---

## 文件结构

```
test_engineer1/
  __init__.py        对外接口
  config.py          TestEngineerConfig（旋钮面板）
  state.py           TEState TypedDict
  standard.py        E2ETestStandard（required/extra，人定铁律）
  prompts.py         TESTER_PLANNER_* / TESTER_WORKER_* 提示词
  graph.py           build_test_engineer() 工厂
  nodes/
    plan_tests.py    Codex 节点
    run_mcp_tests.py 服务生命周期 + Python Playwright
  proxy_server.py    测试专用静态服务 + declared API 同源代理
  README.md          本文件
```

外部配置（改这里，不动内部）：
```
test_standards_cfg.py   E2E_HUMAN_STANDARD / E2E_PLANNER_STANDARD
```

---

## 测试标准（E2ETestStandard）

两个变量在 `test_standards_cfg.py` 里：

| 变量 | 说明 | 谁能改 |
|------|------|--------|
| `E2E_HUMAN_STANDARD` | 铁律（如 `playwright` / `api_contract` 必须过） | 只有人 |
| `E2E_PLANNER_STANDARD` | 追加项 | planner 可加，不可删铁律 |

可追加的 checks：`playwright` / `api_contract` / `smoke`

`api_contract` 不再是单一静态字符串检查：它会读取
`~/gg-workspace/backend/contracts/execution_contract.json`、
`~/gg-workspace/frontend/contracts/execution_contract.json` 和
`~/gg-workspace/contracts/backend_api.json`。空契约、未声明的 frontend fetch、或缺失的模块契约均为失败。

Consumer-driven contract 额外读取
`~/gg-workspace/contracts/consumers/*.json`。这些文件由 frontend 等 consumer 模块的
`execution_contract.consumes` 导出，例如 `frontend__backend.json`。`api_contract`
会验证每个 consumer pact 的 `(provider, method, path)` 都存在于 provider 的
`published_api` 中；provider 不满足 pact 时，失败归因给 provider。

`plan_tests` 产出的 JSON 不会直接进入 Playwright。`plan_schema.py` 会先机器校验：
action 名称、必填字段、`expect_request` 的顺序、`route_intercept` 是否声明策略、
以及 loading 断言是否有延迟 mock。schema 不通过时停在 `test_plan` 阶段，不打回业务模块。

## 运行产物与终止

- 契约检查日志：`~/gg-workspace/tester/contract_check.log`
- 服务启动日志：`~/gg-workspace/tester/service_startup.log`
- 浏览器执行日志：`~/gg-workspace/tester/playwright_mcp.log`
- `nodes/run_mcp_tests.py`：先通过契约硬闸，再由 Python 启 FastAPI 和
  `proxy_server.py`。代理只转发 `available_api` 声明的路径，保证浏览器相对路径请求同源。
  Python Playwright 在同一进程内执行 JSON 测试计划，并在 `finally` 中回收服务。
- 测试规划遇到额度/会话限制时，结果标记 `requires_human=true`，顶层 arbiter 立即停止，不把它误报为产品测试失败。

---

## LLM 通道

| 节点 | Level | Provider |
|------|-------|----------|
| plan_tests  | `TEST_PLANNER_LEVEL` | `programmer1/config.py` 统一配置 |
| run_mcp_tests | — | Python Playwright（无 LLM 调用） |

通过 `TestEngineerConfig.planner_level` 或 `GG_TEST_PLANNER_LEVEL` 修改测试计划模型；浏览器执行器不调用 LLM。

---

## 快速接入 graph.py

```python
from test_engineer1 import build_test_engineer, TestEngineerConfig
from test_standards_cfg import E2E_HUMAN_STANDARD, E2E_PLANNER_STANDARD

te_cfg = TestEngineerConfig(
    backend_workspace=BACKEND_CONFIG.workspace,
    frontend_workspace=FRONTEND_CONFIG.workspace,
    real=real,
    human_standard=E2E_HUMAN_STANDARD,
    planner_standard=E2E_PLANNER_STANDARD,
)
te_app = build_test_engineer(te_cfg)
```

---

## 与 _deprecated_lite_tester / integrated_tester1 的关系

| 模块 | 位置 | 用途 |
|------|------|------|
| `programmer1/integrated_tester1` | programmer1 内部 | reviewer 前的程序化检查（pytest + dom_check） |
| `_deprecated_lite_tester` | 顶层独立文件夹 | 旧 pytest-only tester，保留参考，不参与顶层流程 |
| `test_engineer1` | 顶层独立模块 | 跨模块 E2E 测试（api_contract + Python 服务管理 + Python Playwright） |

---

## 扩展：加新检查项

1. 在 `standard.py` 的 `KNOWN_CHECKS` 加新 key
2. 在 `nodes/run_mcp_tests.py` 里实现对应逻辑
3. 在 `test_standards_cfg.py` 的 `E2E_HUMAN_STANDARD` 或 `E2E_PLANNER_STANDARD` 里加上
