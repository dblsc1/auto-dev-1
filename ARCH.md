# Growth Garden Builder — 架构手册

> 一张图看懂整个系统，然后逐层展开。

---

## 一、全景图

```
用户输入需求
     │
     ▼
┌─────────────────────────────────────────────────────────────────┐
│  graph.py  （LangGraph StateGraph，顶层编排）                    │
│                                                                 │
│  [intake] ──► [dispatch] ──► [backend]  ──┐                    │
│                    ▲                       │                    │
│                    │              [frontend] ──┐                │
│                    │                          │                 │
│               [batch_review] ◄────────────────┘                 │
│                    │  (more batches → dispatch)                 │
│                    │  (all done → test)                         │
│                    ▼                                            │
│               [test] ──► [review] ──► [commit_push] ──► [done] │
└─────────────────────────────────────────────────────────────────┘
```

---

## 二、节点职责

| 节点 | 文件 | 模型 | 职责 |
|------|------|------|------|
| `intake` | `arbiter.py` | Codex L5 | 分析需求，拆成有依赖顺序的批次（batches），写 `.arbiter_plan.json` |
| `dispatch` | `graph.py` | — | 纯路由：看 `cursor` 取下一个任务，按 `owner` 分发给对应模块节点 |
| `backend` | `programmer1/` | 见下 | 后端子图（FastAPI），包含 planner→workers→internal_tester→reviewer |
| `frontend` | `programmer1/` | 见下 | 前端子图（HTML/JS），同上 |
| `batch_review` | `arbiter.py` | Codex L5 | 每批完成后先执行 `ok=false` 硬门禁，再由 AI 裁决继续或重试 |
| `test` | `graph.py` | Python | test_engineer1：api_contract 硬检查 + Python Playwright E2E |
| `review` | `programmer1/modules/arbiter.py` | Codex L5 | 看测试结果拍板：通过→commit_push，失败→dispatch rollback |
| `commit_push` | `arbiter.py` | — | 对每个 workspace 做 git commit；若 git_remote 非空则 push |
| `done` | `graph.py` | — | 汇总输出结论，写 devlog.md |

---

## 三、批次化流程（arbiter_batch_review）

```
intake 产出:
  batches = [
    [be-1],           ← 批次 0：先做后端
    [fe-1],           ← 批次 1：再做前端（依赖后端 API）
  ]

批次 0 执行完 → batch_review:
  先检查本批全部 ModuleReport.ok=true；只有通过后才让 AI 输出 PROCEED / RETRY / ABORT
  ├─ PROCEED   → 加载批次 1 进 dispatch
  ├─ RETRY:be-1→ 把 be-1 插到批次 1 之前再执行一次
  └─ ABORT / quota / retry exhausted → decision=escalate，停止等待人工

  留痕写入 GraphState.batch_logs：
  [{"batch_index": 0, "timestamp": "...", "tasks": [...], "reports": [...]}]

批次 1 执行完 → batch_review:
  无后续批次 → PROCEED → 进入 test
```

保护机制：`max_batch_iterations`（默认 10）超限后 `decision=escalate`，不允许带着未完成模块进入测试。

---

## 四、programmer1 子图（backend / frontend 共用）

```
[planner] ──► [workers×N] ──► [internal_tester] ──► [reviewer] ──► report
```

| 节点 | 模型 | 职责 |
|------|------|------|
| `planner` | Codex L5 | 把模块任务拆成函数级子任务；输出含 function_specs、interface_contract、sequential 标记 |
| `workers` | DeepSeek L3（可配） | sequential=false 并行，sequential=true 串行（同文件顺序追加场景）|
| `internal_tester` | 纯 Python | 按 InternalTestStandard 跑 pytest / dom_check / `run_contract`；任一失败→红灯并写 `contract_report.json` |
| `reviewer` | Codex L4 | ① 文件范围机械闸 ② 内部测试/执行契约硬闸 ③ 语义审查；打回最多 MAX_MODULE_RETRIES 次 |

