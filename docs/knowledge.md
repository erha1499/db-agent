# 业务知识与跨会话记忆

本机业务知识保存经人工确认的指标口径、数据库声明的表关系及 SQL 模板。它与 `session` 的临时用户请求、Web 历史和查询结果分开保存。关闭命令、开始新进程或新建 Web 会话后，在同一个项目目录和当前授权范围仍可通过知识 ID 显式引用。Web用户通过[知识面板与身份配置](web-access.md)管理自己的知识，CLI知识不自动分享给Web用户。

## 创建、审阅与确认

管理入口为本机 CLI 和登录后的 Web 知识面板；这些管理操作不调用模型，确认时需要当前只读数据库配置。两种入口的知识范围隔离，Web 面板操作只作用于当前可信身份。Agent 不提供创建、确认、撤销知识的工具，聊天中的“记住这句话”不会自动写入。只应手工录入可向配置模型提供的业务定义；不要录入业务行、秘密、旧结果或执行批准信息。系统不会从模型回答、SQL 报告或业务行自动提取记忆。

在项目根目录运行以下公开合成例子。`expires_at` 必须是未来且包含时区的时间，请根据维护周期填写；示例时间不是永久有效承诺。

```bash
uv run db-agent knowledge create --stdin <<'JSON'
{
  "kind": "metric",
  "title": "支付成交额",
  "definition": "支付成交额为status='paid'订单total_amount之和，按paid_at归属日期，区间左闭右开；不包含取消和已退款订单。",
  "source": "tests/fixtures/mysql_business.sql",
  "source_version": "public-small-fixture-v1",
  "invalidation_condition": "支付状态语义或指标口径变化时由维护者撤销并创建新版本。",
  "expires_at": "2026-12-31T23:59:59+08:00",
  "tables": ["orders"]
}
JSON
uv run db-agent knowledge list
uv run db-agent knowledge show <id>
uv run db-agent knowledge confirm <id> --digest <完整digest>
```

`create` 只保存不可变草稿。`show` 返回完整内容、来源、范围摘要、生命周期及 digest；维护者核对定义与来源后，把所见完整 digest 交给 `confirm`。摘要只绑定内容，不证明来源文档真实或业务口径正确。确认会读取当前授权表结构并保存指纹，不执行模板 SQL。失败不置 confirmed。不能原地改写或重新确认已经确认/撤销的版本；需要修改时撤销旧 ID，创建并确认新的版本，查询改用新 ID。

每条必须包含以上通用字段。`kind` 的三种取值及额外字段：

| kind | 额外字段 | 确认检查 |
| --- | --- | --- |
| `metric` | 无 | 指标定义由维护者确认，记录当前表结构 |
| `sql_template` | `sql`：一条完整、无未绑定参数的 SELECT 范例 | 当前静态策略与授权、实际表和字段匹配；不取得执行许可 |
| `relationship` | `relationship`：下列列对对象 | 必须匹配当前授权范围内完整声明外键；不接受猜测关联或部分复合键 |

关系例子使用 `tables: ["orders", "customers"]`、`kind: "relationship"`，以及：

```json
{
  "relationship": {
    "table": "orders",
    "columns": ["customer_id"],
    "referenced_table": "customers",
    "referenced_columns": ["id"]
  }
}
```

其余通用字段仍必填。当前只持久化可与数据库声明核对的关系；无外键的人工逻辑关系暂不支持。即使外键已声明，也不能推导所有历史业务数据都符合业务含义。

## 在全新会话取用

```bash
uv run db-agent chat '[[knowledge:<id>]] 统计2026年2月支付成交额，只返回笔数n和金额amount。'
uv run db-agent chat '[[knowledge:<id>]] 本次改按created_at统计2026年2月，其他成交口径不变，只返回笔数n和金额amount。'
```

把 `<id>` 替换为真实的 32 位知识 ID。`chat`、`session` 和 Web 智能查询共用此入口；在 Web 中新建对话并输入同样标记即可。管理 UI 尚未提供，管理仍使用 CLI。`db query` 和 SQL 诊断不解释知识标记，继续按原样 SQL 的直接入口处理。

检索采用“当前范围下 `list/show` 查找 → 本次问题精确引用 ID”，没有向量检索、隐式自动注入或任意文件读取。最多引用 3 条，并集最多 2 张表；记录最多 1000 条、每份创建 JSON 最多 8192 字节、注入知识 JSON 最多 16384 字节。每条可声明最多 3 张表，但超过本次并集预算的知识不能用于 Agent 查询，不会截掉一部分后执行。

主生成、独立需求提取和最终复核接收同一知识内容及摘要；资料以用户数据传递，不进入系统指令，不伪装成当前结构。当前明确要求覆盖旧默认值，缺口或冲突不能解决则停止。SQL 模板可以按当前日期、筛选和分组生成新的候选，原模板不替代完整需求核对；用户明确提供的原样 SQL 也不能被模板覆盖。

同一 `session`/Web 连续查询中，曾使用知识的请求之后，每轮都须显式选择本轮知识；缺少引用会停止并要求重置或新建会话完整重述。历史里的旧 ID 不会自动取用；更换知识时本轮明确引用新 ID，旧定义不会回放。没有知识标记的全新请求不会加载任何知识。

查询报告的 `business_knowledge` 保留本次引用的 ID、摘要、标题、来源/版本、确认和到期时间；确定性回答展示引用来源。它证明本次提供了哪些资料，不独立证明模型完整采用或正确理解；效果还要核对最终 SQL 与实际结果。`outputs/runs` 只增加摘要关联事件，不记录知识正文。

