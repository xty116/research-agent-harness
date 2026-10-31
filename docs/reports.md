# 报告、看板与接口

## 历史记录

运行身份由 `run_id` 确定，每次单独归档。同日多次执行不会相互覆盖。记录类型目前从 mode 与 ID 前缀推断：demo 模式为合成演示；`eval_` 为标注评测；`accept_` 为单任务验收；`source-health` 为通道诊断；其余为日度研究。不要手动重命名数据库运行 ID。

`complete` 仅指本次有界范围内通过信源、任务、引用及 Critic 门禁。`partial` 是带缺口的部分交付；`interrupted` 是保留检查点的中断记录；`evaluated` 表示标注评测结束，不等于真实日报完成。运行中页面为过程快照，不代表定稿。

报告中心在 SQL 中先过滤再分页，按开始时间倒序；每页 12 条。支持标题/ID 的文字搜索、状态、类型、开始与结束日期。时间按配置时区显示，日报归属按当地日计算。对运行中记录每 20 秒检查状态并提示刷新，不强制打断阅读。

## 四种归档文件

终态研究与评测写入 `reports/{live|demo}/DATE-RUN.*`：

- `.md`：中文可阅读版。
- `.report.json`：标准化交付视图，包含 schema_version、run_id、day、mode、kind、status、scope、findings、recommendations、claims、sources、channels、tasks、quality、warnings、usage、stages。
- `.json`：完整 RunState，保留旧调试与恢复接口；不会与标准化 JSON 混用。
- `.html`：内嵌 CSS，无 JavaScript/CDN，保存后可离线阅读；链接指向原始证据页面。

每份报告包括事实与推断标记、Claim ID、引文与证据版本、信源错误、待跟进问题、五维评审、停止原因、实际返回 token 和估算预算。历史断言修订保留并标识；新建议不得引用已被替代的修订。

自动综合失败时使用结构化核验记录，并显示 origin=structured_record 和局限，不捏造模型报告。诊断与标注评测有独立标题及范围说明，不填充为业务发现。未发生的研究步骤、核验与评审保持为空。

运行中硬崩溃可能尚未生成磁盘报告，网页仍可由检查点重建当前视图。恢复到终态后会落盘；有缺失的历史终态产物可用 `repair-reports` 重建。四个文件逐个原子替换，文件组不宣称跨文件事务；如写盘中断导致产物不齐，重新修复即可。

```bash
uv run research-agent repair-reports
uv run research-agent repair-reports --force
uv run research-agent repair-reports --demo
```

此命令取得研究租约，跳过 running，保持原始 status、checkpoint、updated_at、error 与 usage，只更新派生报告路径。失败项目单独返回，不静默忽略。耗时来自原运行时间，不把补报时间当成研究耗时。

## 页面与下载

| 路由 | 内容 |
| --- | --- |
| `/` | 最近正式研究、当天费用、五通道状态、七日节奏与待办 |
| `/reports` | 全历史检索与分页 |
| `/runs/{id}`、`/runs/{id}/report` | 报告阅读页 |
| `/runs/{id}?tab=evidence` | 来源资料与本地不可变证据版本 |
| `/runs/{id}?tab=trace` | 决策/观察事件，每页 100 条，after 游标继续读取 |
| `/runs/{id}?tab=tasks` | 优先级、缺口、完成条件与延期时间 |
| `/runs/{id}/export.md` | Markdown 下载，可用 download=false 复制正文 |
| `/runs/{id}/export.json` | 标准化交付 JSON |
| `/runs/{id}/export.state.json` | 原始状态 JSON |
| `/runs/{id}/export.html` | 独立离线网页 |
| `/sources/{document_id}` | 已保存原文版本与抓取信息 |
| `/evidence` | 原子断言搜索及 verdict 筛选 |
| `/evidence?tab=questions` | 当前跨日待跟进问题 |
| `/skills` | 7 个 Skill 的正文、允许工具与实际工具目录 |
| `/about` | 本地使用与结构说明 |

页面与 API 均可使用 `mode=live` 或 `mode=demo`，查找在所选数据库中进行；不会将另一个模式的同名 ID 当作本记录。导出动作不改运行数据库，也不调用模型。浏览器“打印 / PDF”使用本机打印对话框及专用 A4 样式。

## 只读 API

| 路由 | 返回 |
| --- | --- |
| `/health/live` | 进程状态 |
| `/health/ready` | 数据库 quick_check、凭据配置是否存在、调度和向量状态 |
| `/api/overview` | 累计计数、当天模型估算及搜索 credits |
| `/api/runs?page=1&limit=30` | 不含 checkpoint 的运行列表，保留旧列表接口 |
| `/api/report-catalog` | 分页标准报告，items/total/page/pages/page_size；q/status/kind/start/end 筛选 |
| `/api/runs/{id}` | 元数据、checkpoint、usage、snapshot_at |
| `/api/runs/{id}/report` | 标准化交付 JSON |
| `/api/runs/{id}/events?after=0&limit=100` | 按 seq 升序事件，最多 1000 条/次 |
| `/api/memory` | 最近断言与待办摘要 |
| `/api/skills` | Skill 元数据 |
| `/api/docs` | OpenAPI 交互文档 |

健康检查不进行付费调用，不保证上游额度和内容质量。没有启动任务、修改配置或删除数据的 HTTP 接口。页面对外部内容自动转义，链接只允许无内嵌凭据的 HTTP(S)，并设置 CSP、nosniff、no-referrer 与禁止嵌入头。

本地看板能够展示真实原文和原始状态，备份/截图/分享前需确认内容。仓库 `examples/` 只包含明确标记的合成报告。
