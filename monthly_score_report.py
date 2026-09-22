"""Build the monthly ICIR weight report and optionally deliver it to Weixin.

The report is advisory only: it never changes production score weights.
"""
from __future__ import annotations

import argparse
import json

import score_diagnostics
from wechat_delivery import send_text


def render_message(report: dict) -> str:
    suggestion = report["monthly_weight_suggestion"]
    lines = ["评分体系月度诊断", suggestion["verdict"]]
    for item in suggestion["dimensions"]:
        icir = "未验证" if item["icir"] is None else f'{item["icir"]:.2f}'
        lines.append(
            f'{item["dimension"]}: {item["horizon_days"]}日 ICIR {icir} '
            f'(截面数 {item["cross_section_count"]})'
        )
    weights = suggestion["suggested_weights_pct"]
    if weights:
        lines.append("建议权重: " + "、".join(f"{key} {value:.2f}%" for key, value in weights.items()))
    lines.append("以上仅为离线建议，未自动修改线上权重。")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="screen")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--bootstrap-samples", type=int, default=2000)
    args = parser.parse_args()
    report = score_diagnostics.build_report(
        args.source, bootstrap_samples=max(100, args.bootstrap_samples)
    )
    message = render_message(report)
    print(json.dumps({"message": message}, ensure_ascii=False))
    if args.send and not send_text(message):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