## 隔离与失效

记录保存在项目目录的 `outputs/knowledge/knowledge.sqlite3`，目录权限 0700、文件 0600，不纳入 Git。它与 `outputs/web/conversations.sqlite3` 独立；删除 Web 对话不会删除业务知识。目录体现项目隔离，记录进一步绑定连接主机、端口、数据库、账号及完整白名单的摘要。密码不持久化；换密码不改变范围，修改上述任一目标/权限配置都不能取用旧记录。以上是CLI范围。Web另绑定可信用户与持久化授权代际，任何已观察配置变化都会使旧Web范围失效，恢复旧配置不复活旧条目；未提供加密存储或异地同步。

每次引用先检查 confirmed 状态、到期时间、范围和原内容摘要，再通过受控连接器读取本次结构核对确认时指纹。结构包括字段、索引与完整授权外键。SQL 进入原 QueryService 后仍须通过完整静态预检、普通 EXPLAIN、对象检查和只读事务校验。普通 EXPLAIN 之后、SELECT 派发之前，在该事务连接上再次读取知识表结构、重查知识生命周期；这一附加检查只可以阻止执行，不能放行 BLOCK/REVIEW/UNKNOWN。查询使用到的表保留原 EXPLAIN 的元数据锁约束。

所有实际 describe 都计入原 6 次工具预算；主生成/合同/复核仍共用原 4 次模型调用、60 秒总预算及每请求 1024 token 配置。知识本身不增加预算；单表常见路径是 3 次模型、3 次工具，双表是 3 次模型、5 次工具。提供方曾返回超过请求上限的计数，因此 1024 仍是请求参数，不是已证实的硬费用上限。

以下情况整包停止，不静默忽略失败知识、不回退旧版本：未确认、未知 ID、到期、撤销、数据源/白名单变化、结构指纹不符、存储故障或预算不足。

```bash
uv run db-agent knowledge revoke <id> --reason '支付状态语义已改变，需要重新定义'
```

`invalidation_condition` 是人工维护的业务失效条件，不是可执行表达式。系统不会抓取来源链接或自动识别状态语义变化；维护者必须撤销并重新确认。相同地址/账号/结构下替换了数据库内容，或业务规则改变但结构未变，也不能仅由指纹自动发现。失效校验是派发前的一次本地检查，并非跨 SQLite/MySQL 的原子事务；撤销不会取消已经派发的 SQL，也不声称服务器已经取消。

## 验证与证据

```bash
uv run pytest tests/test_knowledge.py tests/test_agent_knowledge.py tests/test_query_connector.py -q
DB_AGENT_MYSQL_INTEGRATION=1 uv run pytest tests/test_mysql_integration.py tests/test_mysql_query.py -q
uv run python scripts/evaluate_knowledge.py --run
```

离线测试使用明确的 HTTP、结构和查询替身，覆盖精确确认、来源/范围隔离、模板字段、完整外键、当前请求包、撤销/到期/结构变化、故障与预算，以及连接器在 EXPLAIN 后拒绝派发。它们不能代替真实模型语义或数据库验证。

真实验收脚本只允许本地 `127.0.0.1:13306/db_agent` 的 `db_agent_reader` 与固定公开 fixture。它以新进程分别创建/查看/确认知识，并为每道查询再开新进程；参考结果只用于运行之后验收，不提供给 Agent。只新增本地合成知识与忽略的报告，不改业务数据、账号权限或规则。脚本显式调用真实模型，默认 CI 不运行；来源代码和 fixture 哈希前后核对，运行中变更则停止。报告在 `outputs/evals/knowledge-<run-id>.json`。这些固定题是公开验收/回归，不能称无偏正确率。

2026-09-11 首次真实运行 `c2470fc928694ca284174195bc801775`：4 个新进程查询 4/4 完成，撤销后新请求明确拒绝。四项结果依次为：

| 场景 | 独立 fixture 预期 = 实际行 | 模型 / 工具调用 |
| --- | --- | --- |
| 二月支付成交额 | `[[3,"130.00"]]` | 3 / 3 |
| 当前明确改用创建时间 | `[[2,"30.00"]]` | 3 / 3 |
| SQL 模板改为一月 | `[[0,null]]` | 3 / 3 |
| 声明关系关联 east 客户 | `[[2,"130.00"]]` | 3 / 5 |

首次运行尚未保存源码哈希，作为开发验收保留。整合 main `878d6d61add66da7eddd214e0d379dc4ba7ccc91` 后的最终源码冻结回归 `09222087a2f64d10b7e2a73dec4c5e10` 再次取得 4/4，撤销后拒绝通过，产品/脚本/fixture 的源码核对 `source_unchanged=true`；逐项精确校验实际知识 ID/digest，模型/工具次数仍为 3/3、3/3、3/3、3/5。

本地真实 MySQL `tests/test_mysql_integration.py tests/test_mysql_query.py` 共 44 项通过，包括在真实只读事务里读取知识表结构及撤销后不派发 SELECT。额外尝试的 DDL/元数据锁专用 3 项在准备阶段拒绝：现有 Compose 容器属于原项目目录，不能被当前 worktree 当成自身环境使用；没有创建测试表、改变容器标签或放宽校验。该专用环境验证由 PR 的新建 Compose CI 核对，不能把本地拒绝说成通过。

这些证据验证公开合成场景中的持续使用效果，不代表任意知识正确、模型永不误解或生产环境已部署。
