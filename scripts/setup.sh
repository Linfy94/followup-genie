#!/usr/bin/env bash
# 项目跟进精灵 · 通用引导（与宿主无关，幂等，可反复执行）
#
# 用法：
#   FOLLOWUP_HOME=/path/to/runtime bash scripts/setup.sh
#
# 做四件事，**不假设任何宿主**：
#   1. 建运行时目录
#   2. 复制配置模板（已存在则跳过 —— 绝不覆盖你的实际配置）
#   3. 建一个空的 .env（权限 600）
#   4. 跑一次不联网的配置自检
#
# 不做的事：不注册定时任务、不写 cron shim、不碰凭证、不改任何开关。
# 那些都跟宿主有关。
#
# 🔴 **通常不用直接跑这个。** 对外的唯一入口是：
#      python3 scripts/bootstrap.py --host workbuddy|hermes
#    它会把代码放到该放的位置，再按宿主调 setup.sh（通用）或 install.sh（Hermes）。
#    直接跑本脚本只适用于一种情况：代码已经就位，只想重建配置目录。

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 运行时目录：FOLLOWUP_HOME 优先，HERMES_HOME 兼容。
# 前两级与 scripts/core.py 的 hermes_home() 一致 —— 引导建在哪，程序就得从哪读，
# 不一致会表现成「装完了却说配置不存在」。
#
# ⚠️ 第三级**刻意不同**：core 在 ~/.hermes 不存在时也照样返回该路径
#    （运行时要靠它去创建），而这里要求目录已存在才认 ——
#    引导阶段猜错位置会把配置建到一个没人会去读的地方。
if [ -n "${FOLLOWUP_HOME:-}" ]; then
  HOME_DIR="$FOLLOWUP_HOME"
  HOW="FOLLOWUP_HOME"
elif [ -n "${HERMES_HOME:-}" ]; then
  HOME_DIR="$HERMES_HOME"
  HOW="HERMES_HOME（兼容别名）"
elif [ -d "$HOME/.hermes" ]; then
  HOME_DIR="$HOME/.hermes"
  HOW="检测到已安装的 Hermes"
else
  echo "❌ 不知道该把运行时目录建在哪。" >&2
  echo "   请指定：FOLLOWUP_HOME=<目录> bash scripts/setup.sh" >&2
  exit 2
fi

CONF="${HOME_DIR}/followup/config"
STATE="${HOME_DIR}/followup/state"
ENVF="${HOME_DIR}/.env"

echo "🧚 项目跟进精灵 · 通用引导"
echo "   代码目录：  ${SKILL_DIR}"
echo "   运行时目录：${HOME_DIR}（来自 ${HOW}）"
echo

# ── 1. 目录 ──
mkdir -p "${CONF}" "${STATE}"
echo "✅ 目录就绪：followup/config、followup/state"

# ── 2. 配置模板：存在则跳过 ──
# 🔴 绝不覆盖。升级时这一步会再跑一遍，覆盖等于把业务的规则和口径抹掉。
copied=0; skipped=0
for tpl in "${SKILL_DIR}"/templates/*.example.json; do
  [ -e "${tpl}" ] || continue
  base="$(basename "${tpl}" .example.json).json"
  dest="${CONF}/${base}"
  if [ -e "${dest}" ]; then
    echo "   ⏭  ${base} 已存在，跳过（不覆盖你的配置）"
    skipped=$((skipped + 1))
  else
    cp "${tpl}" "${dest}"
    echo "   📄 ${base} 已从模板创建 —— 需要按你的台账修改"
    copied=$((copied + 1))
  fi
done
echo "✅ 配置模板：新建 ${copied}、跳过 ${skipped}"

# ── 3. .env ──
# 只建空文件，绝不写入任何值。凭证要本人去拿，程序不该代劳、也不该猜。
if [ -L "${ENVF}" ] || { [ -e "${ENVF}" ] && [ ! -f "${ENVF}" ]; }; then
  echo "❌ ${ENVF} 必须是运行时目录内的普通文件，不能是目录或符号链接。" >&2
  exit 2
elif [ -e "${ENVF}" ]; then
  chmod 600 "${ENVF}"
  echo "✅ .env 已存在，内容未改动，权限已确认是 600"
else
  umask 077
  cat > "${ENVF}" <<'ENVEOF'
# 项目跟进精灵 · 凭证
# 🔴 这个文件绝不能进 Git、绝不能贴进聊天窗口或工单。
#    .gitignore 已排除它，但那只防住 git，防不住复制粘贴。

# 腾讯文档开放平台 token（必填）
# 获取：docs.qq.com →「更多操作 → 开放平台」→ 扫码
TENCENT_DOCS_TOKEN=

# 企业微信群机器人地址（要推送到企微群才需要）
# 获取：进群聊 → 群设置 → 群机器人 → 添加
# ⚠️ 不要在「工作台 → 智能机器人」里建，那是要写代码接入的另一种东西
FOLLOWUP_WECOM_WEBHOOK=

# 故障告警发到哪（可选，留空则降级为只写 health.json + stderr）
# 形如 telegram:<chat_id>；需要宿主具备发送能力
FOLLOWUP_ALERT_TARGET=
ENVEOF
  chmod 600 "${ENVF}"
  echo "✅ 已创建 ${ENVF}（权限 600，内容为空待填）"
fi

# ── 4. 把存活监控装到 skill 之外 ──
#
# 🔴 监控器必须活在 skill 目录外面。它留在包里的话，skill 被移动、删除、
#    或者升级装坏时，launchd 指向的文件就没了 —— 监控器跟着被监控对象
#    一起消失，而那正是它最该报警的场景。
#
# 每次安装/升级都刷一遍，副本因此自动跟着版本走。
# **不执行 launchctl load** —— 装不装由人决定，脚本只把东西放好。
echo
echo "── 存活监控（装到运行目录，与 skill 解耦）──"
set +e
FOLLOWUP_HOME="${HOME_DIR}" python3 "${SKILL_DIR}/scripts/watchdog.py" \
  --install --version "$(cat "${SKILL_DIR}/VERSION" 2>/dev/null || echo unknown)"
set -e

# ── 5. 不联网自检 ──
echo
echo "── 配置自检（不联网）──"
set +e
FOLLOWUP_HOME="${HOME_DIR}" python3 "${SKILL_DIR}/scripts/doctor.py" --validate-config
rc=$?
set -e

cat <<EOF

── 接下来 ──
1) 按你的台账改配置：${CONF}/ledgers.json 与 rules.json
   （怎么改见 docs/接一条新业务线.md）
2) 填凭证：${ENVF}
3) 再自检一次（这次会联网只读台账）：
     FOLLOWUP_HOME="${HOME_DIR}" python3 "${SKILL_DIR}/scripts/doctor.py"
4) 试跑（不发不写）：
     FOLLOWUP_HOME="${HOME_DIR}" python3 "${SKILL_DIR}/scripts/check_followup.py" --dry-run
5) 定时运行交给你的宿主：Hermes 见 scripts/install.sh，
   WorkBuddy 见 docs/WorkBuddy安装测试.md，其他见 README 的「按宿主安装」。
EOF

exit $rc
