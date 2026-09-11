# 受控数据库变更

当前支持本机独立 MySQL 8.4 合成目标中，按主键替换一条 `inventory.quantity`。完整流程是预览前后值 → 本人明确人工审批 → 独立点击执行 → 取回或核对提交结果 → 需要时创建反向变更并重新审批。它是 TODO18 的限定 DML 里程碑，不是任意 SQL、DDL、DELETE、批量管理器、生产变更平台或双人审批。

原 `execute_query`、`db query`、SQL 政策与只读事务保持原要求；变更不注册为模型工具，不调用模型。Web 当前查询源必须为 MySQL，PostgreSQL 不继承此能力。写目标使用独立配置、连接器和账号，浏览器不能提交连接串、SQL、角色、`approved` 或授权表。

## 准备与使用

在仓库根目录显式创建独立环境：

```bash
uv run python scripts/setup_changes_mysql.py --create
```

脚本只创建以下固定资源；任何同名容器、数据卷或既有配置存在即停止，不覆盖、不重新改密、不采用旧卷：

| 资源 | 精确范围 |
| --- | --- |
| Compose | `infra/changes/compose.yaml`，项目 `db-agent-changes`，服务 `mysql` |
| 网络和卷 | `db-agent-changes_default`、`db-agent-changes_mysql_data` |
| 服务地址 | `127.0.0.1:13316`，库 `db_agent_changes` |
| 合成数据 | `inventory` 三行；`change_receipts` 事务回执 |
| writer | `db_agent_changer`：inventory SELECT、仅 quantity/version UPDATE；回执 SELECT/INSERT |
| reader | `db_agent_change_reader`：仅 inventory SELECT，供独立页面验收的查询源使用 |
| 元数据检查 | `change_schema_guard` 视图调用固定无参数 `change_trigger_count()`；定义纳入目标摘要 |
| 锁定检查身份 | `db_agent_change_inspector@localhost` 禁止登录，仅两张表的 TRIGGER 元数据权限和固定检查函数 EXECUTE |

writer 另有固定视图 SELECT/SHOW VIEW、固定函数 EXECUTE，以及用于核对函数完整定义的 `SHOW_ROUTINE` 元数据权限。`SHOW_ROUTINE` 是实例范围的定义读取权限，因此此版本严格限定独立合成实例，不能照搬到共享生产实例。writer 没有 TRIGGER、DDL、DELETE、修改主键或改删回执权限。管理员仅通过此容器本地 socket 初始化；运行时不使用管理员凭据。

`outputs/changes/compose.env` 保存新容器初始化密码；`target.json` 保存 writer、实际 server UUID 和结构摘要；`reader.json` 保存独立只读账号配置。这些都是私有、忽略的本机文件，不改原项目 `.env`，也不改变 `db_agent_reader`。显式身份模式读取当前项目的 `outputs/changes/target.json`；缺失时关闭变更，文件无效时撤销接入。默认免登录工作区不读取此文件。不要手动刷新摘要以绕过结构漂移。

受控变更需显式启用[多身份模式](web-access.md)并配置查询源与私有身份；默认免登录工作区不开放此入口。在管理员管理命令中明确授权目标和人工审批权限：

```bash
uv run python scripts/manage_web_users.py set alice --tables orders \
  --change-targets local_inventory --allow-change-approval
npm --prefix frontend ci --registry=https://registry.npmjs.org
npm --prefix frontend run build
uv run db-agent web --identity-file outputs/web/identities.json
```

`set` 仍会交互设置登录密码；`--tables` 必须收窄已有查询白名单。省略新增标志时，变更目标为空、审批权限关闭。查询表权限不会自动转成变更权限；`local_inventory` 只授予本页固定目标的数据预览和变更范围。审批权限允许本人审核自己的预览，并执行已审批记录，不提供其他用户代审。

登录后打开“受控变更”，输入商品 ID 和新数量。页面显示精确主机、端口、库表列、原数量/版本、新数量/版本、完整 digest、到期时间。勾选“我已核对目标与前后值”并点击“明确人工审批”；再点击“执行已审批变更”。刷新只读本机记录，核对才向目标发起新的锁读与回执查询。网络失败后保留原变更 ID，不要重新创建同一业务变更来猜测结果。

