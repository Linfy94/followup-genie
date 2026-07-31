#!/usr/bin/env bash
# 项目跟进精灵 · 跑全部自动化测试
#
# 测试绝不碰真实世界：HERMES_HOME 指向临时目录、腾讯文档/企微/telegram/
# 提醒事项全部打桩。跑多少遍都不会打扰业务群里的真人。
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m unittest discover -s tests "$@"
