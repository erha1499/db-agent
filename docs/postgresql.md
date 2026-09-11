# PostgreSQL 与条件聚合

当前可从可信服务端配置选择 MySQL 或 PostgreSQL；一次进程只选一个数据源。
PostgreSQL 接入覆盖授权元数据、静态预检、普通 EXPLAIN、受控 SELECT、自然语言
查询、知识与结果范围、原/候选 SQL 同快照比较。写入不属于本数据源的能力。

## 从本机合成目标开始

独立环境固定为 PostgreSQL 18.6、`db-agent-postgres/postgres`、
`127.0.0.1:15432/db_agent_pg`、`business` schema。
[初始化说明](../infra/postgres/README.md)包含角色、fixture、镜像摘要和保留数据的停止方法。
它不复用 MySQL 容器、数据卷、管理账号或 reader 密码。

```bash
uv sync --locked
uv run python scripts/setup_postgres.py --apply
uv run python scripts/setup_postgres.py --verify-only
```

脚本将两个随机密码保存在忽略的 `.env.postgres`（0600），已有配置不覆盖；
再次 `--apply` 遇到已存在表即停止，不补写、不重建。日常核对用 `--verify-only`。
管理员凭据仅供这个固定合成目标的初始化和隔离测试，不能填入 Agent reader 配置。

应用仍按当前目录的 `.env` 优先、同名环境变量回退读取，**不会自动读取
`.env.postgres`**。将下面的公开配置以及 `.env.postgres` 中的 reader 密码填入
自己的 `.env`，保留已明确的模型配置。不要复制管理员密码到应用配置：

```dotenv
DB_AGENT_DATABASE_KIND=postgresql
DB_AGENT_POSTGRES_HOST=127.0.0.1
DB_AGENT_POSTGRES_PORT=15432
DB_AGENT_POSTGRES_DATABASE=db_agent_pg
DB_AGENT_POSTGRES_SCHEMA=business
DB_AGENT_POSTGRES_USER=db_agent_reader
DB_AGENT_POSTGRES_PASSWORD=<仅本机填写reader密码>
DB_AGENT_POSTGRES_ALLOWED_TABLES='["customers","orders","order_items"]'
```

`DB_AGENT_DATABASE_KIND` 默认为 `mysql`；未知值报配置错误。选择 PostgreSQL 时
不会回退使用 MySQL 的密码。`DB_AGENT_POSTGRES_CONNECT_TIMEOUT_SECONDS`、
`METADATA_TIMEOUT_SECONDS`、`MAX_METADATA_ROWS`、`MAX_METADATA_BYTES` 的默认值
分别为 3秒、5秒、200行、32768字节。分析与查询仍使用原独立
`DB_AGENT_ANALYSIS_*` / `DB_AGENT_QUERY_*` 预算，不增加模型调用或工具次数。

```bash
uv run db-agent db check
uv run db-agent db tables
uv run db-agent db describe orders
uv run db-agent db query "SELECT SUM(CASE WHEN status = 'paid' THEN total_amount ELSE 0 END) AS paid_amount FROM orders"
uv run db-agent chat '查询 orders 中已支付订单的笔数和金额，status=paid 表示已支付，金额字段是 total_amount，用 CASE 条件聚合。'
```

fixture 中已支付订单为3笔、130.00；金额由程序返回为字符串。直接 `db` CLI
不调用模型；Agent 按实际结构生成 SQL、独立提取合同、最终复核，再进入相同
确定性连接器。查询返回行不再发送给模型。

## 新 SQL 能力及边界反例

两个方言都增加 **searched CASE**：依次判断 WHEN 条件，匹配后选择 THEN 值；
不匹配走 ELSE，没有 ELSE 则返回 NULL。它可以出现在投影或批准的基础聚合中。

```sql
SELECT
  COUNT(CASE WHEN status = 'paid' THEN 1 END) AS paid_count,
  SUM(CASE WHEN status = 'paid' THEN total_amount ELSE 0 END) AS paid_amount
FROM orders
```

