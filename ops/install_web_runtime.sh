#!/usr/bin/env bash
# Install version-controlled service/routing files. Run as root during a
# reviewed deployment; this script deliberately does not restart nginx.
set -euo pipefail

repo="${1:-/root/finance-agent}"
install -m 0644 "$repo/ops/finance-agent-api.service" /etc/systemd/system/finance-agent-api.service
install -m 0644 "$repo/ops/invest-next.nginx.conf" /etc/nginx/snippets/invest-next.conf
systemctl daemon-reload
nginx -t
