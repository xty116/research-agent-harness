# 项目组织结构与系统图

本图对应仓库的实际单 Agent 实现。Skills 是研究规程，工具是类型化动作；Harness 负责边界与持久性，ContextBuilder 负责本次可见材料，Store 负责事务。定时器只发起固定目标，不预先规定研究动作顺序。

## 组件组织图

```mermaid
flowchart TB
    subgraph Entry[入口与交付]
        CLI[cli.py: 运行 / 恢复 / 诊断 / 备份 / 补报]
        WEB[web.py: 只读 FastAPI 看板与 API]
        SCHED[scheduler.py: 当地日唯一调度]
        REPORT[reporting + report_model + rendering]
        VIEW[Jinja 页面 / CSS / JS / 离线 HTML]
    end
    subgraph Core[决策与执行]
        APP[application.py: 依赖装配]
        H[harness.py: 生命周期与模型控制循环]
        CTX[context.py: 分层选择与裁剪]
        SK[skills.py + skills/: 7 个研究规程]
        TOOL[tools.py: 17 个注册工具与参数校验]
        DOMAIN[domain.py: 计划 / 决策 / 断言 / 报告契约]
    end
    subgraph Infra[资源与持久化]
        MODEL[providers.py: DeepSeek 结构化输出与思考路由]
        SOURCE[providers.py: 五通道来源适配]
        NET[network.py: 有界公共网络与重定向校验]
        MEM[memory.py: FTS + Qwen3 / RRF]
        DB[store.py: SQLite WAL / 租约 / 预算 / 检查点]
        CONF[settings.py + config/: 固定目标与本地配置]
    end
    CLI --> APP
    WEB --> APP
    WEB --> SCHED
    SCHED --> H
    APP --> H
    H --> CTX
    CTX --> SK
    CTX --> MEM
    H --> MODEL
    H --> TOOL
    TOOL --> SK
    TOOL --> SOURCE
    TOOL --> MODEL
    SOURCE --> NET
    H --> DB
    TOOL --> DB
    MEM --> DB
    H --> REPORT
    WEB --> REPORT
    REPORT --> VIEW
    REPORT --> DB
    DOMAIN -.类型契约.-> H
    CONF --> APP
```

## 一次动作的调用时序

```mermaid
sequenceDiagram
    participant S as Scheduler / CLI
    participant H as Harness
    participant C as ContextBuilder
    participant M as DeepSeek
    participant T as ToolExecutor
    participant D as Store
    S->>H: 固定 Prompt / 新线索 / 历史任务
    H->>D: 获取租约、创建运行与初始检查点
    H->>T: 有界发现扫描
    T->>D: 保存证据版本与通道状态
    H->>C: 组装规划材料、目录、历史、预算
    C->>D: 召回相关证据与待办
    H->>D: 持久预占模型额度
    H->>M: 思考规划 / 结构化 Plan
    M-->>H: 任务、完成条件、优先级
    H->>D: 校验并保存计划 / usage 结算
    loop 每步直到完成或到达上限
        H->>C: 重建当前上下文
        H->>M: 一个 Decision
        M-->>H: 工具、参数、理由、计划修订
        H->>D: 保存调用意图与检查点
        H->>T: 类型 / Skill / 预算 / 已观察证据门禁
        T-->>H: Observation 或明确的失败
        H->>D: 追加观察、证据 / 断言、任务与检查点
    end
    H->>M: 综合 + 五维 Critic
    H->>D: 引用门禁、终态、费用与独立报告路径
    H-->>S: complete / partial / interrupted
```

工具内部的外部请求同样先预占再执行。`assess_claim` 先用非思考模型；遇证据冲突时额外调用思考模型裁决。最多一次质量补查，恢复不重置原时间与预算。

## 上下文与记忆读写图

```mermaid
flowchart LR
    subgraph Durable[持久资产]
        E[证据版本 documents + FTS + vectors]
        K[结论修订 claims]
        Q[跨日待办 tasks]
        EP[情节事件 events]
        P[程序规程 skills/]
        W[工作检查点 runs.checkpoint]
    end
    E --> R[按词项 / 向量相关性与日期召回]
    K --> R
    Q --> R
    W --> B[ContextBuilder]
    R --> B
    EP --> B
    P -->|先目录、按需正文和 reference| B
    B --> C[保护约束 + 当前任务 + 有界原文 + 近期观察]
    C --> M[本次模型调用]
    M --> A[结构化决策与已校验观察]
    A --> W
    A --> EP
    A --> E
    A --> K
    A --> Q
```

原始证据、任务约束与预算不因上下文裁剪而丢失。历史结论带日期与修订身份，不能自动升级为今日事实。未实现独立实体关系知识图谱、自动清理审计记录或自动改写 Skill。

## 数据关系

