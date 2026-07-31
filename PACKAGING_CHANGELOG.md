# 打包改动留痕

版本：`0.2.0-rc2`
修复基线：`e32dcca`

## <mark>0.2.0-rc2 新增与修复</mark>

- <mark>修复 UTF-8 环境下通用安装脚本解析中文标点时崩溃的问题。</mark>
- <mark>Hermes 安装不再吞掉 setup 错误；失败时不会继续生成启动器。</mark>
- <mark>新增 WorkBuddy / Hermes 统一引导程序及隔离安装测试。</mark>
- <mark>README 顶部新增业务唯一指令，其他文档不再复制竞争提示词。</mark>
- <mark>新增零依赖发布构建及 SHA-256 校验。</mark>
- <mark>Skill frontmatter 只保留标准字段，通用运行目录统一为 FOLLOWUP_HOME。</mark>

本轮只在发布副本中做打包与业务引导，未修改正在运行的 Hermes 工程。

## <mark>新增</mark>

- <mark>新增 README、版本、已知问题、安全说明和 Git 忽略规则。</mark>
- <mark>新增业务安装验收、WorkBuddy 试装和新台账接入指引。</mark>
- <mark>新增面向零基础业务人员的完整操作手册和问题反馈模板。</mark>
- <mark>新增 `.skill`、普通 `.zip`、源码压缩包和 SHA-256 校验值。</mark>

## 文案泛化

- ~~“当前口径 28 条”~~
- <mark>改为“所有已超期项目”，避免把某一业务的数量带入通用包。</mark>
- ~~“定时任务已注册为 Hermes cron”~~
- <mark>改为分别说明 Hermes 和 WorkBuddy 的运行方式。</mark>

## 未进入对外工程的原仓库文件

以下文件只记录开发机现场、个人路径或真实业务分析，未复制到对外工程；原文件没有删除：

- `docs/回滚.md`
- `docs/运行与迁移差距.md`
- `docs/原目标与当前实现对照.md`

业务真实配置、状态、备份、日志和 `.env` 从未进入发布副本。
