# 项目跟进精灵 🧚

> `0.3.0-rc3` · 业务内测版。Hermes + macOS 已实测；WorkBuddy 当前工作空间安装已完成隔离测试，仍待业务电脑实测。

## 给业务：只复制下面这一段

> 请帮我安装并启用“项目跟进精灵”。请从 `https://github.com/Linfy94/followup-genie/releases/latest/download/followup-genie-agent.zip` 下载安装包，同时下载同目录的 `SHA256SUMS.txt` 并核对文件完整性；校验失败立即停止。解压后完整阅读包内的 `SKILL.md` 和 `README.md`，判断你当前运行在 WorkBuddy 还是 Hermes，然后使用包内 `scripts/bootstrap.py` 完成对应安装。所有技术操作由你完成，不要让我运行命令、编辑文件或理解 Git；使用独立运行目录，腾讯文档台账永远只读，不覆盖已有配置、状态或凭证。安装后只执行离线配置检查，明确告诉我安装路径、版本和检查结果，然后一次只问我一个业务问题，引导我接入测试台账；在我明确回复“确认通过”前，不得读取正式台账、真实发送消息、写提醒事项或创建自动化。遇到下载、权限、校验或安装错误时立即停止，不猜测、不绕过，并用业务语言告诉我需要确认什么。

这段指令会把程序安装到当前工作空间并立即可用，不承诺自动出现在 WorkBuddy 的“已安装技能”列表。若要显示在技能列表，使用同一 Release 中的 `.skill` 文件手动上传。

读业务维护的项目台账，找出**在某个流程节点上卡太久**的项目，生成待催清单推给业务本人。

纯规则判定，零 LLM、零 token、零第三方 Python 依赖。台账**只读**。

```
🧚 项目跟进精灵 · 2026-08-07

——AI节能盒子——
总任务量：101 个项目里，28 个要催办
积压最重：节能测试 15 项（占一半）

【待收资】2 项
1、某某酒店管理有限公司 — 超期 23 天
2、某某科技有限公司 — 超期 27 天

【预调试/安装】10 项
1、某某生物技术股份有限公司 — 超期 3 天
...
```

「超期天数」＝ 在这个节点待的天数 − 该节点允许的天数。
所以跨阶段的数字可以直接横向比较——「超期 3 天」和「超期 27 天」是同一把尺子。

---

## 铁律：台账只读

**任何情况下都不写入、不修改、不新建、不删除台账。** 唯一的持久化写入是运行时目录下的本地状态文件。

台账是业务的生产数据、多人协作维护。程序一旦误写，业务无法分辨是人改的还是程序改的，
信任一次就没了。**这条优先于任何功能需求**——若某功能只能靠回写台账实现，砍功能，不破例。

只读白名单写死在 [`scripts/qqdoc.py`](scripts/qqdoc.py) 的 `ALLOWED_TOOLS` 常量里，不从配置读取。
⚠️ 特别注意 `sheet.operation_sheet`——它是表格内的 JS 沙箱，能改值改样式增删行列，**永远不要加进白名单**。

---

## 环境要求

| 项 | 要求 |
|---|---|
| Python | **3.9+**（macOS 自带的 3.9 就够，无需另装） |
| 第三方依赖 | **无**。只用标准库 |
| 操作系统 | macOS / Linux。**Windows 尚未验证** |
| 外网 | 只需访问 `docs.qq.com`（国内直连）。推企微则加 `qyapi.weixin.qq.com`，也是国内域名 |
| 模型 API key | **不需要**。主流程是纯规则 |

---

## 安装契约

任何宿主（WorkBuddy / Hermes / launchd / crontab / 手工）都照这一节即可。

### 运行时目录

程序把配置、状态、凭证放在**一个目录**下，结构固定：

```
<运行时目录>/
├── .env                        凭证。绝不进 Git
└── followup/
    ├── config/                 你的配置。升级时永不覆盖
    │   ├── ledgers.json        台账地址、字段映射、责任范围、终止判据
    │   ├── rules.json          节点、阈值、复提醒间隔、工作日口径
    │   ├── output.json         通知通道、提醒事项开关、分割线
    │   └── holidays.json       法定节假日表（工作日计算用）
    └── state/                  程序自己写的状态。升级时永不覆盖
```

