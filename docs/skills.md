# Skills 与工具

本项目的 7 个 skills 是依据业务目标编写和维护的研究规程，使用常见 `SKILL.md` 前置元数据与正文布局。它们不是安装到个人 Codex 的全局技能，也不是伪装成第三方开源包的复制品。

每次控制调用先看到名称、描述、可用工具和内容版本。模型通过 `load_skill` 读取正文后，工具门禁才允许相应动作。`read_reference` 读取 skill 目录内的 Markdown，并受路径和上下文额度限制。`unload_skill` 释放正文和参考资料上下文；规划契约始终保留。

| Skill | 使用时机 | 工具 |
| --- | --- | --- |
| research-planning | 全程管理研究问题、完成条件和停止判断 | complete_task、defer_task、finish |
| web-research | 发现与阅读原始证据 | search_web、search_papers、search_github、fetch_page、read_paper、read_document、find_in_document、browser_read |
| benchmark-verification | 榜单、指标或评测口径核验 | read_document、find_in_document；与 evidence-review 配合 |
| evidence-review | 把具体断言和已观察证据交给核验器 | assess_claim |
| memory-recall | 同实体历史结论与未解决问题 | memory_search、read_document、find_in_document |
| ecosystem-research | 竞品、公开团队变动、供应商信息 | search_web、fetch_page、read_document、find_in_document、search_github |
| dataset-profiling | 已有本地数据样本的质量初查 | profile_dataset、read_document、find_in_document |

`profile_dataset` 支持 CSV/TSV/JSON，最多 8MB、10,000 行、100 个展示字段；检查字段、缺失、重复行和指定 ID 的跨 split 重叠。它不能替代许可证审核或总体偏差评估。

`browser_read` 先无步骤读取页面及控件快照，再使用快照中出现的唯一 CSS selector 做一次 click/select，逐次观察。页面在全新无凭据浏览器上下文中打开；不支持登录、文件上传、下载或 POST 操作。

加入第三方开源 skill 时，需要先核对许可证、实际依赖、工具名称及权限，再适配到这里的类型化工具和引用机制。不能仅复制一份 `SKILL.md` 就宣称其脚本与工具已经可用。当前项目没有运行未经检查的远程 skill 或任意脚本。

参考接口：

- [Agent Skills 格式](https://agentskills.io/specification)
- [DeepSeek JSON 输出](https://api-docs.deepseek.com/guides/json_mode/)
- [DeepSeek 思考开关与 effort](https://api-docs.deepseek.com/guides/thinking_mode/)
- [Tavily Search API](https://docs.tavily.com/documentation/api-reference/endpoint/search)
- [Hugging Face Hub API](https://huggingface.co/docs/hub/api)
- [FastEmbed](https://github.com/qdrant/fastembed)

这些链接解释协议或适配接口，并不表示项目通过了第三方认证。
