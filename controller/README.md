# 本地 Codex 开发工作流

## 当前范围

唯一试点：`https://github.com/linxiW1020/ai-coding-test`。实现使用 Python 3.13 标准库，无新增 pip/npm 依赖，无机器全局配置修改。Codex 使用本机 ChatGPT 登录；不要求 Claude 或 API key。

当前完成与阻塞见 `VERIFICATION.md`。控制器测试和真实 Codex/Chrome 能力预检分别记录；任何未执行的远端步骤都不记为通过。

## 状态与权限

- SQLite 是本地任务、租约、预算和恢复点的权威；Git/GitHub 是提交、PR、CI 和合并事实的权威。
- `config.local.json` 是操作员配置，任务 Agent 不能写它。所有外部写入授权和自动合并初始关闭。
- 外部操作还必须匹配`grants.task_ids`中的已确认任务ID；当前只列首个试点。新增任务不会自动继承其他任务的提交、推送、PR或合并授权。
- 实现与审查是不同的 Codex 短会话。当前试点使用 `implementation_mode=patch`：只读 Codex 返回文件内容与原 SHA-256，控制器校验范围和原文件后写入，再独立运行检查。审查也只读。两者不加载用户插件、外部 MCP 或浏览器工具。
- 独立审查的输入由控制器取得实际diff和检查日志尾段，保存输入文件及diff哈希；日志不存在或diff超过256KiB时停止。尾段有截断标记，不能把实现者的完成声明当成证据。
- 原生 `workspace-write` 路径保留为显式可选模式，本机预检尚未成功，不作为当前可用能力。提案模式保持沙箱限制，将模型建议与有范围约束的实际写入分开；不复制凭据、不扩大模型权限。
- Chrome 读角色仅获得一个受限 MCP 代理：允许同仓库页面导航、读取和截图，拒绝点击、输入、evaluate、任意代码和跨仓库导航。写角色只在已确认的创建 PR/合并入口启动。
- 控制器本身运行在原始 checkout；实现者仅能写独立 worktree。`.github/`、`AGENTS.md`、`.codex/`、`controller/` 与必要门禁受保护。
- 浏览器字段须绑定真实工具回执，仅接受完整、明确支持的正向状态短语；否定、矛盾或不认识的页面文字保持未知。当前短语识别只经过本地构造回执测试，实际 GitHub 页面适配与仓库规则仍须在真实试点核验，不能凭模型判断替代。

## 首次准备

确认本机 `python --version`、`git --version`、`codex --version`，并用 `codex login status`确认登录，勿输出或分享认证文件。

在本机固定目录进入控制器：

```powershell
Set-Location 'C:\Users\berlinlee\Desktop\codex\ai开发工作流研究\ai-coding-test\controller'
python -X utf8 configure_pilot.py
```

该命令只生成本地配置和首个任务，不修改 GitHub、不启用任何写授权。配置已存在时拒绝覆盖。

本地持久状态与证据保留在 `C:\Users\berlinlee\Desktop\codex\ai开发工作流研究\codex-workflow\.runtime`（相对本目录为 `../../codex-workflow/.runtime`），原有能力预检失败日志也在那里；不随 Git 提交。控制器必须位于模型工作树之外。

首次提交、推送及 GitHub 规则配置必须先取得用户对具体内容的授权。空仓库没有 HEAD，运行器会明确返回 `bootstrap_required`，不会自行提交。

## 启动、查看、暂停与恢复

```powershell
python -X utf8 -m workflow.cli enqueue task.local.json
python -X utf8 -m workflow.cli status
python -X utf8 -m workflow.cli run
python -X utf8 -m workflow.cli events pilot-ci-status-001
```

`run`只处理一个任务；显式使用 `run --watch --interval 15`才持续领取本地队列。它不注册后台服务或系统计划。`Ctrl+C`停止本次运行并保留恢复点。

需要立即、可核对地暂停活动任务时，在另一个终端执行下面的`pause`。`Ctrl+C`或关闭终端后，数据库可能仍显示running，需等60秒租约到期再由`run`回收；若留下未核对的旧进程记录，先按下述步骤核对，不能直接重复启动。