**升级契约**：包内的代码与 `templates/` 可以被覆盖，`followup/config/` 与 `followup/state/` **永不覆盖**。
`state/stage_entered.json` 尤其不能丢——它是催办时钟的唯一来源，丢了几个月的积累就归零。

### 告诉程序运行时目录在哪

```bash
FOLLOWUP_HOME=<运行时目录>
```

优先级：`FOLLOWUP_HOME` > `HERMES_HOME`（兼容别名） > `~/.hermes`。

> 为什么有两个变量：这个包最早只跑在 Hermes 上，所以历史变量叫 `HERMES_HOME`。
> 在没装 Hermes 的宿主里让人设它是误导，于是加了中性的 `FOLLOWUP_HOME`。
> 两个都设时前者优先；只设 `HERMES_HOME` 的老安装行为完全不变。

### 一条命令完成安装

```bash
python3 scripts/bootstrap.py --host workbuddy --workspace <当前工作空间>
python3 scripts/bootstrap.py --host hermes        # 或者装到 Hermes
```

**`bootstrap.py` 是唯一入口。** 幂等，可反复执行。它做两件事：

1. **分发**：把代码复制到该去的位置（WorkBuddy 工作空间下的 `.followup-genie/`，
   或 Hermes 的 `skills/work/followup-genie/`）
2. **引导**：按宿主自动调 `setup.sh`（通用）或 `install.sh`（Hermes 额外写 cron 启动器）

引导那一步会：建目录 → 复制配置模板（**已存在则跳过**）→ 建 600 权限的空 `.env`
→ 跑一次不联网自检。

它**不会**：注册定时任务、填凭证、改任何开关。那些跟宿主有关，或者必须由人来做。

> 下游那两个脚本也能单独跑，但只适用于「代码已经就位、只想重建配置」：
> `FOLLOWUP_HOME=<运行时目录> bash scripts/setup.sh`。
> **正常安装不要直接调它们** —— 那会绕过代码分发那一步。

### 填两样东西

1. **配置**：按你的台账改 `followup/config/ledgers.json` 与 `rules.json`
   （详见 [docs/接一条新业务线.md](docs/接一条新业务线.md)）
2. **凭证**：写进 `<运行时目录>/.env`

```bash
TENCENT_DOCS_TOKEN=          # 必填。docs.qq.com →「更多操作 → 开放平台」→ 扫码
FOLLOWUP_WECOM_WEBHOOK=      # 要推企微群才需要。进群聊 → 群设置 → 群机器人 → 添加
FOLLOWUP_ALERT_TARGET=       # 可选。故障告警发到哪，形如 telegram:<chat_id>
```

⚠️ 企微机器人要在**群聊里**建，不是「工作台 → 智能机器人」——后者是要写代码接入的另一种东西。
拿到的地址形如 `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=…`，**它等同于密码**。

---

## 按宿主安装

### Hermes

```bash
python3 scripts/bootstrap.py --host hermes
hermes cron create "0 9 * * 1-5" --name "项目跟进精灵" \
  --script followup_genie.py --no-agent
```

统一引导会把代码安装到 Hermes Skill 目录，再调用 `install.sh` 建配置与 cron 启动器。
`--no-agent` 表示纯脚本、不走 LLM。Hermes 按**退出码**判定任务成败（见下表），失败会投递告警。

### WorkBuddy

业务只需把 README 顶部的唯一指令发给当前 Agent。Agent 下载并校验发布包后执行：

```bash
python3 scripts/bootstrap.py --host workbuddy --workspace "<当前工作空间>"
```

⚠️ WorkBuddy 的定时运行会**发起 Agent 任务**，与 Hermes 的 `--no-agent` 不同，会消耗积分，
也受模型、权限确认和客户端在线状态影响。

### launchd / crontab / 其他

程序本身不管调度——跑一次就退出，把成败写进退出码。宿主负责定时触发和读退出码。

```bash
FOLLOWUP_HOME=<运行时目录> /usr/bin/python3 <包>/scripts/check_followup.py
```

