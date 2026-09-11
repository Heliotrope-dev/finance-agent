"use client";

import { useEffect, useState } from "react";
import { apiGet, fmtPrice, fmtStamp } from "../lib/api";

const DIMS = [
  ["fundamental", "基本面"],
  ["price_position", "价格位置"],
  ["technical", "技术面"],
  ["chips", "筹码面"],
  ["analyst", "分析师"],
  ["data_certainty", "数据确定性"],
];

// 六维拆解默认收起。Streamlit 那版一开始把六条进度条画在卡片正面，实测
// 一张卡被撑高一半、把真正该读的理由挤到屏幕外，一屏只放得下一张半。
// 这里同样只在展开后显示。
function Breakdown({ b }) {
  const rows = DIMS.map(([k, label]) => {
    const v = b?.[k];
    const mx = b?.[`${k}_max`];
    if (v === null || v === undefined || !mx) return null;
    const pct = Math.max(0, Math.min(1, v / mx)) * 100;
    return (
      <div key={k} className="mt-1 flex items-center gap-2">
        <span className="w-[60px] shrink-0 text-[0.7rem]" style={{ color: "var(--fa-faint)" }}>
          {label}
        </span>
        <span
          className="h-1 w-[120px] shrink-0 rounded-full"
          style={{
            background: `linear-gradient(to right, var(--fa-text-2) 0 ${pct}%, var(--fa-border) ${pct}% 100%)`,
          }}
        />
        <span className="text-[0.7rem]" style={{ color: "var(--fa-faint)" }}>
          {v}/{mx}
        </span>
      </div>
    );
  }).filter(Boolean);

  if (!rows.length) return null;
  return <div className="mt-2">{rows}</div>;
}

function Card({ row }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="py-3" style={{ borderBottom: "1px solid var(--fa-border)" }}>
      <div className="flex items-baseline justify-between gap-3">
        <span className="font-semibold tracking-tight">
          <span className="mr-2 font-medium" style={{ color: "var(--fa-faint)" }}>
            {row.rank}
          </span>
          {row.name}
          <span className="ml-1.5 text-[0.74rem] font-normal" style={{ color: "var(--fa-faint)" }}>
            {row.symbol}
          </span>
        </span>
        <span className="flex shrink-0 items-baseline gap-2.5">
          {row.score !== null && row.score !== undefined ? (
            <span
              className="text-[1.02rem] font-semibold tracking-tight"
              style={{ color: row.score >= 60 ? "var(--fa-text)" : "var(--fa-muted)" }}
            >
              {row.score}
            </span>
          ) : null}
          <span className="text-[0.72rem] font-semibold" style={{ color: "var(--fa-muted)" }}>
            研究观点：{row.action}
          </span>
        </span>
      </div>
      <div className="mt-1 text-[0.72rem]" style={{ color: "var(--fa-faint)" }}>
        现价 {fmtPrice(row.price_at_advice)}
        {row.created_at ? `（${fmtStamp(row.created_at)}取价）` : ""}
      </div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="mt-2 text-[0.74rem]"
        style={{ color: "var(--fa-muted)" }}
      >
        {open ? "收起维度打分" : "维度打分"}
      </button>
      {open ? <Breakdown b={row.breakdown} /> : null}
    </div>
  );
}

export default function Leaderboard() {
  const [boards, setBoards] = useState(null);

  useEffect(() => {
    let alive = true;
    apiGet("/api/leaderboard?limit=5")
      .then((d) => alive && setBoards(d.boards || {}))
      .catch(() => alive && setBoards({}));
    return () => {
      alive = false;
    };
  }, []);

  if (!boards) return <div className="h-24" aria-hidden />;

  const groups = [
    ["港股", boards.HK],
    ["美股", boards.US],
  ].filter(([, rows]) => rows && rows.length);

  return (
    <section className="mt-10">
      <h2 className="fa-section-title">投研观察排行榜</h2>
      <p className="mt-1 text-[0.76rem]" style={{ color: "var(--fa-muted)" }}>
        这是基本面和技术面的研究排序，不是下单指令。结论回答的是「现在值不值得新建仓」，
        跟持仓视角的「已持有的仓位要不要继续拿」是两次独立判断，不一致属正常。
      </p>
      {!groups.length ? (
        <p className="mt-4 text-[0.8rem]" style={{ color: "var(--fa-faint)" }}>
          还没有生成过投研观察排行榜。
        </p>
      ) : (
        groups.map(([label, rows]) => (
          <div key={label} className="mt-5">
            <div className="text-[0.78rem] font-semibold" style={{ color: "var(--fa-text-2)" }}>
              {label}
            </div>
            <div className="mt-1">
              {rows.map((r) => (
                <Card key={`${r.market}-${r.symbol}`} row={r} />
              ))}
            </div>
          </div>
        ))
      )}
    </section>
  );
}
