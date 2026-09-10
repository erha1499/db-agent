# 电商数据与业务验收

目标是让查询能力在有规模和业务边界的电商数据上接受验收：客户下单、订单包含多条商品明细、支付可能失败或拆分、退款可能部分完成。所有数据均由公开代码生成，不包含真实用户资料。

## 数据规模与业务关系

默认配置为 `orders=1000000`、`customers=100000`、`products=10000`、`seed=20260910`，数据版本为 `ecommerce-v1`。同一配置产生相同数据，生成器逐单输出，导入器每批默认处理 1000 个订单及其子记录。

| 表 | 默认实际行数 | 含义 |
| --- | ---: | --- |
| `ec_customers` | 100,000 | 客户、地区、注册时间；包含没有订单的客户和 NULL 地区 |
| `ec_products` | 10,000 | 商品、分类、当前价格及停售时间 |
| `ec_orders` | 1,000,000 | 订单状态、成交毛额、创建/支付/取消时间 |
| `ec_order_items` | 2,998,854 | 订单商品、数量、成交单价、优惠及明细净额 |
| `ec_payments` | 1,426,667 | 支付尝试、成功到账和失败原因；一单可多次支付 |
| `ec_refunds` | 572,360 | 针对具体支付的退款尝试与完成时间；一笔支付可多次退款 |

六张业务表合计 6,107,881 行。订单通过 `customer_id` 关联客户；明细通过 `order_id`、`product_id` 关联订单与商品；支付关联订单；退款同时通过订单和支付的复合外键关联原支付。全部使用 InnoDB，主键、外键和状态/金额 CHECK 在导入期间保持启用。

背景订单覆盖 2026 年第一季度，包含热点客户、热点商品和 2 月 15–17 日活动集中下单；普通背景单有 2–4 条明细。固定的低 ID 边界订单独立于背景数据，包含跨月支付、零元订单、取消/待支付、拆分支付和部分退款。背景客户 `1000` 另有可由公式独立推导的 1000 笔、100000.00 元订单，用于在百万订单表上验证索引查询。

金额采用人民币元，生成时按整数分计算，存储为 DECIMAL。订单金额为各明细 `quantity * unit_price - discount_amount` 的合计；成功支付、成功退款分别按流水状态与各自完成时间统计。按创建月、支付月和退款月统计是三种口径，订单数也不等于支付流水数。具体边界账本和独立预期值见 [评测说明](../evals/README.md)。合成数据约定按 UTC 解释时间，但 MySQL DATETIME 本身不携带时区。

## 生成与导入

先按 [README](../README.md) 配置 `.env` 并启动本地 Compose MySQL。导入命令使用容器内 root socket，只允许本项目健康的 `db-agent/mysql`、`db_agent` 库和 `127.0.0.1:13306` 绑定；root 不进入 Agent 配置。

```bash
# 默认实际导入 100 万订单及关联数据。
uv run python scripts/seed_ecommerce.py --apply

# 只生成可查看的 SQL 文件，不访问数据库；目标文件必须尚不存在。
uv run python scripts/seed_ecommerce.py --sql-output outputs/ecommerce/ecommerce.sql

# 对现有数据重新核对清单、真实行数和金额关系，不修改数据。
uv run python scripts/seed_ecommerce.py --verify-only
```

首次导入也可同时指定 `--apply --sql-output outputs/ecommerce/ecommerce.sql`。可以调整 `--orders`、`--customers`、`--products`、`--seed` 与 `--batch-size` 生成其他规模；固定评测要求默认百万规模，其他规模不能使用该预期值声明通过。

脚本取得同连接的导入锁，确认六张业务表和 `ec_seed_manifest` 均不存在后建表。客户/商品与各订单批次分别提交，已提交的数据不会因后续批次失败被抹去。清单先记录 `LOADING`，只有真实行数、订单/明细/支付/退款关系验证通过，并完成统计信息采集后才记录 `COMPLETE`。同配置的完整数据再次 `--apply` 只验收；部分表、不同配置或未完成状态会停止，不会覆盖、DROP、自动重试或断点补写。失败后需先核对清单与现场，再明确决定如何处理。

导出的 SQL 没有运行 Python 的数据库验收，因此文件末尾仅标记 `UNVERIFIED`；直接执行该文件不能作为验收通过证据。对导出数据使用 `--verify-only` 可以生成独立验收报告，但不会改写其数据库清单状态。推荐使用 `--apply` 完成导入与验收闭环。

导入报告和 SQL 只写入 Git 忽略的 `outputs/ecommerce/`。管理清单不加入 Agent 白名单；导入器不是 Agent 工具，也不接受任意 SQL 或数据库目标。

## 开放只读查询

在 `.env` 原白名单中追加六张业务表，保留原有三个小表：

```dotenv
DB_AGENT_MYSQL_ALLOWED_TABLES=["customers","orders","order_items","ec_customers","ec_products","ec_orders","ec_order_items","ec_payments","ec_refunds"]
```

