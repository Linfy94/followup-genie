# WorkBuddy 安装测试

> 当前是预发布验证，不是正式兼容承诺。

## 安装

1. 打开 WorkBuddy 的「技能」。
2. 选择「添加技能」→「上传技能」。
3. 优先上传 `followup-genie-workbuddy-0.2.0-rc1.skill`。
4. 如果客户端不接受 `.skill`，再上传同内容的 `.zip`。
5. 安装后先保持自动化关闭，在普通任务中完成配置和演练。

WorkBuddy 官方建议只启用当前任务需要的 Skill；测试时也只开启项目跟进精灵。

## 给 WorkBuddy 的首次设置指令

把下面整段发给 WorkBuddy：

> 请调用“项目跟进精灵”。先阅读本 Skill 的 `docs/业务安装与验收.md`、
> `docs/业务接入新台账.md` 和 `KNOWN_ISSUES.md`。在我当前选择的独立工作空间中新建
> `runtime` 目录作为运行数据目录，不要使用或覆盖任何已有 Hermes 目录。先从模板生成配置，
> 暂时关闭企业微信真实发送和提醒事项写入。每一步告诉我需要补充什么，不要替我猜台账字段、
> 规则、凭证或接收群。任何情况下禁止修改腾讯文档台账。

首次执行脚本、访问网络或写本地状态时，默认权限可能要求确认。只允许访问本次测试工作空间
和腾讯文档开放平台、企业微信机器人所需网络地址。

## 演练指令

> 请使用当前工作空间的 `runtime` 作为运行目录，调用项目跟进精灵完成配置检查和只读试跑。
> 禁止发送企业微信，禁止写提醒事项，禁止修改腾讯文档。输出：读取到的台账、有效项目数、
> 每种去向数量、应催项目数、警告、台账只读核对结果。失败时原样报告，不要自动改业务规则。

## 正式测试指令

只有演练通过后再发：

> 请使用当前工作空间的 `runtime` 作为运行目录，先做配置检查；通过后执行一次正式催办，
> 只发送到测试群。输出执行时间、读取项目数、应催项目数、消息分片数、每片发送结果和
> 状态提交结果。任何发送失败都不得把本批项目记为已通知。

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
