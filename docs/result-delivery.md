# 数据分析与结果交付

本机 Web 的「分析与交付」页签把实际查询结果转换为分组对比、时间趋势和可下载报告。自然语言查询仍走原有需求核对、SQL 预检与只读执行；分析阶段只读取已经保存的结果，不重新查库，不把业务行发送给模型。

## 从问题到报告

先按 [Web 使用说明](web.md)启动页面。在已授权的小表 fixture 上，可以输入：

> 查询 orders 的全部订单，返回 created_at、status、total_amount 三列，按 id 升序，最多返回20行。不要聚合，不排除任何状态。

查询完成后，打开该结果的「分析与交付」：

1. 维度选择 `status`，指标选择 `total_amount`，展示方式选择「分组对比」，点击「生成分析」。返回行按状态求和，图形、精确明细和分组合计的最小/最大值一起展示。
2. 维度改为 `created_at`，展示方式改为「时间趋势」，重新生成。时间升序排列；给出最早和最晚已返回时间点的合计差额。
3. 下载 HTML 报告，在浏览器打开、打印或保存为 PDF；也可以下载 JSON 快照以保留可程序读取的原始类型和数组位置。JSON 同时包含当前已生成的分析。
4. 刷新或重启 Web 后，从左侧重新打开同一会话，可再次取回保存的结果并分析或下载。取回的是保存时的快照，不是实时刷新后的数据库数据。

修改维度、指标或展示方式会清除旧分析。重新生成前下载的是原始结果报告，不会混入旧选择。单列、没有数值列和空集也可以下载原始报告；无法形成维度/指标组合时不会伪造图表。

## 统计口径与精度

- 维度和指标按列位置选择，重名列仍分别保留。分组以返回值精确匹配；不会模拟 MySQL collation 的大小写、重音或尾随空格等行为。NULL 维度与字符串 `"NULL"` 不合并。
- 目前的汇总方式为**所选指标求和**，支持返回类型中的整数、decimal、float、double、year；不会把 varchar 中的数字猜成指标。SQL 已聚合的值仍只进行求和，不能把平均数的和解释成总体均值。需要订单数、平均值、去重等指标时，应先明确查询口径。
- Decimal 和大整数使用十进制精确计算，标签与表格保留字符串；仅归一化图形坐标使用近似比例。浮点类型只按数据库已经返回的近似值计算，不能恢复数据库浮点误差。非常接近或极大数的图形可能无法区分，应核对精确表格。
- NULL 指标不参与求和，全 NULL 分组保持 NULL；同时显示返回行数和非 NULL 行数。空集不产生虚构的 0 值分组。
- 时间趋势仅接受 date、datetime、timestamp、year 列，按原时间值排序。NULL 或无效时间会拒绝生成趋势，仍可分组对比或导出原始结果。时间点使用等距类别轴，不补齐缺失日期，不计算增长率，不把间距解释为真实时间间隔。DATETIME 不自带时区，TIMESTAMP 仍遵循查询报告的 UTC 会话约定。
- 最多分析 100 个不同维度值，超过时整份分析报错，提示先通过受控 SQL 汇总；不会静默只取前 100 组。原始结果下载不受这个分组限制，仍受原查询的行数、列数与字节预算约束。
- 统计范围始终为**当前 SQL 已返回的行**。WHERE、LIMIT、关联重复和 SQL 预聚合都会影响结果；分析不会去除关联产生的重复。截断结果有明显提示，不能用于推断原查询总量或全库统计。完整返回也只表示当前 SQL 的结果完整。

## 保存、取回与下载边界

继续复用 `outputs/web/conversations.sqlite3`，不建立第二套结果副本或任意文件存储。服务端按当前数据源、账号与白名单 scope 读取已持久化运行，核对会话 ID、运行 ID 和唯一结果 ID 的对应关系；要求运行完成以及一致的 `ok / ALLOW / completed或truncated` 查询报告。未落盘、运行失败、执行未知、矛盾报告或重复结果标识均不能导出。

这只支持当前本机范围内 Web 已保存的查询；CLI 的任意 `result_id` 不能用于取回。导出与分析每次从持久化存储重新取证，不接受浏览器提交结果行、SQL、任意文件路径、数据库目标或授权。删除会话后旧标识失效；换数据源、账号或白名单并重启后旧范围不可访问。另有查询缺报告时，单份有效结果可交付，但报告注明不能代表整轮任务完整成功。

所有接口继续要求本机 Host、同源 Origin 与专用请求头 `X-DB-Agent-Client: web`：

| 方法与路径 | 输入 | 输出 |
| --- | --- | --- |
| `GET /api/conversations/{conversation_id}/runs/{run_id}/results/{result_id}` | 无额外参数 | 原始快照、SQL、来源、报告、范围说明 |
| `POST …/results/{result_id}/analysis` | `dimension`、`measure`（从0开始的列位置），`kind`（comparison/trend） | 快照及确定性分析 |
| `POST …/results/{result_id}/export` | `format`（json/html），可选 `analysis` 同上 | 服务端生成、固定文件名的附件 |

