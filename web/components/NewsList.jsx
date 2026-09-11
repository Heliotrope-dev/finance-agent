"use client";

import { useEffect, useState } from "react";
import { apiGet } from "../lib/api";

// 资讯。每条带"影响：XX"——这批新闻本来就是拿当天真实异动的股票名当关键词
// 搜回来的，related 就是那个关键词本身，是事实不是 AI 推断出来的关联。
export default function NewsList() {
  const [items, setItems] = useState(null);

  useEffect(() => {
    let alive = true;
    apiGet("/api/news?limit=12")
      .then((d) => alive && setItems(d.items || []))
      .catch(() => alive && setItems([]));
    return () => {
      alive = false;
    };
  }, []);

  if (!items) return <div className="h-24" aria-hidden />;
  if (!items.length) return null;

  return (
    <section className="mt-10">
      <h2 className="fa-section-title">今日重磅消息</h2>
      <div className="mt-3">
        {items.map((n, i) => (
          <div
            key={n.url || i}
            className="py-2.5"
            style={{ borderBottom: "1px solid var(--fa-border)" }}
          >
            <a
              href={n.url}
              target="_blank"
              rel="noreferrer"
              className="text-[0.88rem] leading-relaxed"
              style={{ color: "var(--fa-text)", textDecoration: "none" }}
            >
              {n.summary}
            </a>
            <div className="mt-1 text-[0.72rem]" style={{ color: "var(--fa-faint)" }}>
              {n.related ? `影响：${n.related} · ` : ""}
              {n.tag || ""} · {n["日期"] || ""}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