```bash
uv run db-agent db describe ec_orders
uv run db-agent db query "SELECT COUNT(*) AS n, SUM(total_amount) AS amount FROM ec_orders WHERE customer_id=1000 AND created_at >= '2026-02-15' AND created_at < '2026-02-16'"
uv run db-agent chat '查询 ec_orders 中 customer_id=1000 在2026年2月15日创建的订单笔数和 total_amount 合计，日期范围左闭右开。'
```

订单索引覆盖客户与创建时间、状态与创建时间，以及支付时间；支付和退款也有订单/状态及完成时间索引。业务查询仍走原有静态预检、普通 EXPLAIN 与受控 SELECT，不放宽扫描阈值。跨全表的大聚合可能进入 `REVIEW`，只返回少量结果或添加 LIMIT 都不能绕过它。

## 可重复验收

```bash
uv run python scripts/evaluate_ecommerce.py --suite v2 --mode sql --split dev --repeat 1
uv run python scripts/evaluate_ecommerce.py --suite v2 --mode sql --split holdout --repeat 1
uv run python scripts/evaluate_ecommerce.py --suite v2 --mode agent --split dev --repeat 2
uv run python scripts/evaluate_ecommerce.py --suite v2 --mode agent --split holdout --repeat 2
```

SQL 模式直接调用受控查询服务；Agent 模式向配置模型发送业务问题及公开业务字典，让模型自行调用工具和生成 SQL，不向模型提供参考 SQL 或预期值。字典明确成功支付/退款使用 `succeeded` 等状态映射，避免把未知业务口径当成模型应当猜出的知识。两种模式都按独立预期值核对实际结果；Agent 模式还核对动作数量、预算和最终回答的可信报告展示路径。每题即时显示状态，完整报告位于 `outputs/evals/`；有失败时退出码为 1，配置问题为 2。

数据版本始终为 `ecommerce-v1`；评测套件另外分版本。原 v1 的 16 题已用于诊断框架步数、SQL 筛选遗漏和业务字典缺失，保留原题与首次失败报告，不再将后续重跑当作未见过的验收。v2 使用这 16 题做开发回归，另有 8 道新冻结题；共有字典只含口径与状态，不含答案。报告绑定套件、实际输入、源码哈希、模型配置及每次尝试。

开放式解释质量不由本评测自动打分；修改冻结用例或根据其失败调整实现后，需要新版本重新验收。生产运行事件仍不保存原始 SQL 或业务值，显式合成评测的报告可以保留查询证据用于定位失败。

上述行数来自本地 MySQL 8.4.11 的真实导入和重新验收。它验证当前合成数据上的业务结果与控制边界，不构成生产部署、TB 级压测或通用查询正确率结论。

## 2026-09-10 验证记录

本次实际导入 1110 个事务批次，并重新核对六表真实行数、订单与明细金额、成功支付金额、退款归属及每笔支付的累计退款上限。六表统计信息采集均返回 `OK`；重复 `--apply` 确认已有数据后只做验收，未重复写入。

| 验证 | 本次结果 | 范围 |
| --- | --- | --- |
| 完整 pytest，开启两个 MySQL 集成开关 | 832 项通过 | 含 41 项真实 MySQL 集成；其余为离线规则、框架、生成器和失败路径测试 |
| Ruff / Git diff 检查 | 通过 | 静态检查与变更格式 |
| v2 固定 SQL | dev 16/16，holdout 8/8 | 真实受控查询；每题一轮 |
| v2 真实模型回归 | 31/32 | 16 个已暴露案例，各两轮 |
| v2 新冻结模型验收 | 16/16 | 8 道未参与本次提示调整的新题，各两轮 |

模型回归失败发生在第二轮「只查询零订单客户」：SQL 包含 LEFT JOIN 与 COUNT，但缺少聚合后的零值筛选，返回了全部八位客户。返回数据与该 SQL 一致，回答展示也忠实于工具报告，但未满足用户的业务条件。该问题仍列在 [TODO 第 11 步](../TODO.md)；静态预检的授权与风险判断不能替代自然语言业务正确性验收。

此前 v1 模型开发集首次 14/16、首次冻结集 12/16 的失败也保留在本机报告中。由此修正了框架步数计算、SQL 支持范围说明与结果直接展示的产品契约，并在 v2 明确业务字典。没有修改旧报告或原题答案，也没有调整数据库扫描阈值。当前 31/32 与 16/16 是不同集合上的有限样本结果，不能据此声称生产正确率。

本次模型使用本机配置的别名，提供方未提供可核验的实际权重版本；具体配置、来源哈希、逐题输入哈希、运行 ID 和失败记录保留在 `outputs/evals/`。SQL 导出与原始评测报告不纳入公开提交。直接数据库查询不依赖模型，真实环境上线和开放式解释质量尚未验收。
