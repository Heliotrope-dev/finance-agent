"use client";

import { useEffect, useState } from "react";
import { apiGet, fmtUsd, fmtUsdSigned, fmtPct, moveColor } from "../../lib/api";

// AI 模拟炒股。净值口径跟 Streamlit 那版和接口层完全一致：只算 AI 自己按
// 成交流水买入的仓位。账户里其他来源的持仓单独列、压灰、明确标注不计入——
// 这是 2026-09-11 那次账目 bug 的根因（现金没扣、市值却算进净值，倒算出
// +20.25% 的假收益），不能在新前端上重演。
export default function SimPage() {
  const [d, setD] = useState(null);
  const [err, setErr] = useState(false);

  useEffect(() => {
    let alive = true;
    const load = () =>
      apiGet("/api/sim")
        .then((x) => alive && setD(x))
        .catch(() => alive && setErr(true));
    load();
    const t = setInterval(load, 30000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  if (err) {
    return (
      <p className="mt-8 text-[0.85rem]" style={{ color: "var(--fa-muted)" }}>
        模拟盘状态读取失败。
      </p>
    );
  }
  if (!d || !Object.keys(d).length) {
    return <div className="mt-8 h-40" aria-hidden />;
  }

  const cards = [
    ["虚拟现金（剩余可用）", fmtUsd(d.cash)],
    ["持仓市值（仅AI自己买入的）", fmtUsd(d.ai_holdings_value)],
    [`总额（起始${fmtUsd(d.start_capital)}）`, fmtUsd(d.net_value)],
  ];

  return (
    <section className="mt-7">
      <h2 className="fa-section-title">AI 模拟炒股</h2>

      <div className="mt-4 grid grid-cols-1 gap-5 sm:grid-cols-3">
        {cards.map(([label, value]) => (
          <div key={label}>
            <div className="text-[0.76rem]" style={{ color: "var(--fa-muted)" }}>
              {label}
            </div>
            <div className="mt-0.5 text-[1.5rem] font-semibold tracking-tight">
              {value}
            </div>
          </div>
        ))}
      </div>

      <div className="mt-5">
        <div className="text-[0.76rem]" style={{ color: "var(--fa-muted)" }}>
          累计收益率（相对起始本金）
        </div>
        <div
          className="mt-0.5 text-[1.5rem] font-semibold tracking-tight"
          style={{ color: moveColor(d.return_pct) }}
        >
          {fmtPct(d.return_pct)}
        </div>
      </div>

      {d.foreign_positions?.length ? (
        <p className="mt-4 text-[0.74rem]" style={{ color: "var(--fa-faint)" }}>
          账户里还有 {d.foreign_positions.length} 笔非AI下单的持仓，不计入上面的净值和收益率——
          这些不是 AI 的操作记录。
        </p>
      ) : null}

      <h3 className="fa-section-title mt-9 text-[0.95rem]">当前持仓</h3>
      <div className="mt-2">
        {!d.ai_positions?.length && !d.foreign_positions?.length ? (
          <p className="text-[0.82rem]" style={{ color: "var(--fa-faint)" }}>
            当前空仓。
          </p>
        ) : null}

        {d.ai_positions?.map((p) => (
          <div
            key={p.code}
            className="flex items-baseline justify-between py-2"
            style={{ borderBottom: "1px solid var(--fa-border)" }}
          >
            <span className="text-[0.86rem]">
              {p.name}
              <span className="ml-1.5 text-[0.74rem]" style={{ color: "var(--fa-faint)" }}>
                {p.code} · {p.qty}股
              </span>
            </span>
            <span className="text-[0.86rem] font-medium" style={{ color: moveColor(p.pl) }}>
              {fmtUsdSigned(p.pl)}
            </span>
          </div>
        ))}

        {d.foreign_positions?.map((p) => (
          <div
            key={p.code}
            className="flex items-baseline justify-between py-2 opacity-55"
            style={{ borderBottom: "1px solid var(--fa-border)" }}
          >
            <span className="text-[0.86rem]">
              {p.name}
              <span className="ml-1.5 text-[0.74rem]" style={{ color: "var(--fa-faint)" }}>
                {p.code} · {p.qty}股 · 非AI持仓 · 不计入净值
              </span>
            </span>
          </div>
        ))}
      </div>
    </section>
  );
}