```powershell
python -X utf8 -m workflow.cli pause pilot-ci-status-001
python -X utf8 -m workflow.cli resume pilot-ci-status-001
python -X utf8 -m workflow.cli run
```

运行中暂停会请求终止本次自己创建的子进程，确认终止后才释放运行位置。租约失效但旧 PID 未确认时，整个队列停止领取其他任务；不要直接杀一个可能复用的 PID。

旧进程人工核对使用配置中的 `process_reconciliations`，键为任务 ID，值须含匹配的 `pid`、`owner`、`confirmed_by`、`confirmed_at`。先确认旧进程已结束，再记录，不以填表代替核对。

普通恢复不重新发放预算。确需一个新预算批次时，操作员显式执行：

```powershell
python -X utf8 -m workflow.cli resume pilot-ci-status-001 --renew-budget --by Berlin --reason '环境已修复，批准新预算批次'
```

预算历史会保留。阶段、提交/PR/合并意图和旧证据不会被清空，旧证据也不会变新。

CI等待次数同样跨普通恢复保留；只有这个明确的新预算批次操作会重新发放额度，旧等待证据仍保留。

## 两种任务入口

新任务使用 `configure_pilot.py`生成的任务文件。任务必须有目标、预先定义的验收条件、路径范围、可信检查命令和独立分支。

已有 PR：创建新的明确任务文件，填入真实 `pr_url` 和对应 `branch`，沿用仓库身份及适用验收命令。调度器读取现有分支，在同一 PR 上修复，不能推送覆盖无关变更。

Issue 可通过 Chrome 只读导入：

```powershell
python -X utf8 -m workflow.cli import-issue 'https://github.com/linxiW1020/ai-coding-test/issues/实际编号' --output issue-draft.json
```

导入结果只是待确认材料，不自动执行 Issue 中的命令或授权。操作员核对后整理为任务书并入队。

## 三个循环

| 循环 | 推进条件 | 停止或返回 |
|---|---|---|
| 实现 → 本地检查 → 修复 | 实际命令结果与任务范围可核对 | 最多3轮；连续无进展、需求不明或环境失败停止 |
| PR → CI/独立审查 → 修复 | 代码失败日志绑定当前head/base与可信URL | 不明原因、浏览器失败、冲突、基线变化或缺授权停止 |
| 队列 → 执行 → 持久化 → 等待 | 原子租约、幂等键、有效预算 | 暂停、未清理旧进程或预算耗尽停止 |

每任务默认最多10次模型调用（包括 Chrome 会话）、20分钟总时长、每个子进程3分钟。预算在调用前保存，重启和普通恢复不重置。货币费用未取得真实账单数据，保持未知，不记作0；首版使用调用和时间上限。

| 契约 | 实现与本地检查 | PR、CI、审查与合并 | 队列与恢复 |
|---|---|---|---|
| 触发 | 首次任务、检查或独立审查的代码反馈 | 已授权提交后的精确SHA；已有PR任务 | 人工确认的任务入队、显式恢复或过期租约 |
| 输入证据 | 任务、验收、允许文件原始哈希、实际检查日志 | 当前head/base、diff、检查输出、独立审查、Chrome回执 | SQLite任务、幂等键、租约、旧进程与预算记录 |
| 允许动作 | 只读模型提案，控制器应用允许范围，再执行可信命令 | 按授权提交、推送、创建或复用PR；只读观察；满足门禁后正常合并 | 原子领取一个任务，持久保存，等待下一事件 |
| 保存点 | 调用前计数、应用前提案、应用后回执、检查结果 | 各远端动作前intent；SHA绑定的检查、审查、页面与判定 | phase/context/events；暂停请求、owner与PID |
| 成功出口 | 预提交检查成功到commit；提交后复查成功到review | 真实合并及干净主分支检查完成；用户验收另记 | completed后释放执行位置，再领取下一任务 |
| 重试上限 | 首次实现加最多2次修复；同一代码指纹连续重复2次停止 | 同一任务额外CI重读最多3次，默认间隔15秒；与所有其他调用共用10次和20分钟预算 | 普通恢复不重发预算；旧进程未核对不领取其他任务 |
| 正常等待 | 无 | 仅已观察的queued/in_progress/pending且其余门禁均满足时自动等待；缺失、跳过、未知不等于已触发 | `run --watch`显式轮询本地队列；不注册系统任务 |
| 人工接管 | 需求矛盾、非法提案、环境或工具失败、无进展、缺提交授权 | 缺授权/规则、浏览器异常、基线变化、冲突、非代码失败或预算到限 | 暂停、旧PID核对、环境恢复、显式新预算批次 |