JSON 保留原始报告、列数组、行数组、精确数值字符串、NULL 与截断信息，不把重名列改成字典。HTML 包含实际 SQL、范围、当前已生成的图表与分组表、原始结果表；纯文本均转义，控制/双向字符显示为转义文本，内容不执行脚本、不加载外部资源。下载使用带原 API 头的请求；响应禁止缓存并设置附件与 `nosniff`。浏览器负责文件下载位置，服务端不接收路径。

下载文件含查询和业务结果，属于用户自行保存的本机副本。删除应用会话不会删除已下载文件。报告不会保存回模型上下文，不属于跨会话业务知识、执行批准或多用户共享能力。

## 2026-09-11 验收

本次以含 TODO13 CI 的 main `2602cdb01e4605c4de54391e971a5afd95670731` 整合，零新增依赖，保留公开 npm URL 与锁定构建流程。自动测试、真实服务与历史评测分别说明：

| 验证层次 | 入口 | 结果 |
| --- | --- | --- |
| 全量离线回归 | `uv run --locked pytest -q` | 1442 passed、45 skipped；默认跳过需显式授权的真实数据库测试，包含1条已有第三方弃用警告 |
| 确定性分析 + Web 服务合同 | `uv run --locked pytest tests/test_result_delivery.py tests/test_web.py -q` | 74 passed；精度、重复列、NULL、空集、截断、时间、100组限制、输入边界、保存/重启/删除/scope与导出注入 |
| 浏览器 HTTP 合同 | `npm --prefix frontend run test:e2e` | 18 passed（含原有12项）；这些是明确的合成 HTTP 替身 |
| 真实 MySQL 集成 | `DB_AGENT_MYSQL_INTEGRATION=1 uv run pytest tests/test_mysql_integration.py tests/test_mysql_query.py -q` | 42 passed；本地 MySQL 8.4.11、既有公开 fixture，只读业务验证，无权限变更或重新导入 |
| 构建与格式 | `npm --prefix frontend run build`、`npm --prefix frontend run format:check`、`uv run ruff check .`、`git diff --check` | 通过 |
| Python 打包 | `uv build --no-build-isolation`、`.venv/bin/python scripts/check_distribution.py` | wheel/sdist 通过，不含本地凭据、结果或前端构建产物 |

实现提交 `74e1e2ced7d7f1a1b3de305068f3b844eed4ff21` 已在 [CI 34564585740](https://github.com/erha1499/db-agent/actions/runs/34564585740) 完成五个 job：Python 3.11/3.13 离线回归、Chromium HTTP 合同、runner 上的真实 MySQL 集成与锁定构建安装均成功；收尾提交以 [PR #3](https://github.com/erha1499/db-agent/pull/3) 最新 HEAD 对应运行结果为准。

真实页面运行在独立 worktree 的 `127.0.0.1:8016`，仅用原项目明确授权的 `.env`（忽略的符号链接）与既有合成数据，未修改原项目、权限或数据库内容：

- **6 行订单 → 对比/趋势 → HTML/JSON → 重新打开历史**：真实 Agent 完成一次模型生成、需求核对、最终复核及受控查询，`ALLOW / completed`、未截断。独立预期来自 [mysql_business.sql](../tests/fixtures/mysql_business.sql)：paid 的 100+30+0 为 `130.00`，cancelled/refunded 各 `50.00`，pending 为 `0.00`；6个创建时间点首末差为 `-50.00`。下载 JSON 的原始结果逐项等于实际报告，HTML 包含图表、SQL和相同值。
- **空集**：真实请求 `orders.id = 999999`，返回0行、completed，页面不造数据点，下载保留列结构。
- **截断**：真实请求 `ec_orders.id >= 1000 AND id <= 1149 ORDER BY id LIMIT 150`，得到原查询预算下的100行（ID 1000–1099），`truncated / row_limit / server_statement_status=unknown`；分析与下载持续标记部分结果。首次探针误以为生成器ID从1连续增长，用1–150仅得到8行且完整返回，独立断言失败；保留这次记录，核对生成器固定边界后使用1000–1149，没有改产品、预算或预期来伪装截断。
- **桌面/手机与报告渲染**：核对真实页面、390px布局、图表与独立HTML。首次刷新脚本在未重新选择历史时寻找结果页签而超时；修正选择器后复用同一份已保存结果完成，不再次调用模型替换证据。

本机运行记录、原始合成结果与截图在忽略的 `outputs/delivery/`，脚本在 `work/`。这些资产不提交；停止本任务 Web 服务后可按精确目录自行清理，保留原项目数据库卷与工作区。没有重跑已暴露电商或会话冻结套件，不把局部报告/图表验收称为新增通用业务正确率；第三方 Starlette 弃用警告、提供方 token 计数与生产可靠性限制继续保留。最终完整 HEAD 对应远端 CI 与可合并状态以本任务 PR 交付为准。
