"use client";

import { useState } from "react";
import { ApiState, useApi } from "../../components/ApiState";

export default function IpoPage() {
  const [market, setMarket] = useState("HK");
  const { data, error, isLoading } = useApi("/api/ipo", { refreshInterval: 6 * 60 * 60_000 });
  const section = data?.markets?.[market] ?? { calendar: [], briefs: [] };
  return (
    <section className="pt-6">
      <h1 className="fa-section-title">新股专区</h1>
      <div className="mt-4 flex gap-5">{["A", "HK", "US"].map((value) => <button key={value} onClick={() => setMarket(value)} className="text-sm" style={{fontWeight: market === value ? 600 : 400, color: market === value ? "var(--fa-text)" : "var(--fa-muted)"}}>{value === "A" ? "沪深" : value === "HK" ? "港股" : "美股"}</button>)}</div>
      <ApiState error={error} loading={isLoading} empty={!section.calendar.length && !section.briefs.length}>
        <div className="mt-5 space-y-6">
          {section.briefs.map((item) => <article key={item.id ?? item.symbol} className="fa-hairline pb-5"><h2 className="font-semibold">{item.name} <span className="text-xs font-normal text-[var(--fa-faint)]">{item.symbol}</span></h2><p className="mt-2 whitespace-pre-wrap text-sm leading-7 text-[var(--fa-text-2)]">{item.brief_text}</p></article>)}
          {section.calendar.map((item) => <div key={`${item.symbol}-${item.list_date}`} className="flex justify-between gap-4 text-sm"><span>{item.name} <span className="text-[var(--fa-faint)]">{item.symbol}</span></span><span className="shrink-0 text-[var(--fa-muted)]">{item.list_date || "待定"}</span></div>)}
        </div>
      </ApiState>
    </section>
  );
}
