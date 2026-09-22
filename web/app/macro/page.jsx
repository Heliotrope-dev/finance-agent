"use client";

import { ApiState, useApi } from "../../components/ApiState";

export default function MacroPage() {
  const { data, error, isLoading } = useApi("/api/macro", { refreshInterval: 15 * 60_000 });
  const items = data?.items ?? [];
  return (
    <section className="pt-6">
      <h1 className="fa-section-title">宏观简报</h1>
      <ApiState error={error} loading={isLoading} empty={!items.length}>
        <div className="mt-4 space-y-6">
          {items.map((item) => (
            <article key={item.id ?? item.topic} className="fa-hairline pb-6">
              <p className="text-xs text-[var(--fa-faint)]">{item.topic} · {String(item.created_at ?? "").slice(0, 16)}</p>
              <h2 className="mt-1 font-semibold">{item.title}</h2>
              <p className="mt-2 whitespace-pre-wrap text-sm leading-7 text-[var(--fa-text-2)]">{item.brief_text}</p>
            </article>
          ))}
        </div>
      </ApiState>
    </section>
  );
}