crontab 示例（工作日 9:00）：

```cron
0 9 * * 1-5 FOLLOWUP_HOME=/path/to/runtime /usr/bin/python3 /path/to/skill/scripts/check_followup.py
```

⚠️ **cron 的环境与登录 shell 不同**：`PATH` 更短、没有你 shell profile 里的任何东西。
装完务必**在宿主的实际运行路径下**验一次，别只在终端里试通就算数。

---

## 命令与退出码

```bash
FOLLOWUP_HOME="<运行时目录>" python3 scripts/check_followup.py                    # 跑一次（会发消息、会写状态）
FOLLOWUP_HOME="<运行时目录>" python3 scripts/check_followup.py --dry-run          # 试跑：不发不写
FOLLOWUP_HOME="<运行时目录>" python3 scripts/check_followup.py --json             # 结构化输出，给程序用。不发不写
FOLLOWUP_HOME="<运行时目录>" python3 scripts/check_followup.py --today 2026-08-07 # 模拟某一天。不发不写
FOLLOWUP_HOME="<运行时目录>" python3 scripts/check_followup.py --verbose          # 附运行摘要与调试信息
FOLLOWUP_HOME="<运行时目录>" python3 scripts/check_followup.py --verify-readonly  # 顺带核对台账未被修改

FOLLOWUP_HOME="<运行时目录>" python3 scripts/doctor.py --validate-config          # 只查配置，不联网
FOLLOWUP_HOME="<运行时目录>" python3 scripts/doctor.py                            # 全项自检（联网只读台账）

FOLLOWUP_HOME="<运行时目录>" python3 scripts/watchdog.py --install                # 装到运行时目录并生成 plist
FOLLOWUP_HOME="<运行时目录>" python3 scripts/watchdog.py --dry-run                # 存活监控：只打印判定
FOLLOWUP_HOME="<运行时目录>" python3 scripts/watchdog.py --self-test              # 验告警链路（**会真发消息**）

./run_tests.sh                                       # 全部自动化测试，零网络
```

> `watchdog.py` 由 launchd 定时执行，用来发现「任务根本没跑」——
> 那是唯一一类连报错都不会产生的失败。
>
> `setup.sh` 已经自动把它装到 **`<运行时目录>/watchdog/`**（Skill 之外，
> 这样 Skill 被移动、删除或升级装坏都影响不到它），并生成了一份路径填好的
> plist。**但 launchd 还需要你自己装载**，两条命令见
> [docs/外部监控.md](docs/外部监控.md)——装载并跑过 `--self-test` 之前，
> 「能发现任务根本没跑」只是能力，不是闭环。

**`--dry-run`、`--json`、`--today`（不带 `--force-push`）一律严格只读**：
不发企微、不发告警、不创建或修改任何状态文件，连损坏的状态文件都不会被改名。

### 退出码（宿主靠它判定成败）

| 码 | 含义 | 宿主该怎么处理 |
|---|---|---|
| `0` | 正常。**含「今天没有要催的」和「另一次运行进行中，本次跳过」** | 成功 |
| `1` | 主任务失败：取数失败、入口断言不过、主通道投递失败、只读性验证不过 | **报失败**，日志带 stderr |
| `2` | 启动阶段故障：配置错、无启用台账、节假日表损坏、状态目录不可写、参数错 | **报失败** |

🔴 **绝不能把推送失败当成功。** 那会让项目被记为「已通知」进入静默期，
业务没收到、也不会再收到，而一切看起来都像「今天没事」。

---

## 配置四件套

| 文件 | 管什么 | 改动频率 |
|---|---|---|
| `ledgers.json` | 台账在哪、哪列是什么、只管哪些项目、什么算终止 | 接新台账时 |
| `rules.json` | 有哪些节点、各卡多久算超期、多久重复提醒一次 | 业务改口径时 |
| `output.json` | 推给谁、写不写提醒事项、分割线规则 | 装机时定一次 |
| `holidays.json` | 法定节假日与补班日 | **每年更新一次** |

⚠️ `holidays.json` 是全项目唯一需要人工年度维护的东西（国务院调休无法用规则推算）。
过期时 `doctor` 会报警，**不会静默降级**。