`COUNT(CASE ... THEN 1 END)` 只统计匹配项；加上 `ELSE 0` 后，零也是非NULL值，
COUNT 会统计每一行。在6条订单 fixture 中两者分别是3和6，不能认为等价。
无匹配的 `SUM(CASE ... THEN total_amount END)` 返回NULL；明确 `ELSE 0` 才返回0。
LEFT JOIN 保留没有订单的客户时仍须区分无匹配、零金额和退款/取消状态。
这些预期独立来自公开 fixture，不以再运行一次同SQL当成 oracle。

仍不支持 simple CASE、IF/COALESCE 等其他函数、CTE、子查询、集合运算、窗口和
DISTINCT。CASE 内嵌危险函数、子查询、锁或越权对象不会得到特例放行。
PostgreSQL 使用至多63字节的 ASCII 标识符，未加引号名称折成小写，双引号名称
精确匹配；反引号、显式类型转换、占位符及可能被解析器丢弃含义的语法停止。
系统列（例如 ctid、xmin、tableoid）不在本子集内。

PostgreSQL 整数除法和默认NULL排序与 MySQL 不同，合同编译按可信方言处理。
自然语言合同目前只有升/降序，没有独立NULL位置字段；要求非默认NULL位置的
自然语言任务应停止，不能悄悄改义。直接SQL可使用已验证的NULLS FIRST/LAST。

## PostgreSQL 执行证据

每次连接以实际 `current_user` / `session_user`、目标数据库和版本核对配置。
拒绝超级用户、角色成员、建库/建角色/复制/RLS绕过权限、数据库 CREATE/TEMP、
schema CREATE，以及目标表的写入/管理权限和所有权。账号只能读取批准的表，
表名配置本身不能授予数据库权限。冲突的 libpq 服务、hostaddr、options 配置报错，
不能静默替换可信目标。

当前对象限定为普通、永久 heap 表，内建标量字段和简单内建 btree 索引。
视图、外部表、RLS、继承/分区、生成列、自定义类型、表达式/部分/未就绪索引、
表达式扩展统计均拒绝。业务schema中的自定义函数/运算符不受支持。
这些限制也作用于普通 EXPLAIN 前，避免规划期执行未经纳入范围的表达式。
`constraint_exclusion=off` 固定并复核，阻断CHECK约束在规划中的常量求值。
数据库管理员及系统目录完整性仍是可信环境条件，不承诺抵抗恶意超级用户。

查询在同一连接显式 READ COMMITTED READ ONLY 事务中完成：对象验证 →
仅点名表的 ACCESS SHARE 锁 → OID与属性复查 → 普通
`EXPLAIN (FORMAT JSON, VERBOSE TRUE, ANALYZE FALSE) DECLARE ... NO SCROLL CURSOR`
→ 计划规则 → 对象、身份和只否决钩子复核 → 相同模式的服务器游标。
ACCESS SHARE 不锁业务行，避免验证后的普通表被并发替换；不阻止所有管理员
目录/权限变更。会话固定UTC、ISO日期、安全search_path、禁并行与JIT；
statement_timeout、lock_timeout 与应用总时限共同限制等待。

计划解析只接受验证过的节点和完整估计。Seq Scan 使用可信关系行数统计估计
扫描量；过滤后的 Plan Rows 不是全表扫描行数。缺统计/未知节点返回UNKNOWN，
超过原扫描/连接/排序阈值返回REVIEW；LIMIT不能豁免。统计可能陈旧，成本不是秒数，
也没有按这个小样本宣称生产性能。

业务结果由服务器游标逐行读取，保留重名列位置、Decimal与超大整数字符串，
无时区timestamp不凭空添加时区，timestamptz按UTC返回。支持内建text且受结果
JSON字节预算约束；该预算不限制单字段的驱动分配或数据库扫描内存。JSON、数组、
bytea、domain、enum等未支持类型拒绝。只有观察到EOF才返回completed；截断、
取消或错误关闭连接，不排空、不重连补跑，不宣称服务端语句已经确认取消。

## 知识、历史、比较与Web身份

