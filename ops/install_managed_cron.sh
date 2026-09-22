#!/usr/bin/env bash
set -euo pipefail

# 2026-09-22：ops/intraday_watch.cron 以前只是“手工安装时的备忘”，推代码
# 不会更新系统 crontab。结果新脚本能部署却永远没人调用。这里把由仓库
# 管理的任务先从现有 crontab 精确移除，再追加当前版本，重复部署也不会叠加。
repo_dir=${1:-/root/finance-agent}
schedule="$repo_dir/ops/intraday_watch.cron"
test -f "$schedule"

old_cron=$(mktemp /tmp/fa-cron-old.XXXXXX)
new_cron=$(mktemp /tmp/fa-cron-new.XXXXXX)
trap 'rm -f "$old_cron" "$new_cron"' EXIT

crontab -l >"$old_cron" 2>/dev/null || true
# Remove the complete previous managed block, then remove pre-block legacy task
# lines left by older installers. This keeps repeated deployments truly idempotent.
awk '
  /^# BEGIN finance-agent managed$/ { managed=1; next }
  /^# END finance-agent managed$/ { managed=0; next }
  !managed { print }
' "$old_cron" | grep -vE '(intraday_watch\.py|portfolio_snapshot\.py|backfill_outcomes\.py|score_diagnostics\.py|monthly_score_report\.py)' \
  >"$new_cron" || true
printf '\n# BEGIN finance-agent managed\n' >>"$new_cron"
cat "$schedule" >>"$new_cron"
printf '# END finance-agent managed\n' >>"$new_cron"
crontab "$new_cron"
