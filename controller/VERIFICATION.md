# 本地验证快照与后续执行记录（2026-10-22）

用户已于2026-10-22回复“授权”，允许按`EXECUTION-PLAN.md`继续。下方实测事实是首次初始化前的本地验证快照；授权后的Git及GitHub执行状态以本机运行证据`C:\Users\berlinlee\Desktop\codex\ai开发工作流研究\codex-workflow\.runtime\execution-20261022.json`为准，不能把旧快照当作远端当前状态。

## 结论

本地控制器通过158项测试中的157项，1项因Windows符号链接权限跳过；真实Codex修改提案、外部验收和新会话独立审查组件已运行。该验证阶段尚无首次提交，自动化操作配置保持关闭。授权后的独立Chrome预检再次受到Windows沙箱初始化阻塞，真实GitHub端到端仍未完成。

## 实测事实

| 项目 | 结果与支持范围 |
|---|---|
| 本机CLI | `codex-cli 0.162.0-alpha.2`，ChatGPT登录，Python 3.13.14，Node 24.20.0；未检测到gh，不安装它 |
| 仓库 | 按用户URL成功克隆；本地与远端均无提交/分支HEAD。早先已在本地Chrome读取到该公开空仓库页面 |
| 控制器 | 最终158项：157通过、1项真实symlink创建测试因Windows权限跳过；真实Windows junction拒绝测试通过。包含有界CI等待/恢复、SHA失效、幂等、锁、预算、独立审查输入及否定页面文字拒绝。远端和模型调用为fake，不是GitHub运行结果 |
| 真实Codex修改提案 | 只读独立Codex生成2个文件提案，原SHA验证和写入回执存在；独立验收从exit 1变为0，修改后的单元测试exit 0。未执行Git写操作 |
| 真实独立审查组件 | 新的只读Codex会话接收实际diff、文件哈希及独立检查输出，返回pass/0阻断项，并指出一项非阻断测试覆盖建议。只适用于隔离组件，无commit SHA，不是PR审查放行 |
| 原生workspace-write | 两次真实组件预检均未成功写入。保持失败记录，不当作可用能力；当前使用提案/受控写入方式 |
| 独立Chrome入口 | 限制工具范围、修正UTF-8及MCP客户端请求转发后，仍在Windows sandbox初始化失败。未读到指定页面正文，不能记为通过 |
| 当前对话Chrome复查 | 后续两次同入口复查均返回无法加载浏览器请求头策略；未改用其他通道 |
| GitHub写权限与规则 | 公开仓库可读取并不证明有推送/改规则权限；当前会话、Actions是否可用及保护规则能否生效，仍待Chrome恢复后实际核对 |
| 实际PR、CI、合并、产品试用 | 未执行，URL/SHA/接受状态均保持未知 |

## 已保留证据

所有路径位于用户指定工作区，不提交运行时日志、数据库、认证或本地配置。

- 真实Codex提案与外部检查：`C:\Users\berlinlee\Desktop\codex\ai开发工作流研究\codex-workflow\.runtime\local-smoke\20261022T124003Z\summary.json`。同目录包含`application.json`、模型事件与前后检查输出。
- 真实独立审查组件：`C:\Users\berlinlee\Desktop\codex\ai开发工作流研究\codex-workflow\.runtime\review-smoke\20261022T125413Z\summary.json`。同目录保留实际`review-input.json`及其SHA-256、模型输入与结果；未引用实现者完成声明作为证据。
- 最终控制器测试：`C:\Users\berlinlee\Desktop\codex\ai开发工作流研究\codex-workflow\.runtime\verification\20261022T130207Z\receipt.json`。同目录`stderr.log`记录158项及跳过原因，耗时265.623秒；`source-sha256.json`记录运行时的Python源码与测试哈希。此前127/133项收据是中间版本，不代表最终版本。
- 最新独立Chrome预检：`C:\Users\berlinlee\Desktop\codex\ai开发工作流研究\codex-workflow\.runtime\probes\20261022T123701Z\result.json`及同目录事件/错误日志。
- 授权后重试：`C:\Users\berlinlee\Desktop\codex\ai开发工作流研究\codex-workflow\.runtime\probes\20261022T131710Z\result.json`，仍未读到实际URL、标题或正文；没有将本次尝试算作通过。
- Windows沙箱日志：`C:\Users\berlinlee\.codex\.sandbox\sandbox.2026-10-22.log`。实际记录过共享官方`node_repl.exe`访问权限更新遇到`os error 32`，这属于初始化阻塞，不能证明页面或代码本身失败。
- 本地运行器真实启动：SQLite中`pilot-ci-status-001`在`prepare`阶段以`bootstrap_required`停止，`model_calls=0`；没有自行提交。