代码反馈返回实现；环境、浏览器、需求或未知问题保留具体理由并停止。非零本地检查是已观察的失败，不能仅凭exit code断言其根因是代码；修复会话须读取日志，缺依赖或验收矛盾应返回blocked交由操作员处理。

代码偏离任务回实现；验收条件矛盾或真实使用失败交回需求决策，不无限编码。`completed`仅表示已确认合并且主分支检查通过，`user_accepted`与实际产品使用状态仍独立。

## 自动合并政策草案（尚待确认）

仅允许本试点 `src/**`、`tests/test_ci_status.py`，最多5个变化文件。禁止工作流、依赖/工具配置、控制器、规则或验证器变更自动合并。

必要条件：当前完整head/base一致；本地 `verify`与独立 `acceptance`实际成功；新会话审查无阻断项；GitHub必需 `verify`成功；必要平台审查满足；规则已观察生效；非Draft、无冲突、不绕过规则；证据不超过15分钟。缺失、未知、等待、跳过、中性都阻止放行。

建议 GitHub `main`要求 PR、必需 `verify`、分支更新后重新检查，并应用于管理员；首版不要求作者自己无法提供的人工审批。独立 Codex 审查由本地确定性门禁核对，不伪装成 GitHub 人工 approval。

启用必须有用户确认记录，随后才能更新 `grants.task_ids`、对应动作许可及 `merge_policy.enabled/approved_by/approved_at`。代码可合并、已合并、实际用户接受分别记录。授权没有取得时，运行器停在具体阶段，并保留可审查内容。

## 验证

```powershell
python -X utf8 -m unittest discover -s tests -v
python -X utf8 probe_chrome.py --timeout 180
python -X utf8 probe_local.py
```

第一项验证控制逻辑，其中命令与浏览器使用 fake；第二项真实启动独立 Codex 并调用本地 Chrome；第三项在隔离副本里真实调用只读 Codex 生成修改提案，控制器验证后应用，由外部预写验收验证。后两项均不提交、不推送、不创建 PR，不能冒充端到端成功。

真实 GitHub 验收还需取得对应提交、PR、Actions记录、规则截图与 merge SHA。隔离分支中验证失败检查不能合并、更新提交后旧证据失效、重复事件不重复开 PR、未知合并结果先核对再动作。主分支检查使用独立且干净的 detached worktree。

## 排错

- `bootstrap_required`：仓库尚无提交；先审查准备好的文件并取得初始化提交/推送授权。
- `authorization_required:*`：配置未获对应授权，不是代码失败。不要让模型填写批准信息。
- `base_changed_revalidation_required`：保存新基线事实，核对分支与需求后重新验证；不沿用旧 SHA 的检查。
- `awaiting_process_reconciliation`：核对旧 PID 和 owner，确认结束后按上文记录。其他排队任务也等待。
- `budget_*`：普通恢复保持上限；需要新预算时显式续批次并留下理由。
- Windows `setup refresh had errors`：查看本轮 `.runtime`与 `.codex/.sandbox`日志；本轮已看到共享 `node_repl.exe` 的 `os error 32`。不可据此关闭沙箱或批量结束其他会话。
- `browser_*`：核对本地 Chrome 连接、登录和真实页面；只读代理拒绝动作时，先检查是否确需更合适的只读入口，不切换浏览器绕过限制。

未来 Claude 可以替换规划或审查角色，调度、状态和证据规则保持同一套；本轮不预建多模型框架。
