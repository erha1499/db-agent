# db-agent

面向研发人员实际数据库工作流程的 Agent。从 **MySQL SQL 风险预检与诊断** 开始，逐步扩展查询分析、业务记忆和受控数据库变更，以明确授权、受控执行和可核对的结果作为使用要求。

首版采用 **Python + LangChain + OpenAI 兼容模型接口**。使用 LangChain 的 `create_agent` 管理模型和工具协议，减少基础运行时开发，把精力放在数据库业务、执行边界和验证上。

> **当前状态：已提供 SQL 静态预检与普通 EXPLAIN 诊断。** CLI 和 Agent 可以读取授权表结构、预检完整 SQL、获取 MySQL 8.4 的真实计划并返回风险证据。主 Agent 根据报告解释原因并提出候选建议；业务 `SELECT` 执行、结果等价验证和数据库变更尚未实现。

## 快速开始

使用 [uv](https://docs.astral.sh/uv/getting-started/installation/) 管理依赖；项目固定开发 Python 版本为 3.13，具体依赖版本记录在 `uv.lock`。

```bash
git clone https://github.com/erha1499/db-agent.git
cd db-agent
uv sync

cp .env.example .env
# 编辑本地 .env，填写三个模型必填配置后运行
uv run db-agent config
uv run db-agent check
```

`config` 校验模型配置，仅显示三个必填项的状态，不显示值，也不请求模型；`check` 仅发起一次真实模型请求，不连接数据库。完成下方数据库配置后，可以使用 `db` 子命令或带元数据工具的 `chat`；每次 `chat` 创建独立会话，非流式返回回复，不保留聊天记忆。也可以使用 `uv run python -m db_agent` 调用相同命令。

| 配置项 | 用途 | 默认值 |
| --- | --- | --- |
| `DB_AGENT_OPENAI_BASE_URL` | OpenAI 兼容接口的基础 URL | 必填 |
| `DB_AGENT_API_KEY` | 模型接口凭据 | 必填 |
| `DB_AGENT_MODEL` | 模型名称 | 必填 |
| `DB_AGENT_REQUEST_TIMEOUT_SECONDS` | 单次模型请求超时（秒） | `30` |
| `DB_AGENT_RUN_TIMEOUT_SECONDS` | 一次 Agent 运行的模型与工具总时限（秒） | `60` |
| `DB_AGENT_MAX_OUTPUT_TOKENS` | 每次模型响应的输出 token 上限 | `1024` |
| `DB_AGENT_MAX_MODEL_CALLS` | 一次 Agent 运行的模型调用上限 | `4` |
| `DB_AGENT_MAX_TOOL_CALLS` | 一次 Agent 运行的工具调用上限 | `6` |

应用的模型与数据库配置优先读取当前工作目录的 `.env`，未定义的配置项才回退到同名 `DB_AGENT_*` 环境变量，避免旧 shell 配置覆盖本地修改。复制 [.env.example](.env.example) 后，在本地 `.env` 填写真实配置；应用不会执行 shell 配置文件。`.env` 和 `.env.*` 由 Git 忽略，只有占位模板 `.env.example` 纳入版本控制，真实配置不得提交或推送。运行时禁用 LangSmith tracing，避免继承本机 tracing 配置后上传对话。

本地验证入口：

```bash
uv run pytest
uv run ruff check .
git diff --check
```

离线测试验证配置、静态规则、构造计划、连接器边界与 Agent 协议；真实模型连通性单独通过 `check` 验证，数据库连接使用 `db check` 验证。实际诊断链路使用下方 `db analyze` / `chat`，不同层次的验证不能互相替代。

## 本地 MySQL

[compose.yaml](compose.yaml) 提供 MySQL 8.4.11 本地实例，使用 Docker Compose 项目 `db-agent` 和服务 `mysql`。先启动 OrbStack 或其他 Docker 运行环境，在现有 `.env` 中填写两个独立的数据库密码，再从仓库根目录运行：

```bash
docker compose up -d --wait
docker compose ps
```

| 配置项 | 本地值或要求 |
| --- | --- |
| `DB_AGENT_MYSQL_HOST` | `127.0.0.1` |
| `DB_AGENT_MYSQL_PORT` | `13306`；映射到容器 `3306` |
| `DB_AGENT_MYSQL_DATABASE` | `db_agent` |
| `DB_AGENT_MYSQL_USER` | `db_agent_reader` |
| `DB_AGENT_MYSQL_PASSWORD` | 在 `.env` 设置；24–128 位字母、数字、`_` 或 `-` |
| `DB_AGENT_MYSQL_ROOT_PASSWORD` | 在 `.env` 单独设置，仅用于本地管理 |
| `DB_AGENT_MYSQL_ALLOWED_TABLES` | 元数据与 SQL 分析允许访问的表名 JSON 数组，默认 `[]` |
| `DB_AGENT_MYSQL_CONNECT_TIMEOUT_SECONDS` | 连接超时，默认 `3` 秒 |
| `DB_AGENT_MYSQL_METADATA_TIMEOUT_SECONDS` | 单次元数据请求时限，默认 `5` 秒，包含连接器内排队、连接和读取 |
| `DB_AGENT_MYSQL_MAX_METADATA_ROWS` | 单次元数据结果行数上限，默认 `200` |
| `DB_AGENT_MYSQL_MAX_METADATA_BYTES` | 单次元数据结果 JSON 字节上限，默认 `32768` |

服务只绑定本机 `127.0.0.1`，默认容器名为 `db-agent-mysql-1`，数据保存在命名卷 `db-agent_mysql_data`。首次初始化创建 `db_agent` 数据库，并仅为 `db_agent_reader` 授予该数据库的 `SELECT` 和 `SHOW VIEW` 权限。应用按上表连接配置访问数据源；修改主机、数据库名或用户名不会改变 Compose 的固定绑定或初始化对象。Agent 拒绝使用 root，表名白名单还会独立限制可访问的元数据范围。

宿主机的数据库客户端使用上表连接信息，密码取自本地 `.env`。已安装 MySQL CLI 时可交互输入密码连接：

```bash
mysql --protocol=TCP --host=127.0.0.1 --port=13306 --user=db_agent_reader --password db_agent
```

root 仅用于容器内本地 socket 管理，执行以下命令后交互输入 root 密码：

```bash
docker compose exec mysql mysql -uroot -p
```

停止服务并保留数据：

```bash
docker compose stop mysql
```

账号与密码只在空数据卷首次启动时初始化。已有数据卷后修改 `.env` 不会自动修改数据库密码；需要通过 SQL 修改对应账号密码，再同步本地配置。本地实例配置不代表生产部署或数据库业务验收完成。

### 创建合成业务数据与读取结构

数据库启动不会自动创建业务表。管理员可以执行固定的本地初始化脚本：

```bash
uv run python scripts/seed_local_mysql.py
```

[脚本](scripts/seed_local_mysql.py) 先核对 `db-agent/mysql` 容器及 `db_agent` 数据库，再执行固定的[合成业务数据](tests/fixtures/mysql_business.sql)：`customers`、`orders`、`order_items`，包含主键、外键、复合索引，以及取消、退款、零金额、无订单客户等边界数据。任一点名表已存在时，整个初始化停止，不写入或覆盖；重复运行只报告已有表。DDL 中途失败可能保留已建表，脚本不会自动重试或删除。

这是管理员开发脚本，不是 Agent 工具，不接受自定义 SQL 或目标参数。root 凭据只在容器内用于初始化，业务连接使用 reader。接着在本地 `.env` 中明确授权表名：

```dotenv
DB_AGENT_MYSQL_ALLOWED_TABLES='["customers","orders","order_items"]'
```

```bash
uv run db-agent db check
uv run db-agent db tables
uv run db-agent db describe orders
uv run db-agent chat "查看 orders 的字段和索引，说明按 customer_id 和 created_at 查询时可考虑哪些索引。"
```

`db` 子命令不需要模型凭据；`chat` 需要模型与数据库配置，接入 `list_tables`、`describe_table`、`analyze_sql`。白名单为空时不列出任何表，引用未授权表会被拒绝。元数据仅返回基础表的表名、字段与索引，不返回表注释、默认值、外键元数据或业务行；模型根据字段名推断关系时必须说明尚未验证。

### SQL 预检与诊断

```bash
uv run db-agent db analyze 'SELECT id, customer_id FROM orders ORDER BY created_at LIMIT 10'
uv run db-agent chat '分析 SELECT id, customer_id FROM orders ORDER BY created_at LIMIT 10；引用真实结构和计划，区分估算与建议，不做变更。'
```

直接 CLI 由业务代码完成检查与计划取证，返回 JSON；`chat` 由主模型选择工具，读取相同报告后解释问题与建议。`analyze_sql` 内部不调用 LLM；框架层只负责协议、工具白名单、顺序执行和预算，放行规则在领域代码和连接器中。

含敏感字面值的 SQL 可以通过 `db analyze --stdin` 输入，避免写入命令参数或 shell 历史；输入为受长度限制的 UTF-8。直接 CLI 不向模型发送 SQL。`chat` 会把问题、工具调用以及脱敏计划摘要发送给配置的模型，因此只提交允许进入该模型上下文的内容。

当前支持单条完整 `SELECT`、单表和带 `ON` 的显式 `INNER` / `LEFT JOIN`、基础比较与算术、分组、排序、`LIMIT`，以及 `COUNT` / `SUM` / `AVG` / `MIN` / `MAX`。函数名必须紧接左括号，不能加反引号或数据库限定。CTE、子查询、UNION、窗口函数、DISTINCT、无表来源、未绑定占位符等返回 `UNKNOWN`；写操作、越界表、文件操作、锁定读取和非批准函数返回 `BLOCK`。实际注释与提示、未支持的标识符及语法明确拒绝；字符串中的标记按词法区分。使用 MySQL 方言 AST 和节点/参数白名单，不以解析成功当作安全证明，也不改写原 SQL 后冒充原计划。

`explain_checked` 自身每次重新预检，通过后验证当前库、SQL 模式、基础表类型和 MySQL 8.4，固定 JSON 计划版本 1，仅读取 `EXPLAIN FORMAT=JSON`。新版本、其他 SQL 模式或无法识别的计划进入 `UNKNOWN`。计划保留访问路径、索引、估算扫描/产出行数和排序证据；不回传原始条件、字面值或完整计划。单次扫描估算超阈值、连接阶段产出过大、规模较大的排序/临时表返回 `REVIEW`；缺少关键证据不按零处理，小表全扫不机械阻断，`LIMIT` 不豁免大扫描。

| `DB_AGENT_ANALYSIS_` 后缀 | 默认值 | 用途 |
| --- | --- | --- |
| `MAX_SQL_BYTES` / `MAX_AST_NODES` / `MAX_AST_DEPTH` | `16384` / `512` / `32` | SQL 输入与解析复杂度预算 |
| `MAX_TABLES` | `8` | 单条 SQL 的表引用上限 |
| `MAX_PLAN_BYTES` / `MAX_PLAN_NODES` | `65536` / `256` | 计划字节与 JSON 值节点预算 |
| `TIMEOUT_SECONDS` | `10` | 单次诊断预算，包含连接器排队、连接与读取 |
| `REVIEW_SCAN_ROWS` / `REVIEW_JOIN_ROWS` / `REVIEW_SORT_ROWS` | `100000` / `1000000` / `100000` | 估算行数审核阈值，严格超过时命中 |

阈值是初始策略，需结合目标环境校准；不是实测耗时或生产安全保证。报告额外有固定 8 KiB 元数据预算。每个报告包含 `report_id`、策略版本、时间、数据库、结构指纹、规则与证据；结构指纹去掉字面值，仅用于关联同形 SQL，不是参数绑定凭证或执行授权。基础表校验与计划采集不是整个数据库的原子快照。超时关闭连接并停止等待，不能据此声称服务器已取消查询。

四种 `decision` 都会返回报告，均不执行业务 SQL。CLI 成功生成报告时退出码为 `0`，包括 `BLOCK` / `REVIEW` / `UNKNOWN`；配置错误为 `2`、报告外运行失败为 `1`，脚本必须读取 `decision`。业务拒绝也会正常回填给主模型解释。

合成数据与白名单准备好后，显式运行真实 MySQL 集成测试：

```bash
DB_AGENT_MYSQL_INTEGRATION=1 uv run pytest tests/test_mysql_integration.py -q
```

普通 `uv run pytest` 会跳过这些集成测试，避免隐式依赖数据库。集成测试不创建数据，验证真实元数据、账号权限和普通 EXPLAIN；固定 INSERT 权限探针使用零行条件，并在事务结束时回滚。驱动采用 `aiomysql[rsa]`，包含本地 MySQL 密码认证所需的 RSA 支持。

每个连接器顺序处理请求，超过时间、行数或字节预算会返回错误并清理连接。`check`、`chat` 和 `db` 命令在 `outputs/runs/<uuid>.jsonl` 写入运行、模型、工具、数据库和分析事件，记录状态、错误码、耗时、报告 ID、决策、策略版本、规则 ID 与结构指纹。记录不包含问题原文、密码、SQL、工具参数或结果正文；工具调用 ID 只保存摘要。记录失败会在 stderr 提示一次并继续业务，这些记录不承担审批账本职责。

## 要解决的业务问题

研发人员在使用数据库时，通常需要理解库表、编写查询、排查 SQL 问题，以及核对数据。Agent 可以帮助串联这些动作，但自动生成的 SQL 不能未经校验直接执行。

本项目首先回答三个问题：

1. **能否访问？** 当前用户是否有权在目标数据源、库表上执行这类操作。
2. **是否适合执行？** SQL 是否包含禁止的操作，执行计划是否提示大量扫描、连接放大或昂贵排序。
3. **证据是什么？** 展示实际结构、索引、执行计划、规则命中和工具结果，区分观察、估算与建议。

数据库总容量只是背景。风险判断应落到具体访问对象、查询计划和执行限制上；一个小表的全表扫描可能合理，带 `LIMIT` 的聚合查询也可能消耗大量资源。

## 首版业务目标

| 能力 | 首版做到什么程度 |
| --- | --- |
| 数据源与元数据 | 明确授权的 MySQL 数据源，按授权获取相关表结构、字段及索引 |
| 权限校验 | 由服务端绑定身份，检查环境、数据源、库表和操作类型；配合最小权限数据库账号 |
| SQL 静态检查 | 基于 MySQL 方言的语法树限定单语句和支持的语法，识别越界访问与危险操作 |
| 执行计划分析 | 对通过前置检查的查询获取普通 `EXPLAIN FORMAT=JSON`，结合规模统计和策略阈值输出风险证据 |
| SQL 诊断 | 接收已有 SQL，解释计划和错误，提出候选改写或索引建议；建议不自动成为变更 |
| 受控查询 | 仅执行支持且通过全部检查的只读 `SELECT`，限制时间、返回行数、结果大小与并发 |
| Agent 工具运行 | 通过 LangChain 接入领域工具；一个模型、顺序调用、参数校验、有限轮数和总预算 |
| 运行记录 | 关联任务、模型轮次和工具调用，记录脱敏的参数摘要、耗时、错误及结果引用 |

首版以 CLI 或极简入口为主。暂不执行 DML/DDL；如果加入 `UPDATE`、`DELETE` 等检查样本，只做静态审核与风险预览。完整审批流程也不属于首版：需要审核的请求先停止并返回报告。

不在近期范围内：多数据库适配、集群运维、数据库迁移、备份恢复、自动建索引、生产自愈、完整 BI、多 Agent 调度、通用文件/终端工具、Git 管理、人员与待办管理。

## 预期业务流程

```mermaid
flowchart TD
    A[用户提出数据库任务或提交 SQL] --> B[Agent 选择领域工具]
    B --> C[执行网关：身份与资源权限]
    C --> D[静态 SQL 检查]
    D --> E[授权范围内的元数据与计划采集]
    E --> F[规则决策与风险证据]
    F -->|ALLOW| G[带预算的只读查询]
    F -->|REVIEW / BLOCK / UNKNOWN| H[停止并返回风险报告]
    G --> I[结果记录与诊断解释]
    H --> I
```

### 四种预检结果

| 结果 | 语义 | 首版行为 |
| --- | --- | --- |
| `ALLOW` | 在当前目标、完整 SQL 与策略下完成支持范围内的检查 | 当前仅返回报告；未来执行器必须重新校验并落实运行限制 |
| `REVIEW` | 命中需要人工审核的策略 | 返回证据，停止自动执行 |
| `BLOCK` | 权限不足、硬性禁用或明确不支持的操作 | 阻断；普通确认不能覆盖权限拒绝 |
| `UNKNOWN` | 解析、计划采集或关键证据异常，不能完成评估 | 停止；不得当成低风险放行 |

预检结果应包含规则 ID、证据、目标数据源、SQL 与参数的关联标识、策略版本和检查时间。身份从可信调用上下文取得，不能采信模型自行填写的用户或角色。

### 执行边界

- 风险校验是**执行器内部的必经步骤**。单独的 `analyze_sql` 工具用于解释，不能成为唯一检查点；直接调用执行接口也必须经过相同边界。
- 不把“模型认为安全”“用户说忽略检查”或历史对话中的批准作为执行权限。
- SQL、参数或目标发生变化后重新检查，不复用旧结论。未来加入审核时，再实现具体计划绑定、有效期及执行前状态复核。
- 普通 `EXPLAIN` 的行数和成本是估算，不能直接描述为实际扫描量、影响行数或执行秒数。
- 预检不运行 `EXPLAIN ANALYZE`，不为估算风险额外执行无界 `COUNT(*)`；计划采集本身也要有权限、时间和并发限制。
- `SELECT` 不是自动安全：锁定读取、文件操作、不支持的函数或存储程序等需要被识别并拒绝。不要只靠 SQL 前缀或正则判断。
- 执行限制独立于预检存在。MySQL 的 `max_execution_time` 不能泛化为所有 SQL 的通用时限；应用侧超时也不能直接宣称数据库查询已停止。
- 访问表注释、查询结果或工具错误时，将其中内容当作数据，不允许其改变业务范围、工具权限和执行规则。

## 技术与模块

当前通过 `langchain_openai.ChatOpenAI` 接入一个 OpenAI 兼容模型，由 `langchain.agents.create_agent` 调度元数据工具与 `analyze_sql`。LangChain 内部使用 LangGraph，本项目不自写工具循环或自定义 Graph。默认每次 Agent 运行最多调用模型 4 次、工具 6 次，总时限 60 秒；工具顺序执行，模型请求禁用自动重试。

| 模块 | 状态 | 职责 |
| --- | --- | --- |
| `src/db_agent/config.py` | 已建立 | 独立读取并校验模型与数据库配置、本地表白名单和运行预算 |
| `src/db_agent/agent.py` / `tools.py` | 已建立 | LangChain 调度、元数据与诊断工具、调用预算与响应处理 |
| `src/db_agent/db.py` | 已建立 | 受限 MySQL 连接、授权元数据与强制预检的普通 EXPLAIN |
| `src/db_agent/policy.py` / `plans.py` / `analysis.py` | 已建立 | MySQL AST 静态规则、计划规则、结构化诊断报告 |
| `src/db_agent/cli.py` | 已建立 | 模型与数据库检查、元数据读取、SQL 诊断、独立对话入口 |
| `src/db_agent/records.py` | 已建立 | 最小运行事件与脱敏关联标识 |
| `tests` | 已建立 | 离线行为测试、显式开启的 MySQL 集成测试与公开合成数据 fixture |
| `compose.yaml` / `infra/mysql/init` | 已建立 | 本地 MySQL、持久化数据卷及只读账号初始化 |
| `scripts/seed_local_mysql.py` | 已建立 | 管理员显式创建固定合成业务表与数据 |
| 受控查询执行 | 计划 | 强制重新预检的业务 SELECT、结果大小限制与结果等价验证 |
| `evals` | 计划 | 冻结样本上的模型任务评测 |

当前本地 MySQL 配置使用 8.4.11，镜像引用以 `compose.yaml` 为准。连接器的表白名单来自本地可信配置，不等于多用户身份与资源授权服务；后续业务查询执行仍需独立实现与验收。领域规则和连接器保持框架无关，框架负责调度，确定性代码负责授权与执行判断。实际遇到跨进程审批、持久恢复或复杂编排需求时，再扩展相应能力。

## 近期交付与验收

目标是分阶段完成可实际使用的 SQL 预检与诊断业务闭环。每个阶段以真实工具和可复现验收为交付条件，优先完成一条受控链路，再扩大功能范围。接入具体使用环境前，还需核对目标版本、账号权限、运行限制与业务结果；当前支持预检和计划诊断，不具备业务 SQL 执行能力。

| 阶段 | 交付 | 验收 |
| --- | --- | --- |
| 第一阶段：规则与工具 | 隔离测试环境中的 MySQL 数据源、元数据工具、权限与静态检查、普通 EXPLAIN | 不依赖模型也能执行规则和工具测试 |
| 第二阶段：业务闭环 | LangChain 领域工具接入、强制预检入口、诊断报告与轨迹 | 通过全部检查的查询执行；越权、高风险及无法判断请求停止；结果可追踪 |
| 第三阶段：使用验收 | 启动说明、固定案例、目标环境配置说明和测试记录 | 在明确授权的数据源上按文档复现；每项能力与限制有对应证据 |

当前已验证的预检/诊断行为与尚待查询执行阶段验收的行为：

- [x] 当前配置范围内，授权与未授权表请求行为不同，直接调用计划连接器也要重新预检。
- [x] 真实小表扫描不被机械阻断；构造的大扫描计划即使 SQL 带 `LIMIT` 也返回审核。
- [x] 多语句、越界对象、锁定查询和不支持的 SQL 不进入 EXPLAIN。
- [x] 计划获取失败、证据不足进入 `UNKNOWN`，不自动放行。
- [x] SQL 字面值或连接器目标/授权变化后重新校验与取计划，不缓存授权。
- [x] 调用次数、诊断时限、计划大小有行为测试，错误可追溯。
- [x] 真实模型调用元数据和 EXPLAIN 工具，基于报告解释估算及建议边界。
- [ ] 候选查询若参与结果等价比较，也通过相同执行入口，测试集包含边界数据。

模型评测与规则单测分开：规则测试不依赖模型；当前真实 MySQL 用合成数据验证元数据、权限和计划，不验证业务查询执行；本次真实模型调用只是链路检查，不代表冻结测试集上的任务正确率。后续模型评测记录模型版本、提示/规则版本、数据初态、样本与失败原因，预先划分开发样本和冻结样本，必要时重复运行。

大规模计划 fixture 只用于规则边界测试，应标注为构造或脱敏样本。本地小库测试不等于 TB 级压测；当前没有生产指标、准确率或性能提升结论。

## 后续业务路线

按需求逐步推进，不同时铺开：

1. **业务记忆与上下文复用**：经确认的指标口径、表关系和 SQL 模板；按项目/数据源隔离，带来源、版本和失效处理。
2. **Data Agent**：自然语言查数、多轮追问、趋势与分组对比，解释结果并提供来源；先保证业务口径正确，再扩展图表和报告。
3. **受控数据变更**：从限定主键与列的小批量 UPDATE 开始，加入前后值预览、计划确认、并发前置检查、事务内执行凭证与提交结果核对。
4. **数据库管理扩展**：结构变更审核、索引方案验证、锁等待诊断，之后再评估更多数据库与运行时。

记忆不保存可跨任务复用的执行授权。数据库变更和应用任务记录之间的恢复语义需要单独设计；不能将聊天恢复当成事务恢复，也不能把 DDL 当作可普遍事务回滚的操作。

## 公开资料与成果边界

本项目面向实际数据库使用场景开发；公开测试资料使用合成数据和通用数据库场景。仓库不包含任何组织的内部代码、文档、聊天记录、生产连接信息或用户数据。

保留可复现代码、测试与原始验证记录，分别说明规划、已实现能力、测试结果和部署状态。能力说明必须对应代码与证据；测试替身、合成数据评测和真实环境结果分别标注，不把测试结果写成线上效果，也不预填提升百分比。

可公开的最小、脱敏 fixture 放入版本控制；运行日志、查询结果和环境文件保留在被忽略的本地目录。[AGENTS.md](AGENTS.md) 是开发约定，不应自动作为本产品向终端用户加载的业务知识。

## 参考资料

- [LangChain：快速开始](https://docs.langchain.com/oss/python/langchain/quickstart)
- [LangChain：ChatOpenAI 接入](https://docs.langchain.com/oss/python/integrations/chat/openai)
- [aiomysql：连接 API](https://aiomysql.readthedocs.io/en/stable/connection.html)
- [MySQL 8.4：EXPLAIN](https://dev.mysql.com/doc/refman/8.4/en/explain.html)
- [MySQL 8.4：执行计划输出](https://dev.mysql.com/doc/refman/8.4/en/explain-output.html)
- [SQLGlot：解析与限制](https://sqlglot.com/sqlglot.html#faq)
- [MySQL 8.4：LIMIT 优化](https://dev.mysql.com/doc/refman/8.4/en/limit-optimization.html)
- [MySQL 8.4：执行时间提示](https://dev.mysql.com/doc/refman/8.4/en/optimizer-hints.html#optimizer-hints-execution-time)

具体实现前应核对所用依赖和 MySQL 版本对应的文档。借鉴或复用外部代码时保留适用的许可证与来源说明。