复现最终控制器检查：

```powershell
Set-Location 'C:\Users\berlinlee\Desktop\codex\ai开发工作流研究\ai-coding-test\controller'
python -X utf8 -m unittest discover -s tests -v
```

## 关键异常的验收口径

以下预期在测试断言或试点执行前定义。控制器测试的Git/模型/页面为fake；SQLite、文件系统及子进程约束按对应测试实际运行，不能据此宣称GitHub页面已验证。

| 异常 | 预期结果 | 当前已取得证据 | 真实GitHub仍需验证 |
|---|---|---|---|
| 已知错误 | 实际检查非零；门禁拒绝，不执行merge | 构造样例外部验收exit 1；`test_failed_pending_skipped_neutral_unknown_and_unrun_checks_deny`及受限修复测试 | 隔离PR产生红CI，正常合并入口确实受规则阻止 |
| 更新代码或基线 | 旧SHA检查/审查失效，重新取得证据 | `test_checks_cannot_silently_change_head_and_reuse_evidence`、`test_base_change_invalidates_checks_and_review`、`test_stale_browser_sha_never_merges` | 真实新提交不得沿用旧CI和旧审查放行 |
| 重复触发 | 相同输入只产生一份有效任务；身份冲突拒绝 | `test_duplicate_trigger_reuses_original_task`与并发领取、部分push对账测试 | 同一分支/任务重触发不重复创建PR |
| 中断或远端部分成功 | 保存意图；恢复先核对，不能盲目重复写 | 租约fencing、旧PID闭锁、pending patch部分应用恢复、push/merge部分成功恢复测试 | 实际PR/merge结果不明时从真实页面对账 |
| 重试、预算或无进展到限 | 停止新模型调用和合并；普通resume不重置 | `test_model_call_budget_does_not_reset_on_resume`、`test_expired_deadline_survives_resume`、`test_no_progress_limit_stops_repeated_identical_repairs` | 用隔离任务及缩短预算重复验证远端动作不会越限 |
| CI仍在运行 | 保存等待点并有界自动重读；持续等待到限停止 | `test_ci_wait_queued_running_pending_then_success_completes_automatically`、持续pending到限、暂停/恢复、时间与模型预算测试 | 真实Actions从queued到成功的页面观察及当前SHA关联 |
| 页面文字相反或无法证明 | 不把模型绿值或截短quote当成通过 | 否定、矛盾后缀、子串截取、未知语义、工具缺失证明的浏览器回归 | 实际GitHub AX页面适配、规则与绕过状态核验 |

真实异常演练仅在`codex/fault-*`隔离分支/PR进行，不向main放入已知错误、不关闭检查、不放宽断言。每项记录触发前head/base、预期、实际PR/CI/页面回执及恢复方式；未执行的行继续保持待验证。

## Chrome连接的具体缺口

独立CLI能发现官方`cua_repl` MCP入口，受限stdio代理的协议、UTF-8和客户端请求转发也已本地验证；实际官方工具的Node内核未能完成初始化。因此现在缺少的是“独立调用读取真实Chrome正文并回传控制器”的成功证据，并非缺少Claude。

已经尝试重置本任务工具会话并重试；未验证成功。下一步最小恢复尝试是由会话拥有者保存工作并释放相关占用，再通过同一个`probe_chrome.py`重试；需要重启应用或其他会话时由用户处理或另行授权。本轮没有批量结束其他进程、改全局设置或关闭沙箱。释放占用是否足以解决仍待实测，不能写成已完成修复。

## 必须继续取得的证据

1. 用户已审查并授权`EXECUTION-PLAN.md`中的具体远端动作和自动合并条件，授权记录保存在本机运行证据`authorization-20261022.json`；本项已完成。
2. 独立Codex → 本地Chrome → 页面正文 → 返回控制器的真实预检通过。先由会话拥有者核对占用的工具会话，再重试；不批量结束其他进程、不关闭沙箱。
3. 初始化提交及真实Actions记录、试点PR及当前SHA对应的检查/审查/规则证据、真实merge SHA及主分支检查。
4. 真实GitHub失败CI/旧SHA负例，不能用本地fake结果代替。

已完成的本地自检不代表用户验收，也不证明未运行的真实GitHub步骤。
