# SQL 优化验证

`db-agent db compare` 接收原 SQL 与候选 SQL，实际执行受控核对，返回完整结果、差异和优化观测。它不调用模型、不写数据库，不把模型建议或旧分析报告当成执行授权。

## 产品入口

```bash
uv run db-agent db compare \
  --original 'SELECT id FROM orders WHERE id + 0 = 1002' \
  --candidate 'SELECT id FROM orders WHERE id = 1002' \
  --repeat 3
```

敏感 SQL 使用标准输入，避免放入进程参数或 shell 历史。`--stdin` 只接受 `original`、`candidate` 两个字符串字段，不能同时提供 SQL 参数。输入 JSON 最大为两倍 SQL 字节预算加 4096 字节；每条 SQL 仍受原 SQL 字节及 AST 预算限制。下面仅使用公开合成 SQL：

```bash
uv run db-agent db compare --stdin <<'JSON'
{"original":"SELECT COUNT(*) AS n FROM customers","candidate":"SELECT COUNT(region) AS n FROM customers"}
JSON
```

无需模型配置；数据库配置及白名单与 `db query` 相同。候选可来自已有诊断建议，也可由使用者手写；本入口不自动生成或执行后续修正。输入、结果和计划保存在当前返回报告中，SQL/行值不会写入 `outputs/runs` 或发送给模型；自行重定向的 JSON 包含原 SQL、结果和表结构信息，应按数据的权限保存。

| outcome / 退出码 | 含义 |
| --- | --- |
| `observed_equal` / `0` | 本次核对所覆盖的完整结果一致；不是通用等价证明 |
| `different` / `4` | 在完整快照证据内发现行值、重复次数、顺序或列合同差异 |
| `inconclusive` / `1` | 拒绝、截断、错误、证据缺失、跨次变化或顺序合同变化；不能确认相等 |
| 输入或配置错误 / `2` | 未取得有效的比较请求或配置 |

报告含比较 ID、时间、数据源范围摘要、确切原/候选 SQL、结构观察、每次双方的 `decision` / `execution_status` / `result` / 计划 / 耗时、实际完成次数和预算。非法 SQL 不回显到 `statements`。范围摘要只用于关联，不是授权。`general_equivalence_proven` 始终为 `false`，`independent_oracle_checked` 默认为 `false`：使用者提交的原 SQL 不是由产品独立验证过的业务标准答案。

## 比较规则与快照

单 SQL 的 `execute_checked` 保持原来的 READ COMMITTED。比较入口 `compare_checked` 使用同一个私有执行内核，在一个新连接的 `REPEATABLE-READ`、`START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY` 中处理两侧。读回隔离级别并检查协议只读事务状态；两侧先静态检查，任一静态拒绝则不连接。每侧再次原样检查 SQL，验证数据库/版本/模式、基础表/InnoDB、普通 EXPLAIN、计划规则、对象类型和事务状态；每侧独立调用服务端 `before_select` 否决。只有该侧全部 ALLOW 才派发 SELECT。

