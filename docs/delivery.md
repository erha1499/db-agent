# 持续集成与可复现交付

本文说明如何从公开源码安装、在明确目标上验收，以及如何读取 CI 和提供方探针的证据。CI 不持有模型凭据，不自动运行真实模型评测，也不连接开发者本机或生产数据库。

## 自动回归覆盖什么

[GitHub Actions CI](../.github/workflows/ci.yml) 在 PR、main 推送和显式手动触发时运行。使用 Ubuntu 24.04、固定 uv 0.7.6，第三方 Action 绑定完整 commit SHA；权限仅为 `contents: read`，不使用 `pull_request_target` 或持久化 Git 凭据。每个 job 有总时限，同一 PR 的新提交取消旧运行。

| Job | 实际验证 | 不代表什么 |
| --- | --- | --- |
| Offline / Python 3.11、3.13 | 锁文件一致性、Ruff、全部默认 pytest、未意外改动跟踪文件 | HTTP 和数据库替身不代表真实提供方或 MySQL |
| Frontend / Chromium contract | Node 26.7.0、npm 11.19.0，`npm ci`、格式检查、TypeScript/Vite 构建和 Playwright Chromium 合同测试 | 合成 HTTP 替身不代表真实模型/数据库业务链路 |
| MySQL 8.4 / real integration | 在该 runner 新建 Compose 服务，初始化三张公开合成表；以 reader 验证元数据、权限、EXPLAIN、结果与清理，另以固定本地管理员 socket 验证随机表事务和元数据锁 | 不是百万规模评测、生产部署或任意数据源验收 |
| Locked build and installed CLI | 从锁文件安装构建依赖，构建 sdist 和 wheel；核对没有本地配置或运行资产；在新虚拟环境按哈希安装 runtime 依赖与 wheel，运行两个 CLI 入口 | 不发布 PyPI，不保证不同操作系统下产物逐字节相同 |

MySQL 使用项目 [Compose](../compose.yaml) 绑定的 MySQL 8.4.11 镜像摘要、`127.0.0.1:13306` 和本地 socket。临时密码在 runner 内随机生成；不从仓库 Secrets 获取真实数据库或模型配置。结束时停止服务并保留卷直到 runner 回收，不对本机执行清卷。真实 MySQL 的两个开关仅在各自集成步骤开启，默认测试仍跳过这些测试。

判定交付时先核对 PR 的完整 HEAD SHA，再读取该 HEAD 触发的全部 job。PR 工作流默认验证 GitHub 生成的合并结果；HEAD 对应运行成功和 PR 可合并状态需同时成立。旧 SHA 的绿色运行不能证明新提交通过。CI 规则覆盖了什么与仓库是否启用分支保护是两件事；此流程不修改仓库保护或权限。

