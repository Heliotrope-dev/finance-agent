"use client";

import { useEffect, useState } from "react";
import { apiGet, fmtPrice, fmtPct, moveColor } from "../lib/api";

// 首页顶部的指数行情条。Streamlit 那版把它放在最顶上的理由同样适用：
// 打开投资类产品第一件事是想知道"今天市场怎么样"，不是看一张世界地图。
// 窄屏横向滑动，不换行——六个指数换行会把首屏吃掉一半。
export default function IndexStrip() {
  const [items, setItems] = useState(null);

  useEffect(() => {
    let alive = true;
    const load = () =>
      apiGet("/api/indices")
        .then((d) => alive && setItems(d.items || []))
        .catch(() => alive && setItems([]));
    load();
    // 数据本身由服务端每分钟预热一次，这里 60 秒拉一次就够，不做秒级轮询。
    const t = setInterval(load, 60000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  if (items === null) {
    return <div className="h-[62px]" aria-hidden />;
  }
  if (!items.length) return null;

  return (
    <div
      className="flex gap-7 overflow-x-auto py-3"
      style={{ borderBottom: "1px solid var(--fa-border)" }}
    >
      {items.map((it) => (
        <div key={it.name} className="shrink-0">
          <div className="text-[0.7rem]" style={{ color: "var(--fa-faint)" }}>
            {it.name}
          </div>
          <div
            className="text-[0.98rem] font-semibold leading-tight"
            style={{ color: moveColor(it.change) }}
          >
            {fmtPrice(it.last)}
          </div>
          <div className="text-[0.7rem]" style={{ color: moveColor(it.change) }}>
            {fmtPct(it.change_pct)}
          </div>
        </div>
      ))}
    </div>
  );
}
