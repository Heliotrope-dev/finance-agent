"use client";

import { ApiState, useApi } from "../../components/ApiState";

export default function CalendarPage() {
  const { data, error, isLoading } = useApi("/api/calendar", { refreshInterval: 6 * 60 * 60_000 });
  const items = data?.items ?? [];
  return (
    <section className="pt-6">
      <h1 className="fa-section-title">事件日历</h1>
      <p className="mt-1 text-xs text-[var(--fa-faint)]">确定性交易所事件；当前展示新股上市与认购截止日。</p>
      <ApiState error={error} loading={isLoading} empty={!items.length}>
        <div className="mt-4 overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="text-xs text-[var(--fa-faint)]"><tr><th className="py-2">上市日</th><th>市场</th><th>名称</th><th>代码</th><th>认购截止</th></tr></thead>
            <tbody>{items.map((item, index) => <tr key={`${item.market}-${item.symbol}-${index}`} className="fa-hairline"><td className="py-3">{item.list_date || "—"}</td><td>{item.market}</td><td>{item.name || "—"}</td><td>{item.symbol}</td><td>{item.apply_end || "—"}</td></tr>)}</tbody>
          </table>
        </div>
      </ApiState>
    </section>
  );
}
