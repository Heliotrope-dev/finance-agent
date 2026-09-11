"use client";

import { useEffect, useState } from "react";
import { apiGet, fmtPrice } from "../lib/api";

// 今日可执行清单。放在首页第一位——这是整个产品最有行动价值的一块，
// Streamlit 那版原来把它压在页面 80% 的位置，前端审计第一条就是这个。
//
// "可执行"的判定不在前端做：daily_plan.py 已经把买入区间/股数/止损/目标/
// 盈亏比全部校验过并写进快照，前端只负责如实展示，不能自己另算一套闸门。
function Row({ item }) {
  const ready = item["新开仓状态"] === "可执行";
  const lo = item["买入下沿"];
  const hi = item["买入上限"];
  let range = "尚无有效买入区间";
  if (typeof lo === "number" && typeof hi === "number") {
    range = `买入区间 ${fmtPrice(lo)}–${fmtPrice(hi)}`;
  } else if (typeof hi === "number") {
    range = `买入上限 ${fmtPrice(hi)}`;
  } else if (typeof lo === "number") {
    range = `买入下沿 ${fmtPrice(lo)}`;
  }

  const bits = [];
  if (typeof item["现价"] === "number") bits.push(`现价 ${fmtPrice(item["现价"])}`);
  bits.push(range);
  if (item["建议股数"]) bits.push(`买入 ${item["建议股数"]} 股`);
  if (typeof item["止损参考"] === "number") bits.push(`止损 ${fmtPrice(item["止损参考"])}`);
  if (typeof item["目标价"] === "number") bits.push(`目标 ${fmtPrice(item["目标价"])}`);
  if (typeof item["盈亏比"] === "number") bits.push(`盈亏比 ${item["盈亏比"].toFixed(2)}:1`);

  return (
    <div className="py-3" style={{ borderBottom: "1px solid var(--fa-border)" }}>
      <div className="flex items-baseline justify-between gap-3">
        <span className="font-semibold tracking-tight">
          {item["名称"]}
          <span className="ml-1.5 text-[0.74rem] font-normal" style={{ color: "var(--fa-faint)" }}>
            {item["代码"]} · {item["市场"]}
          </span>
        </span>
        <span
          className="shrink-0 text-[0.72rem] font-semibold"
          style={{ color: ready ? "var(--fa-down)" : "var(--fa-faint)" }}
        >
          {ready ? "可执行" : "仅观察"}
        </span>
      </div>
      <div className="mt-1 text-[0.76rem]" style={{ color: "var(--fa-text-2)" }}>
        {bits.join(" · ")}
      </div>
      {/* 没过闸门的也要写明"差在哪"——用户明确要求过"就算没达到门槛也要写
          啊我得参考啊"，把当天几十支的评分工作全藏起来是更糟的选择。 */}
      {item["不可执行原因"] ? (
        <div className="mt-1 text-[0.72rem]" style={{ color: "var(--fa-faint)" }}>
          {item["不可执行原因"]}
        </div>
      ) : null}
    </div>
  );
}

export default function PlanList() {
  const [data, setData] = useState(null);

  useEffect(() => {
    let alive = true;
    Promise.all([
      apiGet("/api/plan/hk").catch(() => null),
      apiGet("/api/plan/us").catch(() => null),
    ]).then(([hk, us]) => {
      if (!alive) return;
      setData({ hk, us });
    });
    return () => {
      alive = false;
    };
  }, []);

  if (!data) return <div className="h-24" aria-hidden />;

  const groups = [
    ["港股", data.hk],
    ["美股", data.us],
  ].filter(([, d]) => d && (d.items || []).length);

  return (
    <section className="mt-7">
      <h2 className="fa-section-title">今日可执行清单</h2>
      <p className="mt-1 text-[0.76rem]" style={{ color: "var(--fa-muted)" }}>
        通过买入区间、股数、止损、目标和盈亏比全部校验的标的排在最前；没过闸门的也列出来，并写明差在哪。
      </p>

      {!groups.length ? (
        <p className="mt-4 text-[0.8rem]" style={{ color: "var(--fa-faint)" }}>
          今天还没有生成盘前计划（港股09:00前、美股21:00前各跑一次）。
        </p>
      ) : (
        groups.map(([label, d]) => {
          const items = [...(d.items || [])].sort(
            (a, b) =>
              (b["新开仓状态"] === "可执行" ? 1 : 0) -
              (a["新开仓状态"] === "可执行" ? 1 : 0),
          );
          return (
            <div key={label} className="mt-5">
              <div className="text-[0.78rem] font-semibold" style={{ color: "var(--fa-text-2)" }}>
                {label}
                <span className="ml-2 font-normal" style={{ color: "var(--fa-faint)" }}>
                  {d.date || ""}
                </span>
              </div>
              <div className="mt-1">
                {items.map((it) => (
                  <Row key={`${it["市场"]}-${it["代码"]}`} item={it} />
                ))}
              </div>
            </div>
          );
        })
      )}
    </section>
  );
}
