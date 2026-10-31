# 本地运行与恢复

## 服务管理

项目服务名：`com.xty.research-agent-harness`。用户登录后由 LaunchAgent 启动，退出终端不影响服务，进程异常退出由 launchd 重启。配置文件位于用户的 `~/Library/LaunchAgents/com.xty.research-agent-harness.plist`，不含 API Key。

源码在克隆的仓库目录，常驻运行目录在 `~/Library/Application Support/ResearchAgentHarness`。安装器生成的 `config/settings.local.yaml` 中数据/报告路径指向运行目录，开发 CLI 和服务因此共用一份预算账本。安装器只在首次部署迁移数据库，以后同步代码和配置，不覆盖运行数据。

所需凭据保存在运行目录的 `secrets.env`（目录 0700、文件 0600），不复制其他原项目变量。修改原凭据后执行 `service.py install` 同步；停止/卸载服务不会自动删除该私有文件。无需修改系统安全设置或授予全盘访问权限。

在项目根目录执行：

```bash
.venv/bin/python scripts/service.py status
.venv/bin/python scripts/service.py stop
.venv/bin/python scripts/service.py start
.venv/bin/python scripts/service.py restart
.venv/bin/python scripts/service.py install
.venv/bin/python scripts/service.py uninstall
```

`stop` 会卸载当前任务，避免 KeepAlive 立即拉起；`start` 重新加载已有配置。`uninstall` 只删除本项目的 LaunchAgent，不删除数据、报告或原项目。

macOS 沙箱可能限制启动浏览器或访问 launchd。部署在用户会话中正常运行；开发验收时需要允许该进程启动本项目的 Chromium。无需关闭系统安全设置或开启远程访问。

## 时间、幂等与费用

每天按 `schedule.timezone/hour/minute` 判断到点，默认悉尼 08:30，自动处理夏令时。每 30 秒检查；错过时间后在当天恢复运行时补跑。默认不会补跑所有历史缺失日期，也不会唤醒关机的电脑。

同一当地日的定时任务只创建一次，包括 `partial` 结果。这样上游故障时不会每 30 秒花钱重试。恢复额度后可手动 `research-agent run`，新运行仍累计到当天预算。正常暂停/退出期间的未完成运行会优先恢复；崩溃后租约最长 180 秒到期。

模型每次请求先预占再结算。费用是配置费率下的估算，包含思考输出 tokens；超时未确认计费的额度保留。默认单次 $0.30、当天 $0.60，搜索额度单列。不要通过改数据库、删 usage 或创建新运行来释放未知费用。实际账单请以供应商控制台为准。

## 健康与日常检查

- `GET /health/live`：进程存活。
- `GET /health/ready`：本地数据库、Key 是否配置、skills 数量、调度与向量状态。只检查配置，不保证上游余额、速率或服务可用，也不发付费请求。
- `GET /api/runs`、`GET /api/runs/{id}/events`：状态和轨迹。
- `GET /api/memory`：最新结论和待跟进问题。
- 看板只读，默认仅绑定 `127.0.0.1:8765`。没有用于远程管理或登录鉴权的公网接口，不应直接改为公网监听。

`complete` 表示本次有界范围内的信源扫描、任务、确定性校验和 Critic 都通过；不代表覆盖全网或保证所有结论正确。`partial` 是有效的部分交付，需要看报告中的缺口、失败信源和停止原因。

## 已知上游错误

| 现象 | 行为与处理 |
| --- | --- |
| Tavily HTTP 432，plan usage limit | 账户用量超限，四方向全网检索缺失；不会升级套餐或切换收费服务。恢复原账户额度或由用户配置新 Key 后手动重跑 |
| DeepSeek 401 | 凭据无效，明确失败并释放本次未接受请求预占；核对环境变量/原项目配置 |
| 网络超时或 5xx | 有限重试，未知计费保留预占；继续失败则部分交付 |
| 结构化 JSON 截断或字段错误 | 提供脱敏字段提示，有界重试；思考与非思考输出上限分别配置 |
| 浏览器或向量不可用 | 工具返回明确失败/状态；可换普通抓取或 FTS，不能伪装已经完成浏览器取证或语义召回 |
| 无法取得完整评测口径 | 输出 unverified，延期并保留原问题和缺失字段 |

