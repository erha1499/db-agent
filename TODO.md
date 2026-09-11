# db-agent TODO

目标：让研发人员通过自然语言或 SQL，完成有明确权限、风险检查和可核对结果的数据库查询与分析。

**当前阶段：原 1–19 步已交付各自限定范围，接下来优先改善首次配置和日常启动体验。** 状态核对至 2026-09-11；启动命令见 [README](README.md)，开发约定见 [AGENTS.md](AGENTS.md)。目前仍需分别完成配置、建表、Web 身份管理和前端构建，尚无统一初始化与启动自检入口。

## 下一步：降低上手成本

以下为建议优先级，不代表已经启动或授权实施。先完成一项并验收，再选择下一项。

| 优先级 | 待办 | 完成时应能验证什么 |
| --- | --- | --- |
| P1 | **统一本机初始化入口**：串起依赖检查、必要配置、合成数据和 Web 身份准备 | 新用户按一条主路径到达首次登录和成功 SQL 查询；已有配置、账号、表和数据卷不被覆盖；模型配置可后补 |
| P1 | **增加配置与启动自检**：集中报告缺少的配置、数据库连接、表范围、身份文件、前端产物和端口问题 | 错误能直接定位到修复步骤；不打印凭据；数据库检查不依赖模型凭据，模型连通性需显式触发 |
| P2 | **自动核对 README 的首次与再次启动流程** | 隔离合成环境按文档完成安装、初始化、登录、查询、停止后重启；重复操作保留已有数据；真实模型验证单独记录 |

## 已交付里程碑索引

保留原编号，便于查找旧任务。下表表示源码中已有实现及对应验收记录，不是本轮重新运行的结果，也不表示当前远端 CI 或生产环境状态。具体数字、首次失败和源码快照只在链接文档维护。

| 原编号 | 已交付范围 | 实现或使用与验收入口 |
| --- | --- | --- |
| 1 | 项目初始化：Python、uv、CLI 与依赖锁 | [项目配置](pyproject.toml) |
| 2 | 模型、数据库与预算配置，私有凭据不入库 | [配置模板](.env.example)、[配置模块](src/db_agent/config.py) |
| 3 | 单个 OpenAI 兼容模型、连接检查与请求预算 | [模型接入](src/db_agent/agent.py)、[提供方验证](docs/delivery.md) |
| 4 | LangChain Agent、领域工具与调用预算 | [Agent](src/db_agent/agent.py)、[工具定义](src/db_agent/tools.py) |
| 5 | 本地 MySQL 只读账号、小表 fixture 与百万订单合成数据 | [Compose](compose.yaml)、[电商数据](docs/ecommerce.md) |
| 6 | 授权基础表的字段、索引与完整声明外键 | [连接器](src/db_agent/db.py) |
| 7 | SQL AST 静态校验、授权对象与支持范围检查 | [静态规则](src/db_agent/policy.py) |
| 8 | 普通 EXPLAIN 与结构化风险诊断 | [分析服务](src/db_agent/analysis.py)、[计划规则](src/db_agent/plans.py) |
| 9 | 执行入口重新预检、只读 SELECT 与有界结果 | [查询服务](src/db_agent/query.py)、[结果处理](src/db_agent/results.py) |
| 10 | 规则、协议与显式数据库测试，脱敏运行记录 | [测试](tests/)、[运行记录](src/db_agent/records.py) |
| 11 | 需求合同、最终复核、确定性查询回答与固定业务验收 | [需求核对](docs/semantic-review.md)、[电商验收](docs/ecommerce.md)、[评测说明](evals/README.md) |
| 12 | CLI 会话内多轮查询与本机 Web 历史 | [会话说明](docs/conversations.md)、[多轮验收](evals/CONVERSATIONS.md)、[Web](docs/web.md) |
| 13 | 锁定构建、Python/数据库/前端回归与 GitHub CI | [交付说明](docs/delivery.md)、[当前工作流](.github/workflows/ci.yml) |
| 14 | 人工确认的本机业务知识，新会话显式引用 | [业务知识](docs/knowledge.md) |
| 15 | 原/候选 SQL 在同一只读快照内核对结果与观测计划 | [优化验证](docs/optimization.md) |
| 16 | 已保存结果的图表、报告与 HTML/JSON 导出 | [结果交付](docs/result-delivery.md) |
| 17 | 本机 Web 多身份、表范围和按用户隔离的历史/知识/结果 | [身份接入与验收](docs/web-access.md) |
| 18 | 独立合成 MySQL 的单行库存变更、审批、回执核对与反向恢复 | [受控变更](docs/changes.md) |
| 19 | MySQL/PostgreSQL 的 searched CASE 与独立 PostgreSQL 受控查询 | [PostgreSQL](docs/postgresql.md)、[模型验收](evals/POSTGRES.md) |

## 仍未覆盖的范围

- **通用 SQL 与跨源分析**：CTE、子查询、UNION、窗口函数等仍未支持；跨源 JOIN/比较尚未实现。后续扩展须分别补充方言、权限、计划和结果验收。
- **通用记忆与 SQL 等价证明**：知识需要人工确认和每轮显式引用；优化比较只说明本次快照完整结果是否一致，不证明所有数据下等价或普遍提速。
- **生产服务与通用写入**：Web 仍限本机单进程；公网部署、SSO、列/行权限、任意 DDL/DELETE 和 PostgreSQL 写入尚未实现。现有库存变更不代表获得其他数据库写权限。
- **提供方运行保证**：已观察到返回 token 计数超过请求上限；硬 token 上限、计费口径、服务器取消仍未验证，见[实测记录](docs/delivery.md#2026-09-11-提供方实测)。客户端超时或停止不能当作服务器已经停止的证明。

上述范围按实际需求另立任务，不作为本轮启动体验改进的前置条件。

## 如何维护

1. 新任务写清当前问题、范围和完成条件；计划、已实现、已验证分别记录。
2. 完成必要验收后再调整状态，并链接具体文档；真实模型、数据库、浏览器合同与当前提交的远端 CI 分开判断。
3. README 只维护启动与常用入口，TODO 维护当前优先级，专题文档维护细节和历史证据，避免三处重复堆积。