🔴 **装机时它是空的。** 配置不随包分发（升级不覆盖是同一个设计的两面），
所以 `setup.sh` 复制过去的是空模板：`holidays: []`、`workdays: []`、
`covers_year: 0`、`verified: false`，且 `rules.json` 的
`workday.exclude_holidays` 默认为 `false`。

**只有当某个节点的复提醒写成 `{"workdays": N}` 时才需要管它。** 那时要做两件事：

1. 照国务院当年的《部分节假日安排的通知》填 `holidays.json`（放假日与补班日都要，
   补班日最容易漏），填完把 `verified` 改成 `true`、`covers_year` 填当年
2. 把 `rules.json` 的 `workday.exclude_holidays` 改成 `true`

两件事漏任何一件，`doctor` 都会报「工作日口径与规则对不上」并说清是哪个节点——
**不会静默按只排周末算**。如果全部节点都是自然日（`{"days": N}`），
保持现状即可，`doctor` 会显示「无需节假日表」。

---

## 验收顺序

装完按这个顺序走，每一步都过了再进下一步：

```bash
# 1. 装对了没（不联网）
FOLLOWUP_HOME=<目录> python3 scripts/doctor.py --validate-config

# 2. 配好了没（联网，只读台账）
FOLLOWUP_HOME=<目录> python3 scripts/doctor.py

# 3. 结果对不对（不发不写）—— 逐条人工核对至少 5 个项目
FOLLOWUP_HOME=<目录> python3 scripts/check_followup.py --dry-run --verify-readonly

# 4. 真跑一次，确认消息真的送到了
FOLLOWUP_HOME=<目录> python3 scripts/check_followup.py

# 5. 在宿主的实际运行路径下再触发一次
```

第 3 步**必须人工核对**：这条单确实卡在这个节点、确实超了这么多天。对不上就是 bug，不要放行。

---

## 出问题时

| 现象 | 先看哪里 |
|---|---|
| **完全没反应** | 是不是当天真的没有要催的？跑 `--verbose` 看运行摘要。再确认宿主真的触发了 |
| **显示成功但群里没消息** | 先看应催数是不是 0（无事不发）；再看 `output.json` 的 `notify.primary` 与 `wecom_webhook.enabled` 是否都指向企微 |
| **某个项目没被催** | `--json` 查它落在哪一类：未超期 / 终止 / 暂缓 / 范围外 / 还在复提醒间隔内 |
| **天数看着不对** | ③④ 的时钟起点是**近似值**（台账没有「安装成功日期」列）。见 [KNOWN_ISSUES.md](KNOWN_ISSUES.md) |
| **报「凭证失效」「表头缺列」** | 这是故障不是「今天没事」，退出码非零。照提示修，别忽略 |
| **状态文件损坏** | 坏文件会被改名保留（`*.corrupt.*`），**不要删状态目录「重试」** |

---

## 已知边界

见 [KNOWN_ISSUES.md](KNOWN_ISSUES.md)。当前 `0.3.0-rc3` 的主要限制：
WorkBuddy 未实机验证、Windows 未验证、**外部存活监控已交付但默认未安装**。

---

## 开发

```bash
./run_tests.sh        # 纯标准库 unittest，零网络、零真实状态
```

测试全程在临时目录里跑，腾讯文档 / 企微 / telegram / 提醒事项全部打桩——
跑多少遍都不会打扰任何真人。用例数以实际输出为准。

其他文档：

- [CHANGELOG.md](CHANGELOG.md) — 每次改了什么，尤其是**业务能感知的行为变化**
- [SECURITY.md](SECURITY.md) — 数据边界与凭证处理
- [docs/接一条新业务线.md](docs/接一条新业务线.md) — 从台账链接到跑通首次催办
- [docs/业务安装与验收.md](docs/业务安装与验收.md) — 业务侧的验收标准
- [docs/业务操作手册-零基础版.md](docs/业务操作手册-零基础版.md) — 给零基础业务的完整手册

---

## 许可证

**内部项目，暂未确定开源许可。** 未经授权请勿分发。
