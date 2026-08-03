#!/usr/bin/env bash
# 项目跟进精灵 · 跑全部自动化测试
#
# 测试绝不碰真实世界：HERMES_HOME 指向临时目录、腾讯文档/企微/telegram/
# 提醒事项全部打桩。跑多少遍都不会打扰业务群里的真人。
set -euo pipefail
cd "$(dirname "$0")"

# -W error::ResourceWarning —— 未关闭的文件句柄**直接判失败**。
#
# 这类警告平时只是刷屏，容易被无视，但它指向的是真问题：
# 忘了关的句柄在长跑进程里会累积，最后撞上 ulimit。
# 与其靠人盯输出，不如让它跑红。
exec python3 -W error::ResourceWarning -m unittest discover -s tests "$@"