第二侧的计划在第一侧结束后采集；第二侧失败时第一侧可能已经完成，只能展示其单独证据，整次不能确认相等。任何一侧截断、超时、断连或取消都停止本对比较，不排空 SSCursor、不重连补跑。事务随连接清理结束；应用取消不等于服务器 SQL 已确认取消。MySQL 的一致性读取语义见[官方文档](https://dev.mysql.com/doc/refman/8.4/en/innodb-consistent-read.html)。元数据变化导致取证错误时同样停止。

一对 SQL 共用原操作总预算，默认 15 秒，包含排队、连接、两侧分析和读取；每侧分析最多 10 秒、SELECT 派发和读取最多 5 秒。结果仍每侧最多 100 行、32768 JSON 字节、64 列，扫描阈值仍为 100000。`--repeat` 明确选择 1–3 对，默认 1；分别使用新快照，顺序为 AB、BA、AB，每对仍受同样预算，最多 6 次业务 SELECT。差异或失败后停止剩余对，保留计划次数与实际完成次数。

- 按列位置比较，列标签、类型和数量构成结果接口；重名列不会覆盖，交换位置可被识别。类型或别名变化报告 `column_contract_differs`，不会悄悄丢弃列合同。
- 双方没有 ORDER BY 时比较多重集，保留每种行的重复次数；任一有 ORDER BY 则比较返回序列。添加或删除 ORDER BY 时，即使当前序列相同也报告 `inconclusive`。
- NULL 独立于零、空字符串和文本 `NULL`。数值使用精确十进制值比较，允许同类型 Decimal 的尾随零差异；大整数不转 float。浮点不设容差，文本精确比较，不模拟 MySQL collation。
- GROUP BY、JOIN、日期条件和聚合仍由 SQL 决定；比较不会消除关联放大、补齐缺组或把 NULL 改成零。DATETIME 无时区，TIMESTAMP 使用 UTC 会话。
- 完整结果仅覆盖 SQL 的 WHERE/LIMIT。无排序 LIMIT 和并列排序键仍可能产生不确定选择；一次相同不保证其他计划、数据或时刻相同。多次期间发现任一侧结果变化则返回 `results_changed_between_trials`，不汇总性能。

AST 相同只是一项结构观察；本快照结果一致只是一项运行观察；通用 SQL 等价证明需要另行建立。固定边界数据可以击穿已知错误改写，但有限样本不能证明所有数据状态都正确。

## 独立验收与复现

```bash
# 离线协议、异常、预算和结果语义
uv run pytest tests/test_optimization.py tests/test_optimization_connector.py -q

# 已有固定本地数据，reader-only，不创建或修改数据
DB_AGENT_MYSQL_INTEGRATION=1 uv run pytest tests/test_mysql_optimization.py -q

# 显式比较，报告写入忽略的 outputs/evals/optimization
uv run python scripts/evaluate_optimization.py --run --repeat 3

# 可选：已有 ecommerce-v1 百万订单数据，不自动初始化、不扩大白名单
uv run python scripts/evaluate_optimization.py --run --include-ecommerce --repeat 3
```

[独立案例](../evals/optimization_cases.json)的预期由[固定合成数据](../tests/fixtures/mysql_business.sql)人工推导；包含谓词重排、索引条件、折扣金额、NULL、空集、日期边界、重名列、超大整数、排序、LIMIT、重复行、LEFT/INNER JOIN、COUNT(nullable) 和 JOIN 放大 SUM 的反例。验收代码直接核对预期行，不调用产品比较函数生成自己的答案。这是公开、已用于开发的工程验收集，不是未见题准确率评估。

[脚本](../scripts/evaluate_optimization.py)仅允许固定本地 reader 目标。显式初始化所有预算默认值，`.env` 和 shell 都不能放宽验收阈值。每次保存实际计划、结果、执行顺序和耗时，记录产品源码、脚本及案例哈希，前后复核不变。电商规模检查使用 101 个固定 ID 范围的有界 COUNT 及一条 MIN/MAX，全部走原 QueryService 重检；这是一段时间内的规模观察，不是原子全库快照。优化比较本身每一对有独立的一致性快照。

### 2026-09-11 本地真实结果

环境：MySQL 8.4.11，`127.0.0.1:13306/db_agent`、`db_agent_reader`，既有公开合成数据。模型调用 0、数据库写入 0。最终脚本报告 `bffacf1fe7da48bc919e89ead4929878`，UTC `05:39:52`–`05:39:53`，24/24 案例通过独立预期核对；10 个相同结果案例各运行 3 对，13 个错误改写各在首对发现差异，1 个删除排序合同案例正确返回不确定，共 44 对、88 次业务 SELECT。源码哈希前后相同。

电商 ID 范围规范为固定审计单 `1–8`，背景单 `1000–1000991`。窗口内范围计数合计 1,000,000 订单；最小/最大 ID 为 1/1000991。初版规模检查错误假定 ID 连续到 1,000,000，首段实际 9009 行而非假定 10000 行，按失败停止。随后根据既有数据规范修正规模 oracle，未改数据、权限、业务案例预期或扫描阈值。复审另发现 `_env_file=None` 仍继承 shell；已改为显式默认值，并在该最终报告重新验收。

| 场景 | 计划观察（原 → 候选） | 三次派发到读取完成的客户端耗时 ms（原 / 候选） | 中位数 ms |
| --- | --- | --- | --- |
| 6 行 orders，`id + 0 = 1002` → `id = 1002` | index，估算每次扫描 6 → const，1 | 0.243 / 0.248；0.235 / 0.265；0.254 / 0.212 | 0.243 → 0.248 |
| 百万订单表内 `id <= 20000 AND id + 0 = 2` → `id <= 20000 AND id = 2` | PRIMARY range，估算 38920 → PRIMARY const，1 | 2.698 / 0.351；2.342 / 0.224；2.157 / 0.244 | 2.342 → 0.244 |

电商双方完整结果均为 `[[2, "90.00"]]`，金额独立来自固定审计单的 80−10+20。计划行数是估算，不能当成实测读取行数；客户端时间包含网络、结果解码/编码与游标清理，不含前面的计划采集。未执行 EXPLAIN ANALYZE、清缓存、建索引或管理员写入。小表案例并未观察到耗时改善；电商案例观察到更低耗时，但仅有本地 3 次、受限谓词和该快照结果，不能据此宣称普遍性能提升或生产收益。

最终离线、真实 MySQL、构建与当前 PR HEAD CI 状态由 PR 交付记录维护。新增真实案例已接入 GitHub MySQL job；离线协议替身与真实结果分别报告。该功能目前为直接 CLI 产品入口，不新增 Agent 工具、Web 比较 API 或多身份配置入口。TODO17 整合时必须保留共用 SELECT 派发前和读取后的身份否决检查；不能绕过其收窄的连接器实例。

## PostgreSQL 数据源

可信配置选为PostgreSQL时，直接CLI比较使用该源自己的预检/计划/只读执行内核，
两侧共享REPEATABLE READ READ ONLY快照，报告标记`same_readonly_postgres_snapshot`。
保留AB/BA轮换、完整结果和独立列/行比较规则；截断、未知、失败或撤权均不能确认相等。
MySQL的InnoDB/SQL mode/计划v1不作为PG证据，也不混比两个源的结果。
PG知识模板确认与比较使用可信postgres方言和实际schema，详见
[PostgreSQL使用与验收](postgresql.md)。
