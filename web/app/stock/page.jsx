"use client";

import { useEffect, useRef, useState } from "react";
import { CandlestickSeries, createChart } from "lightweight-charts";
import { ApiState, useApi } from "../../components/ApiState";
import { fmtPrice } from "../../lib/api";

function PriceChart({ items }) {
  const host = useRef(null);
  useEffect(() => {
    if (!host.current || !items.length) return;
    const chart = createChart(host.current, { height: 320, layout: { background: { color: "transparent" }, textColor: "#82858e" }, grid: { vertLines: { color: "#eaeaef" }, horzLines: { color: "#eaeaef" } } });
    const series = chart.addSeries(CandlestickSeries, { upColor: "#d0342c", downColor: "#12855f", borderVisible: false, wickUpColor: "#d0342c", wickDownColor: "#12855f" });
    series.setData(items.filter((item) => item.time && item.open != null && item.close != null));
    chart.timeScale().fitContent();
    const resize = () => chart.applyOptions({ width: host.current?.clientWidth ?? 600 });
    resize();
    window.addEventListener("resize", resize);
    return () => { window.removeEventListener("resize", resize); chart.remove(); };
  }, [items]);
  return <div ref={host} className="mt-5 w-full" />;
}

export default function StockPage() {
  const [input, setInput] = useState("US:AAPL");
  const [selection, setSelection] = useState({ market: "US", symbol: "AAPL" });
  const key = `${selection.market}/${selection.symbol}`;
  const quote = useApi(`/api/quote/${key}`, { refreshInterval: 5_000 });
  const kline = useApi(`/api/kline/${key}?days=180`);
  const analysis = useApi(`/api/analysis/${key}`, { refreshInterval: 5 * 60_000 });
  function submit(event) {
    event.preventDefault();
    const [market, symbol] = input.toUpperCase().split(":");
    if (["A", "HK", "US", "CRYPTO"].includes(market) && symbol) setSelection({ market, symbol });
  }
  const q = quote.data?.quote ?? {};
  return <section className="pt-6"><h1 className="fa-section-title">个股详情</h1><form onSubmit={submit} className="mt-4 flex gap-2"><input value={input} onChange={(e) => setInput(e.target.value)} className="min-w-0 flex-1 rounded border border-[var(--fa-border-2)] bg-white px-3 py-2 text-sm" aria-label="市场和代码"/><button className="rounded bg-[var(--fa-text)] px-4 py-2 text-sm text-white">查询</button></form><p className="mt-1 text-xs text-[var(--fa-faint)]">格式：A:600519、HK:00700、US:AAPL、CRYPTO:BTC</p><ApiState error={quote.error} loading={quote.isLoading} empty={!Object.keys(q).length}><div className="mt-6"><h2 className="text-lg font-semibold">{q.名称 || selection.symbol}</h2><p className="text-2xl font-semibold">{fmtPrice(q.最新价)}</p></div></ApiState><ApiState error={kline.error} loading={kline.isLoading} empty={!kline.data?.items?.length}><PriceChart items={kline.data?.items ?? []}/></ApiState><div className="mt-6 fa-hairline pb-6"><h2 className="font-semibold">最近一次系统判断</h2><ApiState error={analysis.error} loading={analysis.isLoading} empty={!analysis.data?.analysis?.in_pool}><p className="mt-2 whitespace-pre-wrap text-sm leading-7 text-[var(--fa-text-2)]">{analysis.data?.analysis?.verdict_excerpt}</p></ApiState></div></section>;
}
