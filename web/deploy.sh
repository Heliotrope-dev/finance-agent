#!/usr/bin/env bash
# 把新前端构建产物发到 VPS。
#
# 刻意在本地构建、只把 out/ 传上去，不在服务器上跑 npm build：那台机器只有
# 1.9G 内存，2026-09-11 刚因为内存打满触发过一次 OOM（Streamlit + OpenClaw +
# Futu OpenD + 若干 cron 已经占掉大半），而 next build 的瞬时内存占用轻松到
# 几百 MB。构建这件事没有理由放在生产机上做。
#
# 发布目录是 /var/www/invest-next/next 而不是项目目录下：/root 是 0700，
# nginx 的 www-data 进不去（实测报 13: Permission denied），而放宽 /root
# 权限等于把数据库、secrets.toml、SSH 私钥一起暴露给 web 进程。
#
# 传输用 tar over ssh 而不是 rsync：目标机器没装 rsync。
set -euo pipefail

HOST="${1:-dmit}"
REMOTE="/var/www/invest-next/next"

echo "== 本地构建 =="
npm run build

echo "== 上传静态产物到 $HOST:$REMOTE =="
tar -czf - -C out . | ssh "$HOST" "rm -rf '$REMOTE' && mkdir -p '$REMOTE' && tar -xzf - -C '$REMOTE' && chown -R www-data:www-data /var/www/invest-next"

echo "== 完成。nginx 直接发静态文件，不需要重启任何服务 =="
echo "   访问：https://invest.heliotrope.online:2053/next/"