**DEV_MODE**（`graph.py` 顶部 `DEV_MODE=True`）：planner 拆完方案后暂停等人工审批，审批不通过重拆。

**顺序 worker**：当同一文件需要多个 worker 顺序追加时（如 index.html 骨架→渲染→交互），planner 把子任务标 `sequential=true`；workers 节点检测到后切换为串行 `await`，后序 worker 用 Edit 而非 Write。

---

## 五、模型分级（engine/registry.py）

| Level | Provider | 模型 | 用在哪 |
|-------|----------|------|--------|
| L6 | Codex | （订阅默认）effort=high | 高算力备用，替代 Opus |
| L5 | Codex | （订阅默认）effort=high | arbiter、planner，规划推理 |
| L4 | Codex | （订阅默认）effort=high | reviewer，代码审查 |
| L3 | DeepSeek | deepseek-v4-pro | worker，写代码 |
| L2 | GLM | glm-4 | 轻量备用 |
| L1 | DeepSeek | deepseek-v4-flash | 极轻量备用 |

凭证：`.env` 文件，`DEEPSEEK_API_KEY` / `GLM_API_KEY`。Codex 用本机订阅，无需 key。

---

## 六、测试体系（三层）

```
层 1  integrated_tester1/   程序化，在 workers 之后立即跑
      ├─ pytest              python -m pytest workspace/
      └─ dom_check           静态分析 HTML：检测 inline-style 覆盖 class、缺 fetch 错误处理等

层 2  test_engineer1/        顶层 test 节点：real 模式默认启用
      ├─ api_contract         读模块 execution_contract + shared backend_api，核对后端路由、前端 fetch 和声明的 consumes
      ├─ plan_tests           Codex 读代码，输出结构化测试计划 JSON
      └─ run_mcp_tests        Python 起/停 FastAPI + 同源代理；Python Playwright 执行测试计划
```

**TestStandard 双变量**（`test_standards_cfg.py`）：

```python
BACKEND_HUMAN_STANDARD   = InternalTestStandard(required={"compile","pytest","api_contract"})  # 铁律，永不删
FRONTEND_HUMAN_STANDARD  = InternalTestStandard(required={"compile","dom_check","api_contract"})
BACKEND_PLANNER_STANDARD = InternalTestStandard(...)   # planner 只能加，不能删铁律
# effective = human_standard.merged(planner_standard).planner_add(*task.test_additions)
```

**执行契约硬闸**：planner 先输出 `execution_contract.json`，框架先做 DSL/语言/preflight，再在 worker 后执行 `run_contract`。它检查文件范围、函数签名、后端路由、前端 DOM/fetch 和跨模块 consumes；失败时按 `repair_target=planner|worker` 重拆或重写，不能由 reviewer 放行。

**Consumer-driven contract 流**：

```
backend planner
  └─ public_api
      └─ graph.py 导出 contracts/backend_api.json          # provider contract

frontend planner
  └─ consumes: [{provider:"backend", method:"POST", path:"/login"}]
      ├─ run_contract 先验证 consumes 必须存在于 backend published_api
      └─ graph.py 导出 contracts/consumers/frontend__backend.json  # consumer pact

test_engineer1
  ├─ api_contract 读取 backend_api.json
  ├─ consumer-pacts 读取 contracts/consumers/*.json
  ├─ provider verification：backend published_api 必须满足每个 consumer pact
  ├─ plan_tests 生成 E2E JSON
  ├─ plan_schema.py 机器校验 action/字段/顺序/瞬时 loading 规则
  └─ run_mcp_tests 执行通过 schema 的 Playwright 计划
```

规则：provider contract 由后端发布；consumer pact 由前端的 `consumes` 产生；E2E planner 只能验证 pact，不允许重新发明接口。测试计划 JSON 先过 `test_engineer1/plan_schema.py`，非法 action、缺字段、点击前 `expect_request`、无 delay 的 loading 断言都会在 test_plan 阶段失败，不能打回业务模块。

---

## 七、CI（`.github/workflows/ci.yml`）

