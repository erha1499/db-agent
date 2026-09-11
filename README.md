# db-agent

用 SQL 或自然语言查询数据库、检查 SQL 风险，并在本机网页查看、分析和导出结果。基于 Python + LangChain，支持 MySQL / PostgreSQL；查询执行前由程序重新校验权限、SQL 和执行计划。

**第一次使用，按下面的本地 MySQL 路径启动：先查到示例数据，再打开网页，最后按需接入模型。** 只用 SQL 不需要模型 API Key。已有环境直接看[以后每次启动](#以后每次启动)；项目进度看 [TODO](TODO.md)。

## 快速开始

以下命令均在仓库根目录执行。准备好：

- **uv**：安装 Python 与项目依赖，开发版本由 `.python-version` 固定为 Python 3.13。
- **Docker 和 Docker Compose**：先启动 Docker Desktop、OrbStack 等 Docker 运行环境。
- **Node.js / npm**：只有网页需要；项目 CI 使用 Node 26.7.0 / npm 11.19.0。

### 安装依赖

```bash
git clone https://github.com/erha1499/db-agent.git
cd db-agent
uv sync --locked
# 已有 .env 时保留它
test -f .env || cp .env.example .env
chmod 600 .env
```

### 本地 MySQL

首次使用只需在 `.env` 中填写下面 **两个不同的密码**，其余数据库配置保持模板值。建议两者均使用 24–128 位字母、数字、`_` 或 `-`；这是 reader 密码的初始化要求。模型的三个占位项暂时不用改。

| 必填项 | 用途 |
| --- | --- |
| `DB_AGENT_MYSQL_PASSWORD` | 程序连接数据库的只读账号密码 |
| `DB_AGENT_MYSQL_ROOT_PASSWORD` | 仅供本地容器初始化和管理员使用 |

模板已经配置好 `127.0.0.1:13306/db_agent`、账号 `db_agent_reader`，以及允许访问的三张示例表：

```dotenv
DB_AGENT_DATABASE_KIND=mysql
DB_AGENT_MYSQL_ALLOWED_TABLES=["customers","orders","order_items"]
```

启动数据库，并在**首次使用、三张示例表都不存在时**初始化数据：

```bash
docker compose up -d --wait
uv run python scripts/seed_local_mysql.py
```

看到“已在 db-agent/mysql 的 db_agent 中创建 customers、orders、order_items 合成数据”即初始化完成。数据库启动本身不会建业务表；任一示例表已存在时，脚本会停止且不覆盖。已有完整数据直接进入下一步；若曾初始化中断，先检查已有表，脚本不会自动补齐。

配置由程序读取，无需 `source .env`。应用优先采用当前目录 `.env`，同名环境变量仅作缺省回退；Docker Compose 对同名环境变量的优先级不同，启动前应清除终端中与 `.env` 冲突的旧配置。已有数据卷时，改 `.env` **不会修改数据库内的密码**：需先由管理员改数据库密码，再同步配置，不通过删除数据卷解决。

### 运行第一条查询

```bash
uv run db-agent db check
uv run db-agent db tables
uv run db-agent db query "SELECT COUNT(*) AS order_count, SUM(total_amount) AS total FROM orders WHERE status = 'paid'"
```

这三条命令均不调用模型。固定[示例数据](tests/fixtures/mysql_business.sql)的最后一条查询应返回：

```json
[[3, "130.00"]]
```

这是响应中 `result.rows` 的内容，表示 3 笔已支付订单、合计 130.00；金额用字符串保留精度。完整成功还应同时看到 `status: "ok"`、`decision: "ALLOW"`、`execution_status: "completed"` 和 `result.truncated: false`。

## 打开本地 Web 页面

先完成上面的数据库配置和首条查询，再执行一次：

```bash
uv run python scripts/manage_web_users.py set alice --tables customers orders order_items
npm --prefix frontend ci --registry=https://registry.npmjs.org
npm --prefix frontend run build
uv run db-agent web
```

第一条命令会要求输入两次 **12–128 字符的网页登录密码**，输入时不显示。它与数据库密码独立，脚本会创建私有身份文件 `outputs/web/identities.json`。重新对同一用户运行 `set` 会替换密码和完整权限；日常启动无需重复执行。

打开 [http://127.0.0.1:8000](http://127.0.0.1:8000)，使用 `alice` 和刚设置的密码登录，选择 **直接查询 SQL**，粘贴上面的 SQL。此账号可查看三张示例表、执行受控 SQL 查询和诊断；不调用模型。查询完成后可查看结果表格，在“分析与交付”中生成图表、下载 HTML 报告或 JSON。

Web 固定监听本机，当前为单进程入口。Python wheel 不包含已构建页面，源码启动需要上面的前端构建。更多使用方式见 [Web 说明](docs/web.md)和[身份管理](docs/web-access.md)。

## 启用自然语言查询（可选）

在 `.env` 中替换模型的三个占位值，使用支持工具调用的 OpenAI 兼容接口：

```dotenv
DB_AGENT_OPENAI_BASE_URL=https://your-provider.example/v1
DB_AGENT_API_KEY=replace-with-your-api-key
DB_AGENT_MODEL=replace-with-your-model
```

先核对配置，再检查模型连接，最后查询数据库：

```bash
uv run db-agent config
uv run db-agent check
uv run db-agent chat '查询 orders 表中 status 为 paid 的订单数量和 total_amount 合计。'
# 需要连续追问时使用：
uv run db-agent session
```

`config` 只校验格式、隐藏配置值，模板占位值也可能通过；`check` 才会真实调用一次模型，且不检查数据库。`chat` 每次独立运行；`session` 在内存中支持多轮，退出清空，失败或结果不完整后需 `/reset` 并完整重述。

如果也要在网页使用“智能查询”，为 `alice` 开启模型权限，然后重启 Web 并重新登录：

```bash
uv run python scripts/manage_web_users.py set alice \
  --tables customers orders order_items --allow-model \
  --model-tables customers orders order_items
# 在原 Web 终端按 Ctrl+C 后启动
uv run db-agent web
```

此命令会重新设置登录密码与权限。`--tables` 必须在数据库白名单内，`--model-tables` 必须在用户表权限内。问题、SQL、授权结构和计划摘要会发送给配置模型；查询返回行由程序直接展示，不再回传模型。更完整的模型核对方式见[交付说明](docs/delivery.md)。

## 以后每次启动

在已配置过的仓库根目录运行，保持 Web 终端开启：

```bash
docker compose up -d --wait
uv run db-agent web
```

打开 [http://127.0.0.1:8000](http://127.0.0.1:8000) 并登录即可。只用 CLI 时无需启动 Web，直接运行 `uv run db-agent db query '…'` 或 `chat`。

- **更新代码后**：运行 `uv sync --locked`；前端依赖或源码更新后再执行 `npm --prefix frontend ci --registry=https://registry.npmjs.org` 和 `npm --prefix frontend run build`。
- **停止 Web**：在对应终端按 `Ctrl+C`。
- **停止数据库并保留数据**：`docker compose stop mysql`。数据保存在 `db-agent_mysql_data` 卷中。
- **修改配置后**：重启 Web 并重新登录。配置变化会隔离旧范围的历史、结果和知识，恢复旧配置也不会让旧范围重新可用。

## 常用命令与启动排错

| 想做什么 | 命令 |
| --- | --- |
| 检查数据库 / 模型连接 | `uv run db-agent db check` / `uv run db-agent check` |
| 查看允许的表 / 表结构 | `uv run db-agent db tables` / `uv run db-agent db describe orders` |
| 只诊断，不执行 SELECT | `uv run db-agent db analyze 'SELECT id FROM orders LIMIT 5'` |
| 执行受控查询 | `uv run db-agent db query 'SELECT id FROM orders ORDER BY id LIMIT 5'` |
| 从标准输入读取 SQL | `uv run db-agent db query --stdin`（`analyze` 同样支持） |
| 查看完整 CLI 参数 | `uv run db-agent --help` / `uv run db-agent db --help` |

| 现象 | 检查与处理 |
| --- | --- |
| `uv` / `docker` / `npm` 找不到 | 先安装对应工具；`npm` 仅网页需要，Docker 还需启动运行环境 |
| 数据库连接失败 | 用 `docker compose ps` 确认 `mysql` 为 healthy，再核对 `.env` 的端口和 reader 密码；已有卷按上文改密规则处理 |
| 查不到表或未授权 | 检查是否完成示例数据初始化、数据库白名单是否包含目标表；Web 用户还需有对应表权限 |
| 网页提示先构建前端 | 执行上面的 `npm ci` 与 `npm run build`，再重新打开页面 |
| 身份文件缺失或无效 | 用管理脚本创建身份；文件默认位于 `outputs/web/identities.json`，权限须为 `600`，用户表权限不得超出数据库白名单 |
| 网页不能使用智能查询 | 先用 `check` 验证真实模型，再核对用户的 `--allow-model` / `--model-tables`；重启 Web 并重新登录 |
| 8000 端口被占用 | 用 `uv run db-agent web --port 8001`，打开 `http://127.0.0.1:8001` |

## 能力与详细文档

| 能力 | 当前范围与入口 |
| --- | --- |
| 查询与需求核对 | 授权元数据、普通 EXPLAIN、受控 SELECT；[自然语言需求核对](docs/semantic-review.md) |
| 多轮查询与本地网页 | [CLI 会话](docs/conversations.md)、[Web 使用](docs/web.md)、[登录与表权限](docs/web-access.md) |
| 结果分析与导出 | 对已保存结果做分组、趋势、HTML/JSON 导出，不再次查库；[结果交付](docs/result-delivery.md) |
| 跨会话业务知识 | 人工创建、确认、显式引用与撤销；[业务知识](docs/knowledge.md) |
| SQL 优化核对 | `db compare` 在同一只读快照比较原/候选 SQL；结果一致只代表该快照；[优化验证](docs/optimization.md) |
| PostgreSQL | 独立本地实例与 reader，可信配置切换数据源；[配置与限制](docs/postgresql.md) |
| 受控变更 | 默认关闭；仅独立合成 MySQL 的单行库存调整，Web 人工审批与恢复；[独立准备流程](docs/changes.md) |
| 大规模示例与评测 | 百万订单合成数据及固定业务案例；[电商数据](docs/ecommerce.md)、[评测索引](evals/README.md) |

查询路径仅执行支持且通过全部检查的只读 SELECT；`BLOCK`、`REVIEW`、`UNKNOWN` 均停止。支持基础聚合、显式 INNER/LEFT JOIN 和 searched `CASE WHEN`，CTE、子查询、UNION、窗口函数、DISTINCT 等仍未支持。普通 EXPLAIN 是估算，`ALLOW` 不等于查询完成；截断结果不是完整集合。

默认每轮模型最多 4 次、工具 6 次、总时限 60 秒；查询最多返回 100 行 / 32768 字节。完整可调项见 [.env.example](.env.example)。当前未证明提供方遵守 token 硬上限，应用超时也不代表服务器已确认取消。公网/生产接入、SSO、列/行权限、跨源 JOIN、任意 DDL/DELETE 与 PostgreSQL 写入尚未提供。

## 开发与验证

```bash
uv run --locked pytest -q
uv run --locked ruff check .
git diff --check
```

默认 pytest 包含替身测试，真实数据库测试需显式开关；真实模型验收单独运行。前端构建、浏览器合同、MySQL/PostgreSQL 集成与打包入口见[交付说明](docs/delivery.md)及各功能文档，最新 CI 看 [GitHub Actions](https://github.com/erha1499/db-agent/actions)。

开发约定见 [AGENTS.md](AGENTS.md)，当前进度和下一步见 [TODO.md](TODO.md)。真实配置 `.env`、Web 历史/结果 `outputs/web`、业务知识 `outputs/knowledge` 和运行记录 `outputs/runs` 均留在本机，不提交；Web 历史包含问题、SQL 与结果，运行记录则不记录这些正文。公开仓库只使用合成数据，测试和历史验收不代表生产效果。
