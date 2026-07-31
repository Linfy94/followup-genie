# WorkBuddy 安装测试

> 当前是预发布验证，不是正式兼容承诺。

## 安装

1. 打开 WorkBuddy 的「技能」。
2. 选择「添加技能」→「上传技能」。
3. 优先上传 `followup-genie-workbuddy.skill`。
4. 如果客户端不接受 `.skill`，再上传同内容的 `.zip`。
5. 安装后先保持自动化关闭，在普通任务中完成配置和演练。

WorkBuddy 官方建议只启用当前任务需要的 Skill；测试时也只开启项目跟进精灵。

## 首次设置说明

只复制根目录 `README.md` 顶部“给业务：只复制下面这一段”的完整内容。
本文件不再维护第二套提示词。

首次执行脚本、访问网络或写本地状态时，默认权限可能要求确认。只允许访问本次测试工作空间
和腾讯文档开放平台、企业微信机器人所需网络地址。

## 演练说明

首次设置完成后继续在同一任务中操作，不需要业务再复制第二条指令。Agent 必须按
`SKILL.md` 的演练流程执行，并等待业务明确回复“确认通过”。

## 正式测试说明

只有业务明确回复“确认通过”后，Agent 才能继续测试群发送；不需要业务复制新的提示词。

## 创建自动化

WorkBuddy 的自动化会按计划发起 Agent 任务，会消耗积分。建议先设为工作日每天一次，
并选择独立工作空间和本 Skill。

自动化提示词：

> 调用项目跟进精灵，使用当前工作空间的 `runtime` 作为运行目录。先检查配置，再执行当天
> 正式催办。腾讯文档只读；只向配置中的测试群发送；失败时不得提交通知状态。最终明确报告：
> 是否启动、读取项目数、应催项目数、发送是否完整、状态是否提交、健康记录是否更新。

创建后先手动触发一次，确认权限弹窗已经处理；再观察连续 3 个工作日的执行历史。

## 本轮重点观察

- 客户端是否接受 `.skill`，还是只接受 `.zip`。
- Skill 安装后能否读取包内脚本和文档。
- 自动化无人值守时，脚本或网络权限是否再次要求人工确认。
- 电脑休眠或 WorkBuddy 退出时，是否补跑错过的任务。
- “0 个要催办”是否仍会给业务明确结果。
- 升级 Skill 后，独立工作空间中的 `runtime/config` 和 `runtime/state` 是否保留。

这些行为官方文档没有给出完整承诺，必须以业务电脑实测为准。

## WorkBuddy 官方说明

- 技能安装：https://www.codebuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Skills-Market
- 自动化：https://www.codebuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Automation-Guide
- 默认权限与安全沙箱：https://www.codebuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Permission-Modes
