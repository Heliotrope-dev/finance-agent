"use client";

import { useEffect, useState } from "react";
import { apiGet, fmtPct, moveColor } from "../../lib/api";

// AI 战绩墙。两份文档都把它列为最重要的一项，理由是投资产品最缺的是信任：
// 别人家的"AI 选股"只展示成功案例，把失败的也全部公开本身就是卖点。
//
// 两个数字的口径必须分开说清楚：
// - 顶部汇总用全样本（已回填的方向类判断全部计入），因为最近的判断绝大
//   多数是"持有/观望"，只看最近 20 条经常一条方向判断都没有；
// - 下面的明细是最近 20 条，用来逐条核对。
export default function RecordPage() {
  const [d, setD] = useState(null);

  useEffect(() => {
    let alive = true;
    apiGet("/api/track-record?limit=20")
      .then((x) => alive && setD(x))
      .catch(() => alive && setD({ summary: {}, recent: [] }));
    return () => {
      alive = false;
    };
  }, []);

  if (!d) return <div className="mt-8 h-40" aria-hidden />;

  const s = d.summary || {};
  const rows = d.recent || [];

  return (
    <section className="mt-7">
      <h2 className="fa-section-title">AI 战绩墙</h2>
      <p className="mt-1 text-[0.76rem]" style={{ color: "var(--fa-muted)" }}>
        每一条判断生成时记下当时价格，到期后由系统自动补录事后价格。亏的也在里面，没有挑过。
      </p>

      {s.directional_count ? (
        <div className="mt-5 grid grid-cols-1 gap-5 sm:grid-cols-3">
          <div>
            <div className="text-[0.76rem]" style={{ color: "var(--fa-muted)" }}>
              方向判断胜率
            </div>
            <div className="mt-0.5 text-[1.5rem] font-semibold tracking-tight">
              {s.win_rate?.toFixed(0)}%
            </div>
            <div className="mt-0.5 text-[0.72rem]" style={{ color: "var(--fa-faint)" }}>
              {s.directional_count} 条买入/卖出判断中说对 {s.hits} 条
            </div>
          </div>
          <div>
            <div className="text-[0.76rem]" style={{ color: "var(--fa-muted)" }}>
              平均事后涨跌
            </div>
            <div
              className="mt-0.5 text-[1.5rem] font-semibold tracking-tight"
              style={{ color: moveColor(s.avg_return_pct) }}
            >
              {fmtPct(s.avg_return_pct)}
            </div>
          </div>
          <div>
            <div className="text-[0.76rem]" style={{ color: "var(--fa-muted)" }}>
              已回填判断
            </div>
            <div className="mt-0.5 text-[1.5rem] font-semibold tracking-tight">
              {s.total_reviewed}
            </div>
          </div>
        </div>
      ) : (
        <p className="mt-4 text-[0.82rem]" style={{ color: "var(--fa-faint)" }}>
          还没有已回填事后价格的判断记录。
        </p>
      )}

      <h3 className="fa-section-title mt-9 text-[0.95rem]">最近 20 条</h3>
      <div className="mt-2">
        {rows.map((r, i) => {
          const mark =
            r.hit === true ? ["说对", "var(--fa-down)"]
            : r.hit === false ? ["说错", "var(--fa-up)"]
            : ["无方向", "var(--fa-faint)"];
          return (
            <div
              key={`${r.symbol}-${r.created_at}-${i}`}
              className="flex items-center gap-3 py-2 text-[0.8rem]"
              style={{ borderBottom: "1px solid var(--fa-border)" }}
            >
              <span className="w-[42px] shrink-0" style={{ color: "var(--fa-faint)" }}>
                {(r.created_at || "").slice(5, 10)}
              </span>
              <span className="min-w-0 flex-1 truncate">{r.name || r.symbol}</span>
              <span className="w-[34px] shrink-0" style={{ color: "var(--fa-text-2)" }}>
                {r.action}
              </span>
              <span className="w-[30px] shrink-0" style={{ color: "var(--fa-faint)" }}>
                {r.score ?? "—"}
              </span>
              <span
                className="w-[62px] shrink-0 text-right font-semibold"
                style={{ color: moveColor(r.return_pct) }}
              >
                {fmtPct(r.return_pct)}
              </span>
              <span
                className="w-[44px] shrink-0 text-right text-[0.72rem]"
                style={{ color: mark[1] }}
              >
                {mark[0]}
              </span>
            </div>
          );
        })}
      </div>
      <p className="mt-3 text-[0.72rem]" style={{ color: "var(--fa-faint)" }}>
        「说对/说错」只对买入、卖出这类带方向的结论成立，持有/观望不计入胜率。
      </p>
    </section>
  );
}