2026-09-11 的完整实现提交 `848fa724d8d4c9756d2728481a116485588b2e82` 已在[远端运行 34563655206](https://github.com/erha1499/db-agent/actions/runs/34563655206)通过全部五个 job：Python 3.11.12/3.13.3 各1395 passed、45 skipped；MySQL集成42 passed、事务锁3 passed；Node26.7.0/npm11.19.0 的Chromium合同12 passed；sdist111项、wheel27项检查和新环境安装通过。默认跳过的45项由真实MySQL两个步骤单独运行，不是漏掉后计为通过。Python有1条现有Starlette对AnyIO旧别名的弃用警告，不影响断言。前端 HTTP 合同、离线提供方替身、真实MySQL和下文真实模型探针分别保留边界；此处没有重新宣称百万规模业务评测通过。后续文档收尾提交及最新状态以 [PR #1](https://github.com/erha1499/db-agent/pull/1) 当前HEAD的运行结果为准。

## 安装与构建复现

快速开发使用 Python 3.13 和 `uv sync --locked`。包声明的最低版本为 3.11，CI 同时回归 3.11 与 3.13；其他 Python/操作系统组合仍需实际验证。`--locked` 要求 `pyproject.toml` 与 `uv.lock` 一致，不能用 `--frozen` 掩盖过期锁文件。

```bash
git clone https://github.com/erha1499/db-agent.git
cd db-agent
# 复现交付时 checkout 已审查的完整 commit SHA，然后记录 git rev-parse HEAD
uv sync --locked
cp .env.example .env
```

只运行直接 `db` CLI 时，仅填写数据库配置即可；真实模型只从当前目录 `.env` 的明确配置加载，缺省字段才回退到环境变量。不要用 `source .env`，不要把模板空密码当成已配置账户。表白名单默认为空，必须由操作者明确填写。数据源初始化、已有卷改密和合成数据流程见 [README](../README.md#本地-mysql)。

需要复现 wheel 安装时，从干净 checkout 执行以下步骤。构建后端及其依赖也由 `uv.lock` 的 `build` group 约束；`--no-install-project` 避免构建依赖尚未准备时先执行 editable build。

```bash
uv sync --locked --group build --no-install-project
uv build --no-build-isolation
.venv/bin/python scripts/check_distribution.py
uv export --locked --no-dev --no-emit-project --output-file dist/requirements.txt
uv venv work/package-venv
uv pip install --python work/package-venv/bin/python --require-hashes -r dist/requirements.txt
uv pip install --python work/package-venv/bin/python --no-deps dist/db_agent-*.whl
uv pip check --python work/package-venv/bin/python
work/package-venv/bin/db-agent --help
```

完成后可用 `uv sync --locked` 恢复开发环境。`dist/`、`work/`、`outputs/` 都不提交；构建步骤不执行上传。sdist 使用明确清单，CI 还放入合成的 `.env` 和 `outputs` 文件再检查产物，防止打包边界退化。安装/运行记录应保留 commit SHA、uv/Python 版本、锁文件 SHA256 和命令结果；锁定依赖版本不等于锁定主机内核或远端服务行为。

源码包包含 `frontend` 源码、配置与 `package-lock.json`，排除 `node_modules`、前端 `dist`、Playwright 运行产物；CI 放入这些目录的合成文件后核对清单。Python wheel 只安装 Python 入口，不携带已构建页面。从源码启动 Web 时另用 Node 26.7.0/npm 11.19.0 执行 `npm --prefix frontend ci --registry=https://registry.npmjs.org` 与 `npm --prefix frontend run build`，再按 [Web 说明](web.md)启动。前端回归入口为 `npm --prefix frontend run format:check`、`npm --prefix frontend run test:e2e`；首次运行需在 frontend 目录执行 `npx --no-install playwright install --with-deps chromium`。浏览器测试本身不需要模型或数据库。

前端锁文件的全部 resolved 地址使用公开 npm registry，CI 在安装前检查公开 HTTPS 地址和 integrity，并显式设置 `NPM_CONFIG_REGISTRY`。首次完整远端运行曾被旧锁文件中的内部镜像地址阻断；已逐项核对176个锁定包的公开版本及对应 SHA512/SHA1 完整性值，仅替换下载 URL，不升级包或改写原哈希。[npm 对 registry 的定义](https://docs.npmjs.com/cli/v11/configuring-npm/package-lock-json/)说明公共 registry URL 会使用当前配置的 registry，因此在配置过其他镜像的机器上复现时仍要显式指定上述参数。

## 在明确目标上逐层验收

1. **本地规则**：`uv run --locked pytest -q`、`uv run --locked ruff check .`、`git diff --check`。核对通过/跳过数量，不能把默认跳过集成测试写成数据库通过。
2. **数据库身份与环境**：按 README 启动固定 Compose、仅在空表目标初始化 fixture；`uv run --locked db-agent db check`。核对目标、MySQL 版本、只读账号与显式白名单。
3. **真实业务读取**：`DB_AGENT_MYSQL_INTEGRATION=1 uv run --locked pytest tests/test_mysql_integration.py tests/test_mysql_query.py -q`。断言来自独立的公开 fixture，包含明确的拒绝与超限。
4. **事务/锁**：仅在属于当前 checkout 的固定本地 Compose 上，`DB_AGENT_MYSQL_DDL_INTEGRATION=1 uv run --locked pytest tests/test_mysql_query_isolation.py -q`。它会建改并清理独有随机测试表；不要为通过它而挪用另一 worktree 的容器或放宽归属检查。
5. **模型协议与业务**：明确配置后先运行下节探针，再按 [电商评测](ecommerce.md) 或 [会话评测](../evals/CONVERSATIONS.md) 显式执行。已经暴露的题重跑仍是回归；修改产品后不得沿用旧冻结哈希声称独立验收。

规则成功、数据库连接成功、工具协议完整、SQL 完整执行和业务预期正确分别记录。没有凭据或环境时继续其他步骤，将缺少的证据标为未验证，不自动降级或更换目标。

## 提供方协议、超时与 token 核验

[探针](../scripts/check_provider.py) 仅通过当前配置模型发送固定公开合成请求，无数据库访问，不是产品工具。没有 `--run` 时拒绝调用模型：

```bash
mkdir -p outputs/provider
uv run --locked python scripts/check_provider.py --run > outputs/provider/protocol.json
uv run --locked python scripts/check_provider.py --run --mode token-limit > outputs/provider/token-limit.json
uv run --locked python scripts/check_provider.py --run --mode http-timeout > outputs/provider/http-timeout.json
uv run --locked python scripts/check_provider.py --run --mode total-timeout > outputs/provider/total-timeout.json
```

每次运行有独立的原配置总时限和调用预算：protocol 最多两次，其余模式一次，均不自动重试。protocol 检查普通文本和 function calling；后者要求唯一、完整、固定值正确的调用、无正文混杂、非截断终态，并对照 LangChain `include_raw` 返回的 `AIMessage` 和解析对象。这个层次不等于保留原始 HTTP 响应正文。token-limit 只将输出请求上限缩小到 `min(64, 原配置)`，用较长合成回答观察截断；不提高原有预算。

HTTP 事件钩子只记录实际发出的 token 字段、工具数量、非流式标记和 HTTP 状态；响应只保留允许的协议状态、数字计数与耗时。报告记录 UTC 时间、探针/锁文件哈希、依赖版本以及端点/模型哈希，不打印端点、API key、响应正文、原始工具参数或异常文本；tracing 关闭。HTTP/total 两种模式只将对应时限缩小到不超过 0.01 秒，用于区分网络客户端超时和整个 await 的总时限，不是默认 30/60 秒等待的性能实测。

`protocol` 退出码 0 仅表示本次合成协议兼容；`token-limit` 退出码 0 仅表示取得响应，是否出现 length 由 `length_finish_observed` 独立报告，超请求计数也不会被改写成通过。两种超时模式退出码 0 仅表示对应客户端超时被观察到。缺失 usage 返回 null，不填零；`output_counters_agree` 对照提供方与框架计数，`reported_output_exceeds_requested` 独立报告是否超请求。出现客户端超时与关闭连接，不能证明请求已经到达提供方，更不能证明服务器停止生成或不再计费。不会因某个模式退出码为 0 就把 `hard_token_limit_verified`、`billing_verified` 或 `provider_server_cancellation_verified` 改为 true。

当前运行时仍只向 ChatOpenAI 传递请求输出上限，不按返回 usage 实施本地硬上限。本次样本及历史业务诊断需分别保留：短样本没有超限不能推翻[此前超过请求输出计数的观察](ecommerce.md#2026-09-11-v8-最终业务验收)。未有提供方独立计数说明、服务器侧证据和账单前，不宣称 token 硬上限、reasoning 与正文的计费关系或真实费用已验证。

### 2026-09-11 提供方实测

在当前明确配置提供方上，04:37:31–04:37:37 UTC 显式运行上述四个模式（共五个计划请求，无业务数据输入）。版本为 langchain-openai 1.6.2 / openai 3.11.0 / httpx 0.28.1；HTTP 时限 30 秒、总时限 60 秒、原输出请求上限 1024，探针不改变产品配置。

| 样本 | 实际请求与观察 | 结论 |
| --- | --- | --- |
| 普通文本 | 请求 1024；HTTP 200、stop；output/completion 131、reasoning 125；1140 ms | 本次普通响应协议可用 |
| function calling | 请求 1024；HTTP 200、tool_calls；唯一完整调用，output/completion 95、reasoning 35；801 ms | 本次结构化协议可用 |
| 低上限长回答 | 实际 `max_completion_tokens=64`；HTTP 200、stop；output/completion **446**、reasoning 46；1747 ms | **返回计数超过请求上限，未观察到 length 截断** |
| HTTP 超时 | HTTP 时限缩为 0.01 秒；请求事件 1 次、无响应状态；30 ms 报 http_timeout | 客户端网络超时被观察到，未定位连接/读等具体阶段 |
| 总超时 | 总时限缩为 0.01 秒；请求事件 1 次、无响应状态；18 ms 报 total_timeout | 整个 await 的总时限被观察到 |

三个成功响应的提供方 completion 计数与框架 output 计数一致；这是同一响应的两种表示，不是独立计量。全部模式均确认自有客户端关闭、重试为 0。请求事件仅表示进入 HTTP 发送流程，不证明提供方已接收。此前开发中同一普通短提示还观察到 output=21；最终加入探针/锁文件指纹后重新运行得到上述131，两个样本均保存在本地，不把短样本变化解释为费用或性能统计。

本次探针 SHA256 为 `522b2de430545698f4e85f9cd0f2a6657dd6ca6f41355b21ed34fc49fc8b3bab`，当时锁文件 SHA256 为 `ffff39a7a9c040fa0f8839d46f1fb8e6da66c21e3feb2c85e301f512a308ef21`。报告保留完整端点/模型哈希及上述原始关键字段于忽略的 `outputs/provider/final-*.json`，不提交模型标识、端点或响应正文。

合入 Web 后，在提交 `8749cb8cafe9116fa6fc8bc5783e01f915c49cd0` 的整合环境于04:42:40–04:42:46 UTC 再次显式执行四个模式。探针及模型依赖版本未变，锁文件 SHA256 为 `79f4dfc96be2ddcf21ae394253dd37959b2b9e6311181ccd2daec0a2a96c4b61`。普通/工具两次协议仍通过，output分别36/78；低上限样本再次实际请求64却返回**513**（reasoning113、stop）。HTTP超时30 ms；总超时11 ms且未出现发送事件，因此后者只能证明派发前总时限生效。自有客户端全部关闭，报告为 `outputs/provider/integrated-*.json`。这次追加复核保留了前一次446的异常，不以新样本替换旧结果。

这次核验确认了协议与客户端超时行为，并重现了计数超过请求上限的异常；没有证明提供方 token 硬上限、计费规则或服务器取消。该限制保留为交付边界，不能通过上调预算、修改风险规则或把观测退出码写成硬上限验收成功来消除。

参考实现语义：[uv 锁定与同步](https://docs.astral.sh/uv/concepts/projects/sync/)、[uv GitHub Actions 集成](https://docs.astral.sh/uv/guides/integration/github/)、[GitHub 工作流权限与语法](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)。本文的项目结果以实际运行记录为准。