数据源范围摘要包含数据库种类、主机/端口、数据库、schema、账号和完整白名单；
Web额外绑定登录身份和持久化授权代际，模型可见表是身份表范围的子集。
CLI与Web均通过可信工厂构造对应连接器；PostgreSQL保留每次操作、模型HTTP前、
派发前和读取后授权否决，不能通过新adapter跳过旧边界。

切换数据源、schema或权限后不能取回旧源知识/历史/结果ID。此版本扩充scope
摘要格式，旧版范围下的历史不会自动迁移或复活；需要新建会话并重新确认知识。
知识不是执行凭证，生命周期和结构依旧在实际查询事务中复核。

`db compare` 对两条SQL各做完整检查，在同一个PostgreSQL REPEATABLE READ
只读快照内比较完整结果。报告标记 `same_readonly_postgres_snapshot`；它不与
MySQL结果混比，不宣称通用SQL等价。任一截断、失败、权限变化或未知计划均不确认相同。
该能力与生产写入、通用数据库适配、跨数据库JOIN都无关。

## 可复现验证

```bash
DB_AGENT_POSTGRES_INTEGRATION=1 uv run pytest tests/test_postgres_integration.py tests/test_postgres_services.py -q
DB_AGENT_POSTGRES_DDL_INTEGRATION=1 uv run pytest tests/test_postgres_isolation.py -q
uv run python scripts/evaluate_postgres.py --run --repeat 1
```

前两个入口是实际PostgreSQL；隔离测试仅创建和清理独有随机探针，原三张fixture
不变。HTTP合同测试的模型替身会单独标明，不能冒充真实自然语言验收。
真实模型固定业务验收与首次失败记录见[验收说明](../evals/POSTGRES.md)。
CI单独新建合成PostgreSQL目标，不依赖本机容器、不默认调用真实模型。

依据：[18.6发布与修复](https://www.postgresql.org/docs/18/release-18-6.html)、
[只读事务](https://www.postgresql.org/docs/18/sql-set-transaction.html)、
[EXPLAIN](https://www.postgresql.org/docs/18/sql-explain.html)、
[规划器源码](https://github.com/postgres/postgres/blob/REL_18_6/src/backend/optimizer/util/plancat.c)、
[Psycopg服务器游标](https://www.psycopg.org/psycopg3/docs/advanced/cursors.html)。

## 2026-09-11 本地验收记录

本次整合了main `e3d9825123e5ce312a082f7126dbebd30a6e8e6b`的TODO17身份边界。
最终源码的本地验证：

| 层次 | 命令/入口 | 结果 |
| --- | --- | --- |
| 离线全量 | `uv run pytest -q` | 1909 passed / 169 skipped；跳过项需要明确真实环境开关 |
| 真实PG | 两个PG集成开关加上述三个test模块 | 82 passed：40业务、21隔离、21服务/身份 |
| 原MySQL与新增CASE | 只读MySQL开关，metadata/query/optimization/identity_optimization/mysql_case | 93 passed；原库reader配置只复制至忽略目录，无原库数据/权限变更 |
| 前端 | `npm --prefix frontend run build`、`run test:e2e` | build通过，33 Chromium合同测试通过；模型/HTTP替身不算真实PG模型 |
| 锁定打包 | `uv sync --locked --group build`、`uv build --no-build-isolation`、`scripts/check_distribution.py` | wheel与sdist检查通过；36/161 entries，未包含本地凭据/运行资产 |
| 模型业务 | `scripts/evaluate_postgres.py --run --repeat 1` | 首次5/6；提示与整合修复后完整6/6，见评测记录 |

SQL比较补充真实101位numeric精度案例，避免复用MySQL图表的100位算术预算导致误拒绝；
图表仍保留该独立预算并明确报错。所有隔离probe均使用随机独有对象，清理后表/schema/
角色无残留；公开三表5/6/6行与扫描探针100001行保留。

本机额外资产为忽略的`.env`、`.env.postgres`、`work/mysql-regression/.env`、
`outputs/evals/postgres`、运行记录、前端构建和打包目录，均不提交。
旧MySQL事务DDL测试未在原库重跑；远端MySQL job在全新合成目标运行它们。
远端CI以本PR最终HEAD的六项job为准，不用本地通过代替远端状态。