```mermaid
erDiagram
    RUNS ||--o{ RUN_DOCUMENTS : observes
    DOCUMENTS ||--o{ RUN_DOCUMENTS : appears_in
    RUNS ||--o{ CLAIMS : assesses
    RUNS ||--o{ EVENTS : appends
    RUNS ||--o{ USAGE : reserves
    DOCUMENTS ||--o| VECTORS : embeds
    RUNS ||--o{ TASKS : last_updates
    RUNS {
        string id PK
        string daily_key UK
        string local_day
        string status
        json checkpoint
        string report_path
    }
    DOCUMENTS {
        string id PK
        string url
        text text
        string kind
        string fetched_at
    }
    CLAIMS {
        string id PK
        string question_key
        string run_id FK
        string task_id
        json payload
    }
    TASKS {
        string id PK
        string question_key UK
        json payload
        string last_run_id
    }
    EVENTS {
        int seq PK
        string run_id
        string kind
        json payload
    }
    USAGE {
        string id PK
        string run_id
        string local_day
        float reserved_usd
        float actual_usd
        int credits
        string status
    }
    RUN_DOCUMENTS {
        string run_id FK
        string document_id FK
    }
    VECTORS {
        string document_id PK
        string model
        json vector
    }
```

图中关系同时包含应用层逻辑关系：`tasks.last_run_id`、`events.run_id` 及 `usage.run_id` 并非全部通过数据库外键声明。FTS5 是证据搜索索引；leases 表独立管理执行租约，不表示业务事实。

## macOS 部署结构

```mermaid
flowchart TD
    SRC[源码目录: Git 仓库 + 忽略的本地配置] -->|service.py install 同步代码| RT[Application Support/ResearchAgentHarness]
    SRC -->|首次一致性迁移、之后不覆盖| DB[运行数据 / SQLite / 报告 / 缓存]
    ENV[既有环境或私有 .env] -->|仅选用的键 / 0600| SEC[运行目录 secrets.env]
    LD[用户 LaunchAgent / KeepAlive] --> PROC[运行副本 .venv / FastAPI + Scheduler]
    RT --> PROC
    SEC --> PROC
    PROC --> DB
    DEV[源码 CLI] -->|同一数据库与预算账本| DB
    BROWSER[本机浏览器] -->|127.0.0.1:8765 只读| PROC
```

退出终端不影响 LaunchAgent。退出登录、关机及睡眠期间不承诺执行；恢复到点后只补当天尚未建立的定时运行。默认便携配置不启用调度，已部署机器的配置在被忽略的本地文件中单独维护。

## 完整源码目录

下列清单包含版本库内容，不包含凭据、运行数据、缓存、本地配置或生成的 LaunchAgent。

```text
research-agent-harness/
├── .github/
│   └── workflows/
│       └── check.yml
├── config/
│   ├── demo.yaml
│   ├── settings.yaml
│   └── watchlist.yaml
├── docs/
│   ├── images/
│   │   ├── dashboard.jpg
│   │   ├── report-mobile.jpg
│   │   └── report.jpg
│   ├── architecture.md
│   ├── operations.md
│   ├── reports.md
│   ├── skills.md
│   ├── structure.md
│   └── validation.md
├── evals/
│   └── cases.json
├── examples/
│   ├── README.md
│   ├── demo-report.html
│   ├── demo-report.md
│   └── demo-report.report.json
├── scripts/
│   ├── live_smoke.py
│   ├── package_source.py
│   └── service.py
├── skills/
│   ├── benchmark-verification/
│   │   ├── references/
│   │   │   └── comparison.md
│   │   └── SKILL.md
│   ├── dataset-profiling/
│   │   └── SKILL.md
│   ├── ecosystem-research/
│   │   └── SKILL.md
│   ├── evidence-review/
│   │   └── SKILL.md
│   ├── memory-recall/
│   │   └── SKILL.md
│   ├── research-planning/
│   │   └── SKILL.md
│   └── web-research/
│       ├── references/
│       │   └── routing.md
│       └── SKILL.md
├── src/
│   └── research_agent/
│       ├── static/
│       │   ├── app.css
│       │   ├── app.js
│       │   └── favicon.svg
│       ├── templates/
│       │   ├── about.html
│       │   ├── base.html
│       │   ├── error.html
│       │   ├── evidence.html
│       │   ├── macros.html
│       │   ├── overview.html
│       │   ├── report.html
│       │   ├── report_body.html
│       │   ├── reports.html
│       │   ├── skills.html
│       │   ├── source.html
│       │   └── standalone.html
│       ├── __init__.py
│       ├── application.py
│       ├── cli.py
│       ├── context.py
│       ├── demo.py
│       ├── domain.py
│       ├── errors.py
│       ├── evaluation.py
│       ├── harness.py
│       ├── logging_config.py
│       ├── memory.py
│       ├── network.py
│       ├── providers.py
│       ├── rendering.py
│       ├── report_model.py
│       ├── reporting.py
│       ├── scheduler.py
│       ├── settings.py
│       ├── skills.py
│       ├── store.py
│       ├── tools.py
│       └── web.py
├── tests/
│   ├── conftest.py
│   ├── test_budget_memory.py
│   ├── test_context_provider_web.py
│   ├── test_harness.py
│   ├── test_network_tools.py
│   ├── test_report_center.py
│   ├── test_retrieval_reporting.py
│   └── test_sources.py
├── .env.example
├── .gitignore
├── AGENTS.md
├── README.md
├── pyproject.toml
└── uv.lock
```

运行后生成的 `data/`、`reports/`、`.venv/` 和 `config/settings.local.yaml` 不在上述版本库中。