当前约束：一次恰好一条已存在记录；ID 1–1,000,000,000，数量 0–1,000,000，版本最多 2,147,483,646；相同数量不建立变更。预览和审批共用 5 分钟有效期。每授权范围最多 100 笔永久记录，本版本没有删除执行账本接口；请求最多 8 个排队，单次含排队总预算 15 秒，MySQL 行锁/元数据锁等待 2 秒、连接 3 秒。要求本机时钟正确，不是跨机器时间或通用分布式事务保证。

## 审批、事务与恢复规则

身份来自当前服务端登录会话。用户、查询源种类、数据源配置、身份配置、写目标 UUID/结构摘要及凭据版本共同进入授权代际；配置变化撤销旧登录。每份预览绑定 owner、授权 scope、目标、主键、前后数量/版本、期限、恢复来源和内容摘要。审批正文只接受 digest；审批人、时间及执行 claim 由服务端持久化。

真正写入入口再次核对所有绑定、审批权限、有效期和持久化凭证；每次数据库派发前后及提交前还会重验，只允许否决。授权或关键存储失效时停止。两个相同请求标识只能取回同一范围内的原记录，不能更换内容；不同用户的标识与记录隔离。

执行顺序：

1. SQLite 原子转换 approved → executing，提交 claim 后才能连接 MySQL。缺审批或 claim 保存失败时不写数据库。
2. 新连接核对实际数据库、最小 writer 身份、server UUID、MySQL 8.4、严格模式和完整定义。两张业务/回执表必须是 InnoDB 基础表；固定检查函数先核验检查身份具备两表完整 TRIGGER 可见权限，缺失返回负值拒绝；权限完整且两表触发器计数为零才继续。
3. 显式 READ COMMITTED + READ WRITE 事务，按主键 `FOR UPDATE` 锁行，取得回执表元数据锁后再次检查结构和事务状态。数量或版本任一不同都拒绝，包含数量改回原值但版本已经前进的情形。
4. 参数化 UPDATE 只改该行数量和版本，实际影响必须为 1；同事务 INSERT 唯一 change_id 回执，核对事务内后值、回执和授权后 COMMIT。
5. 保存实际结果证据。提交回执或后置存储失败不会重新执行：保留 unknown 或 executing，交由核对。

`rolled_back` 仅表示本次事务在 COMMIT 派发前收到 ROLLBACK 确认；应用取消/连接关闭本身不是服务器 SQL 已取消的证明。COMMIT 派发后的异常一律保留不确定。发现既有回执时进入核对，不把新事务的回滚误说成该变更历史未提交。

核对先取得服务内互斥锁，确认原派发任务已退出；随后在新事务中锁定同一库存行，等待旧数据库事务完成，再用 READ COMMITTED 读回执。只有精确 UUID/定义/授权、锁屏障和回执取证完整时，匹配回执确认 committed；无回执可确认 not_committed。超时、断连、结构变化、回执不匹配都保留未知。已证实提交是不可逆的历史事实；后续核对失败只更新最新证据，不把历史状态降为未提交。

恢复是新的反向 DML：原变更必须已确认提交且最新取证有效，当前数量/版本必须仍等于原后值；新预览把数量恢复原值、版本继续加一。它需要新的 digest、人工审批和执行 claim；执行前会再次核对原提交依据。后续修改造成漂移时拒绝恢复，不覆盖别人的新结果。两份同前值的并发审批最多一份通过锁内前值检查。

这是单进程、单账本范围的受控执行，不宣称通用 exactly-once。Web 持有该 SQLite 文件的进程锁；不要在服务运行中复制/恢复账本启动第二个派发器。数据库账号/初始化定义和本机操作者属于可信边界。数据库故障、管理员篡改回执、丢失目标卷或账本不能通过自动重试“恢复”。

## 授权撤销后的人工核对

授权代际变化后，旧历史、旧 unknown 和旧审批均不能被新代际重新访问；恢复原配置也不会复活权限。仍需核对的旧记录保留在私有 SQLite 和目标回执表，不能为了查看它而放宽用户权限。

