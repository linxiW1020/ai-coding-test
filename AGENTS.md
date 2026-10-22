# ai-coding-test

这是 Codex 开发工作流的隔离试点，样例用于验证流程，不代表客户业务或生产能力。

- 默认使用简体中文沟通。代码与命令保留英文。
- `controller/` 是本地调度、预算、授权及验证器，任务 Agent 不得修改。
- 源码范围由每个任务的 `allowed_paths` 决定，不能修改 `.github/`、本文件、`.codex/` 或门禁来掩盖失败。
- 实现任务不自行执行 `git commit`、`git push`、创建或合并 PR，由控制器按人工授权处理。
- 基础检查：`python -m unittest discover -s tests -v`。
- 控制器检查：在 `controller/` 中运行 `python -m unittest discover -s tests -v`。
- 提交、推送、创建或合并 PR、修改仓库规则都需要 Berlin 明确授权，已有明确授权的动作不重复确认。

## Code Review Rules

- 检查必须对当前代码版本有效，未知、缺失、跳过和等待不能当成成功。
- 保留任务之外的用户改动。需求不清、环境失败或证据不足时返回具体原因。
