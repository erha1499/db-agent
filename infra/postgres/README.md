# 独立 PostgreSQL 合成环境

本环境固定使用 Compose 项目 `db-agent-postgres`、服务 `postgres`、数据库
`db_agent_pg`、schema `business`，仅绑定 `127.0.0.1:15432`。它不访问或修改
原 `db-agent/mysql`，不是生产环境。

从仓库根目录运行：

```bash
uv run python scripts/setup_postgres.py --apply
uv run python scripts/setup_postgres.py --verify-only
```

`--apply` 仅在 `.env.postgres` 不存在时创建 0600 私有配置，随机生成两个不同
的密码，不输出密码。已有文件必须匹配固定目标和三表白名单，脚本不会覆盖；
人工填写可参考 `.env.postgres.example`。Docker Compose 必须显式选择这个
文件，不能使用原 MySQL 的 `.env`：

```bash
docker compose --env-file .env.postgres -f compose.postgres.yaml ps
docker compose --env-file .env.postgres -f compose.postgres.yaml up -d --wait postgres
```

首次空卷启动由官方 entrypoint 创建只读角色与 schema；业务 fixture 则由
`--apply` 在确认目标对象全部不存在后，以同一事务创建和导入。任一目标已存在
时再次 `--apply` 返回 1，且不写 fixture；应改用只读的 `--verify-only`。
初始化失败不自动修复、补写、重试或删除。现有卷上修改配置文件不会更改数据库
密码或权限；配置与数据库不一致时停止验收。

运行账号为 `db_agent_reader`，没有超级用户、建库、建角色、复制、RLS 绕过和
任何角色成员权限。数据库仅有 CONNECT；business schema 仅有 USAGE；固定
fixture 表仅有 SELECT。reader 不能创建普通表或临时表，默认只读事务，默认
搜索路径为 `pg_catalog, business`，时区 UTC。应用仍必须独立检查实际权限、
目标、对象、SQL、计划和执行预算；默认只读设置不能替代这些检查。

管理员仅用于独立合成目标的初始化，入口是容器内 postgres OS 用户的本地
peer 认证，管理密码不能作为 Agent 凭据：

```bash
docker compose --env-file .env.postgres -f compose.postgres.yaml exec --user postgres postgres psql --username=postgres --dbname=db_agent_pg
```

`tests/fixtures/postgres_business.sql` 的数据是公开合成数据，金额口径与原
MySQL 小 fixture 一致。`created_at` 是无时区 timestamp；`paid_at` 是
timestamptz，1001 号订单的 `2026-02-01 08:01:00+08:00` 输入应返回
`2026-02-01 00:01:00+00`。独立预期为：

| 检查项 | 预期 |
| --- | --- |
| customers / orders / order_items | 5 / 6 / 6 行 |
| 所有订单金额 / 所有行净额 | 均为 230.00 |
| status=paid 的订单金额 | 130.00 |
| created_at 属于 2 月的 paid 订单金额 | 30.00 |
| paid_at 在 UTC 2 月的 paid 订单金额 | 130.00 |
| 无订单客户 | id=4 |
| 未知地区客户 | id=3，region 为 NULL |
| pg_scan_probe | 100001 行，id=1..100001，bucket=1 |

扫描探针只用于计划阈值测试，故意不在常规三表白名单中；测试需单独明确授权。
普通 EXPLAIN 是计划取证，不执行待检查 SQL；这些小样本不代表生产性能。

停止并保留数据：

```bash
docker compose --env-file .env.postgres -f compose.postgres.yaml stop postgres
```

仅清理本测试项目的容器和网络、保留命名卷：

```bash
docker compose --env-file .env.postgres -f compose.postgres.yaml down
```

卷名为 `db-agent-postgres_postgres_data`；上述命令不删除它。永久删除卷及其
数据须另行明确授权，不能使用 `down -v` 当成改密或重跑初始化的办法。私有
`.env.postgres` 是本机凭据资产，不提交、不输出，停止实例后仍需保留以便复用。

镜像锁定为 PostgreSQL `18.6` 和实际拉取的官方多平台 digest
`sha256:4ef4dbc939d61acea57712655ddb4b4ab27419c913f94cca0cd57cb3ea3c2280`。
已按[官方镜像说明](https://hub.docker.com/_/postgres)采用 PostgreSQL 18 的
`/var/lib/postgresql` 卷路径；角色和搜索路径分别遵循
[CREATE ROLE](https://www.postgresql.org/docs/18/sql-createrole.html)及
[schema 与 search_path](https://www.postgresql.org/docs/18/ddl-schemas.html)文档。
