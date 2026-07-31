#!/usr/bin/env bash
# 项目跟进精灵 · Hermes 专用安装（幂等，可反复执行）
#
# 结构：通用部分调 setup.sh，这里只做 Hermes 独有的两件事。
#   通用（setup.sh）：建目录、复制配置模板、建 .env、跑不联网自检
#   Hermes 专属：写 cron shim、给出注册 cron 的下一步
#
# 两个宿主共用同一套引导逻辑，不各写一遍 —— 否则迟早只改了一边，
# 表现成「Hermes 上能装、WorkBuddy 上少个目录」。
#
# 不做的事：不注册 cron（那一步要人看过自检结果再决定）、不改写入开关、
#          不填 .env（凭证要业务本人扫码）。

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 运行时解析 Hermes 目录，不硬编码
if [ -n "${HERMES_HOME:-}" ]; then
  HH="${HERMES_HOME}"
elif [ -d "${HOME}/.hermes" ]; then
  HH="${HOME}/.hermes"
else
  echo "❌ 找不到 Hermes 配置目录。请先安装 Hermes，或设置 HERMES_HOME。" >&2
  echo "   不用 Hermes 的话，直接跑通用引导：" >&2
  echo "     FOLLOWUP_HOME=<目录> bash \"${SKILL_DIR}/scripts/setup.sh\"" >&2
  exit 1
fi

FU="${HH}/followup"

# ── 1~2. 通用引导：目录、配置模板、.env、不联网自检 ──
HERMES_HOME="${HH}" bash "${SKILL_DIR}/scripts/setup.sh"

echo
echo "── Hermes 专属部分 ──"

# ── 3. cron shim ──
# hermes cron 的 --script 参数只接受 <hermes>/scripts/ 下的路径，而真实代码在
# skill 包里（这样才能随 hermes skills update 升级）。shim 是个几行的瘦启动器。
# 不用软链接：skill 卸载或改名会留下断链，报错难懂。
mkdir -p "${HH}/scripts"
SHIM="${HH}/scripts/followup_genie.py"
cat > "${SHIM}" <<'PYEOF'
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
项目跟进精灵的 cron 启动器（由 install.sh 生成，不要手改）。

存在的唯一理由：hermes cron 的 --script 只认 <hermes>/scripts/ 下的路径，
而真实代码在 skill 包里（那样才能随 hermes skills update 一起升级）。
本文件只负责找到包、把参数转交过去。
"""
import os
import runpy
import sys
from pathlib import Path


def hermes_home() -> Path:
    # 三级优先级必须与 core.hermes_home() 完全一致，否则 shim 找的目录
    # 和程序读的目录会不是同一个 —— 那会表现成「cron 跑成功了但配置没生效」。
    env = os.environ.get("FOLLOWUP_HOME") or os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "hermes"
    return Path.home() / ".hermes"


target = hermes_home() / "skills" / "work" / "followup-genie" / "scripts"
if not (target / "check_followup.py").exists():
    print(
        f"❌ 找不到 followup-genie 的脚本目录：{target}\n"
        f"   skill 可能被卸载或改名了。重新安装：hermes skills install followup-genie",
        file=sys.stderr,
    )
    sys.exit(2)

sys.path.insert(0, str(target))
sys.argv[0] = str(target / "check_followup.py")
runpy.run_path(str(target / "check_followup.py"), run_name="__main__")
PYEOF
chmod +x "${SHIM}"
echo "✅ cron 启动器已写入：${SHIM}"

cat <<EOF

── 接下来（Hermes）──
1) 若配置是新建的：按你的台账改 ${FU}/config/ledgers.json 和 rules.json
2) 腾讯文档授权：docs.qq.com →「更多操作 → 开放平台」→「OpenClaw 专用入口」
   扫码后把 token 写入 ${HH}/.env 的 TENCENT_DOCS_TOKEN=
   （这一步必须业务本人操作）
3) 试跑： FOLLOWUP_HOME="${HH}" python3 "${SKILL_DIR}/scripts/check_followup.py" --dry-run
4) 注册定时任务（确认试跑结果无误后再做）：
   hermes cron create "0 9 * * *" --name "项目跟进精灵" \\
     --script followup_genie.py --no-agent
5) 要真的写提醒事项时：把 ${FU}/config/output.json 的 reminders.write 改成 true
   然后跑一次触发 macOS 授权弹窗，点允许。
   ⚠️ 之后必须用 cron 的实际运行路径复验（hermes cron run "项目跟进精灵"）——
      终端里通过不代表 cron 通过，而且失败是静默的。
EOF
