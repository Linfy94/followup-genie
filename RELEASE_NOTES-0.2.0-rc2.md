# 项目跟进精灵 0.2.0-rc2

这是业务内测版，重点修复首次安装“看似成功、实际未完成”的问题。

## 本次修复

- 修复 UTF-8 环境下通用安装脚本直接退出。
- Hermes 安装失败时返回真实失败，不再继续显示成功。
- 新增 WorkBuddy / Hermes 统一引导程序。
- 已有业务配置、状态和凭证不会被覆盖；`.env` 权限统一为 `600`。
- README 顶部提供业务唯一指令。
- Skill 元数据改为标准格式。
- 新增可校验的 Agent 直装包和 WorkBuddy 手动上传包。

## GitHub Release 需要上传

- `followup-genie-agent.zip`
- `followup-genie-agent-0.2.0-rc2.zip`
- `followup-genie-workbuddy.skill`
- `followup-genie-workbuddy-0.2.0-rc2.skill`
- `SHA256SUMS.txt`

发布前先将仓库设为公开。发布后用未登录状态验证：

- `https://github.com/Linfy94/followup-genie/releases/latest/download/followup-genie-agent.zip`
- `https://github.com/Linfy94/followup-genie/releases/latest/download/SHA256SUMS.txt`

## 当前边界

- WorkBuddy 的 GitHub 直装以“当前工作空间可以立即运行”为成功标准，不承诺显示在技能列表。
- 要显示在 WorkBuddy 技能列表，手动上传同一 Release 中的 `.skill` 文件。
- WorkBuddy 业务电脑、Windows、休眠补跑和无人值守权限仍需实机验证。