本机管理员停下旧服务、核对精确独立目标和旧记录的 change_id/digest/item_id/前后值后，可在该容器的 socket 会话中进行**只读人工核对**。不要在聊天或日志打印密码：

```bash
docker compose -f infra/changes/compose.yaml --env-file outputs/changes/compose.env \
  exec mysql mysql --protocol=socket -uroot -p db_agent_changes
```

在同一 READ COMMITTED 事务中先 `SELECT quantity,version FROM inventory WHERE id=<已核对主键> FOR UPDATE`，再按精确 change_id 查询 `change_receipts` 并比对完整 digest、前后值/版本，最后 ROLLBACK 释放锁。必须确认旧进程已停止、实际 server UUID/定义与原记录目标一致；缺任何证据仍未知，禁止直接重发。此人工通道不导入旧批准、不恢复 Web scope、不由 Agent 自动调用。

## 验证与证据

```bash
uv run pytest tests/test_changes.py -q
DB_AGENT_CHANGES_INTEGRATION=1 uv run pytest tests/test_mysql_changes.py -q
uv run python scripts/evaluate_changes.py --run
```

截至 2026-09-11 本 worktree 的阶段验收：

- 28 项离线测试核对输入、审批、持久化状态、隔离、恢复依据与 PostgreSQL 不继承变更；使用明确数据库替身。
- 30 项独立真实 MySQL 测试覆盖实际前后值/回执、权限拒绝、双用户同目标隔离、重复与并发、前值/版本漂移、审批和存储故障、UPDATE 后真实服务器权限拒绝触发的回滚、已存在回执、触发器漂移/检查权限丢失、锁等待超时/过期、注销后 watcher 取消，以及恢复。权限故障测试在回执写入位置注入 writer 无权执行的 `INSERT INTO inventory`，并核对先前 UPDATE 已回滚、无回执；它没有撤销回执表权限，不能称为回执 INSERT 本身被拒绝。
- 其中两项真实 TCP 代理只转发 `127.0.0.1:13316`：一项在 COMMIT 到达服务器前断连，另一项实际收到 MySQL COMMIT OK 包后丢弃。另有明确标注的驱动边界注入和 SQLite 故障注入；它们连接真实数据库，但不冒充自然发生的网络事故。
- 独立 Chromium → 登录 → Web API → MySQL → 外部行/回执读回，10/10步骤通过。两笔实际事务分别变更和恢复数量，版本累计加二；没有 HTTP route 替身或模型调用。报告、两个变更 ID、实际前后值、回执和源码摘要保存在忽略的 `outputs/changes/acceptance/<id>/report.json`，截图位于同目录。
- 前端全套 42 条 Chromium **HTTP 合同替身**通过，其中 9 条覆盖变更面板；整合 TODO19 后原 MySQL 读取/CASE/优化/身份优化回归 93 项通过，PostgreSQL 只读与 Web 回归 62 项通过，其中真实 PG 查询成功后，在显式授予 MySQL 变更范围的身份下，所有变更入口仍被数据源种类检查拒绝。上述证据均为本机合成环境，不是生产验证或正确率统计。

GitHub CI 的独立 `MySQL changes / real transactions and browser` job 从空的新 runner 创建此目标，运行真实写入/恢复/TCP故障测试和真实浏览器流程；原查询、前端、Python版本与打包 job 保留。最终 HEAD 的远端状态须查看对应 PR，不沿用旧提交绿灯。

停止并保留合成数据和回执：

```bash
docker compose -f infra/changes/compose.yaml --env-file outputs/changes/compose.env stop mysql
```

重新启动只使用同一配置执行 `up -d --wait`。验收脚本自动停止其临时 Web 子进程和 TCP 代理；不会删除容器、数据卷、账本或凭据。`outputs/changes/acceptance` 是本机隐私材料，可在不再需要核对时由维护者清理；删除备份不等于回滚数据库。原 `db-agent/mysql`、`db-agent_mysql_data` 和 13306 端口不受此命令影响。

实现依据：[MySQL 锁读](https://dev.mysql.com/doc/refman/8.4/en/innodb-locking-reads.html)、[存储例程权限](https://dev.mysql.com/doc/refman/8.4/en/stored-routines-privileges.html)。应用仍以自己的实际事务、回执和边界测试作为验收依据。
