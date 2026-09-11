#!/usr/bin/env bash
# 把新前端构建产物发到 VPS。
#
# 刻意在本地构建、只把 out/ 传上去，不在服务器上跑 npm build：那台机器只有
# 1.9G 内存，2026-09-11 刚因为内存打满触发过一次 OOM（Streamlit + OpenClaw +
# Futu OpenD + 若干 cron 已经占掉大半），而 next build 的瞬时内存占用轻松到
# 几百 MB。构建这件事没有任何理由放在生产机上做。
#
# 用法：在 web/ 目录下执行 ./deploy.sh
set -euo pipefail

HOST="${1:-dmit}"
REMOTE_DIR="/root/finance-agent/web"

echo "== 本地构建 =="
npm run build

echo "== 上传静态产物 =="
ssh "$HOST" "mkdir -p $REMOTE_DIR/out"
rsync -az --delete out/ "$HOST:$REMOTE_DIR/out/"

echo "== 完成。nginx 直接发 $REMOTE_DIR/out，无需重启任何服务 =="