一次本地联调观察到，配置中的 `deepseek-chat` 与 `deepseek-reasoner` 均可调用，但实际返回名为 `deepseek-flash`。项目保留原名称并显式设置思考开关。若要更换底层模型，需修改配置、按所用费率调整估算并重新做小额验收。

## 日志、数据与备份

```text
data/agent-live.sqlite3     真实状态、证据、结论、预算与向量
data/agent-demo.sqlite3     合成演示数据
data/logs/service.log       轮转日志，2MB × 当前与3份历史
data/logs/launchd.*.log     进程标准输出/错误（含少量启动和崩溃信息）
data/models/               Qwen3 模型缓存
data/browsers/             Chromium
data/samples/              人工提供的本地样本
reports/live/              Markdown + 原状态 JSON + 标准报告 JSON + 离线 HTML
reports/demo/              标记为合成演示的报告
```

这些路径相对于 `~/Library/Application Support/ResearchAgentHarness`；源码目录中的旧 `data/` 与 `reports/` 是首次部署前的保留记录。

标准输出/错误由 launchd 写文件，未做自动轮转，长期运行时应检查体积。主业务日志会轮转；模型原始思维链、凭据和 HTTP 调试日志不主动记录。

一致性备份：

```bash
.venv/bin/research-agent backup data/backups/agent-backup.sqlite3
```

上面的相对备份路径以当前目录为准；运行数据库和常驻日志位于 Application Support 运行目录。可用 `archive-task TASK_ID --reason "已不再适用的原因"` 管理性归档问题；不会伪装为已核验完成，也不会删除审计记录。

还应复制 `reports/`、`config/` 和 `skills/`；不把原项目 `.env` 放进版本库。恢复时先停服务，把当前数据目录完整保留，再以备份恢复数据库；不要在运行期间直接替换 SQLite 文件。模型/浏览器缓存可重新下载，无需作为业务证据备份。

自动清理长期记忆尚未启用，默认只是召回窗口限制，不会悄悄删除审计记录。长期运行应按磁盘容量制定人工归档策略。

## 修改与再次验收

修改源码目录里的配置、Prompt、工具或 skills 后，执行 `service.py install` 同步并重启；单独 `restart` 仅重启已经部署的版本。改每日 Prompt/权重会影响之后运行，旧运行保留原 Prompt。改工具/skill 后执行 Ruff、pytest 和 skill 格式校验，再用明确标记的 demo 和小额真实运行检验。

```bash
.venv/bin/ruff check .
.venv/bin/pytest -q
.venv/bin/research-agent run --demo
.venv/bin/research-agent run --fail-on-partial
```

离线测试用于控制逻辑、故障恢复和证据约束回归；它不能证明线上来源永远可访问，也不能证明模型研究质量。真实验收结果与外部依赖状态在 `docs/validation.md` 单独记录。

## 报告修复与升级

升级前备份数据库；在源码目录安装新依赖并执行 `service.py install`，再运行 `research-agent repair-reports --force` 和演示模式修复。补报只生成派生文件，不改变原研究状态、检查点或 usage。服务的每次真实调度也会输出四种报告文件。详细文件格式与接口见 [reports.md](reports.md)。

仓库便携配置关闭定时。首次安装前在 `config/settings.local.yaml` 中明确启用调度并配置预算。`service.py render` 只预览本机 plist，不安装；生成的 plist 和本地路径不进入版本库。安装脚本优先使用已有系统 Python 3.14，否则采用执行脚本的解释器；若解释器位于受 macOS 后台访问限制的位置，改用普通安装的 Python 3.12+ 后重装，无需降低系统安全设置。