```
push / PR
  │
  ├─ lint（无 API key）
  │    ├─ py_compile 所有 .py
  │    └─ import smoke: build(real=False)
  │
  └─ e2e（无 AI key，needs lint）
       ├─ 启动 uvicorn main:app :8000
       ├─ 启动同源静态代理（只转发契约声明的 API）
       └─ Python Playwright 执行浏览器用例
```

E2E 不需要任何 API key：测试的是 repo 内的示例 app（乘法计算器），
与 AI pipeline 产物（`~/gg-workspace/`）完全解耦。

---

## 八、目录结构

```
growth-garden-builder/
├── graph.py                  顶层编排图（build / main）
├── arbiter.py                业务裁决：intake + batch_review + review_test
├── backend_cfg.py            后端 ModuleConfig（workspace、standard、rules）
├── frontend_cfg.py           前端 ModuleConfig
├── test_standards_cfg.py     TestStandard 外部配置（human + planner 双变量）
├── main.py                   示例后端（FastAPI 乘法器）
├── index.html                示例前端（HTML 乘法计算器）
│
├── programmer1/              编码子系统（git submodule）
│   ├── engine/               模型路由层（registry、agent、providers）
│   ├── modules/              子图：code_module（planner→workers→tester→reviewer）
│   ├── integrated_tester1/   程序化内部测试器（pytest + dom_check）
│   ├── schemas.py            Task / CodingResult / ReviewResult / ModuleReport
│   ├── state.py              GraphState / ModuleState
│   └── config.py             ModuleConfig（含 human_standard + planner_standard）
│
├── test_engineer1/           E2E 测试子系统
│   ├── graph.py              2 节点子图：plan_tests → run_mcp_tests
│   ├── nodes/
│   │   ├── plan_tests.py     Codex 读代码 → 结构化测试计划 JSON
│   │   └── run_mcp_tests.py  Python 服务生命周期 + Python Playwright
│   ├── standard.py           E2ETestStandard（required / extra / 端口配置）
│   └── config.py             TestEngineerConfig
│
├── _deprecated_lite_tester/  旧 pytest-only tester，保留参考，不参与顶层流程
│
└── tests/                    CI & 本地 E2E 测试（无需 AI）
    ├── conftest.py           playwright browser fixture（含 snap-chromium fallback）
    ├── test_e2e.py           4 个用例：页面加载 / 3×7 / 负数 / 空输入报错
    └── run_e2e.sh            本地一键跑：起服务 → pytest → 停服务
```

---

## 九、关键环境变量

| 变量 | 含义 |
|------|------|
| `GG_REAL=1` | 启用真实 AI 调用（否则 stub 模式，免费全景） |
| `GG_E2E` | 已废弃；`GG_REAL=1` 时默认使用 test_engineer1 E2E |
| `DEV_MODE=True` | graph.py 顶部旋钮，planner 拆完等人工审批 |
| `DEEPSEEK_API_KEY` | DeepSeek L1/L3 凭证 |
| `GLM_API_KEY` | GLM L2 凭证（无则自动升 L3） |

---

## 十、扩展指南

**加新模块（如 godot）**：
1. 新建 `godot_cfg.py`，配置 `ModuleConfig(name="godot", workspace=...)`
2. `graph.py`：`_KNOWN_MODULES` 加 `"godot"`，`build()` 里注册节点
3. `arbiter.py` `_SYSTEM` 提示词里加 godot 说明

**加新 TestStandard 检查项**：
1. `programmer1/integrated_tester1/standard.py` → `KNOWN_CHECKS` 加新 key
2. `integrated_tester1/runner.py` → `run_checks()` 加对应实现
3. `test_standards_cfg.py` → 按需加入 `human_standard.required`

**让 batch_review 更智能**：
`arbiter_batch_review` 里的 `_ai_batch_decision()` 已接入 Codex L5。
可在 `_BATCH_REVIEW_SYSTEM` 里补充业务规则，例如：
"若后端 API 路径变更，自动修改前端批次的 description 后再 PROCEED"。
