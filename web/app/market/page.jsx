"use client";

import { useEffect, useState } from "react";
import { apiGet, fmtPrice, fmtPct, moveColor } from "../../lib/api";

// 行情分区。Streamlit 那版是三个互不牵连的 fragment（指数快照 / 涨跌排行 /
// 热门板块），这里照着拆成三个独立请求：其中港股排行本地实测要 81 秒、
// 美股板块干脆跑不出来，一个慢接口绝不能拖住整页——所以三块各自加载、
// 各自显示状态，先回来的先渲染。
const MARKETS = [
  { code: "A", label: "沪深" },
  { code: "HK", label: "港股" },
  { code: "US", label: "美股" },
  { code: "CRYPTO", label: "虚拟货币" },
];

export default function MarketPage() {
  const [market, setMarket] = useState("A");

  return (
    <div className="pt-5">
      <div className="flex gap-5 overflow-x-auto pb-1">
        {MARKETS.map((m) => (
          <button
            key={m.code}
            type="button"
            onClick={() => setMarket(m.code)}
            className="whitespace-nowrap text-[0.85rem] transition-colors"
            style={{
              color: market === m.code ? "var(--fa-text)" : "var(--fa-muted)",
              fontWeight: market === m.code ? 600 : 400,
              cursor: "pointer",
            }}
          >
            {m.label}
          </button>
        ))}
      </div>

      {/* key 带上 market：切市场时让三块整个重挂载，而不是在旧数据上打补丁。
          否则从沪深切到港股的 81 秒里，页面会一直显示沪深的数字却标着港股，
          比空着更容易误导——Streamlit 那版也栽过同一个坑。 */}
      <IndexCards key={`idx-${market}`} market={market} />
      <Movers key={`mv-${market}`} market={market} />
      <Sectors key={`sc-${market}`} market={market} />
    </div>
  );
}

// ── 通用的区块加载状态 ──────────────────────────────────────────────────
function Section({ title, note, state, empty, children }) {
  return (
    <section className="mt-7">
      <div className="flex items-baseline justify-between">
        <h2 className="text-[0.92rem] font-semibold">{title}</h2>
        {note && (
          <span className="text-[0.7rem]" style={{ color: "var(--fa-faint)" }}>
            {note}
          </span>
        )}
      </div>
      {state === "loading" && (
        <p className="mt-3 text-[0.78rem]" style={{ color: "var(--fa-muted)" }}>
          正在查询…（这个市场的数据源较慢时可能要等十几秒）
        </p>
      )}
      {state === "error" && (
        <p className="mt-3 text-[0.78rem]" style={{ color: "var(--fa-muted)" }}>
          暂时取不到，稍后再试。
        </p>
      )}
      {state === "done" && empty && (
        <p className="mt-3 text-[0.78rem]" style={{ color: "var(--fa-muted)" }}>
          暂无数据。
        </p>
      )}
      {state === "done" && !empty && children}
    </section>
  );
}

// 三块都用同一个取数模式，抽出来避免各写一遍 loading/error 状态机。
function useEndpoint(path) {
  const [data, setData] = useState(null);
  const [state, setState] = useState("loading");

  useEffect(() => {
    let alive = true;
    setState("loading");
    apiGet(path)
      .then((d) => {
        if (!alive) return;
        setData(d);
        setState("done");
      })
      .catch(() => alive && setState("error"));
    return () => {
      alive = false;
    };
  }, [path]);

  return [data, state];
}

function IndexCards({ market }) {
  const [data, state] = useEndpoint(`/api/market/${market}/indices`);
  const items = data?.items ?? [];

  return (
    <Section title="指数" state={state} empty={!items.length}>
      <div className="mt-3 grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-3">
        {items.map((it) => (
          <div key={it.name}>
            <div className="text-[0.72rem]" style={{ color: "var(--fa-faint)" }}>
              {it.name}
            </div>
            <div
              className="text-[1.05rem] font-semibold leading-tight"
              style={{ color: moveColor(it.change) }}
            >
              {fmtPrice(it.last)}
            </div>
            <div className="text-[0.72rem]" style={{ color: moveColor(it.change) }}>
              {fmtPct(it.change_pct)}
            </div>
          </div>
        ))}
      </div>
    </Section>
  );
}

function Movers({ market }) {
  const [data, state] = useEndpoint(`/api/market/${market}/movers?limit=15`);
  const items = data?.items ?? [];

  // 口径如实标注：沪深这一栏取的是涨停股池，不是"官方成分股"，
  // data_sources 里专门解释过为什么这么取，页面不能含糊带过。
  const note = market === "A" ? "取自涨停股池，非官方成分股名单" : "涨幅居前";

  return (
    <Section title="个股涨跌" note={note} state={state} empty={!items.length}>
      <div className="mt-3 flex flex-col">
        {items.map((it) => (
          <div
            key={it.symbol}
            className="flex items-baseline justify-between py-2"
            style={{ borderBottom: "1px solid var(--fa-border)" }}
          >
            <span className="flex min-w-0 items-baseline gap-2">
              <span className="truncate text-[0.85rem]">{it.name}</span>
              <span
                className="shrink-0 text-[0.7rem]"
                style={{ color: "var(--fa-faint)" }}
              >
                {it.symbol}
              </span>
            </span>
            <span className="flex shrink-0 items-baseline gap-3 tabular-nums">
              <span className="text-[0.85rem]">{fmtPrice(it.last)}</span>
              <span
                className="w-[4.5rem] text-right text-[0.8rem]"
                style={{ color: moveColor(it.change_pct) }}
              >
                {fmtPct(it.change_pct)}
              </span>
            </span>
          </div>
        ))}
      </div>
    </Section>
  );
}

function Sectors({ market }) {
  const [data, state] = useEndpoint(`/api/market/${market}/sectors?limit=12`);
  const items = data?.items ?? [];

  // 后端明确标了不支持（虚拟货币没有板块概念、美股那条数据源慢到不可用）
  // 就整块不渲染，而不是显示一个永远空着的区块。
  if (state === "done" && data && data.supported === false) return null;

  return (
    <Section
      title="热门板块"
      note="热度＝成交额代理指标，非官方人气榜"
      state={state}
      empty={!items.length}
    >
      <div className="mt-3 flex flex-col">
        {items.map((it) => (
          <div
            key={it.name}
            className="flex items-baseline justify-between py-2"
            style={{ borderBottom: "1px solid var(--fa-border)" }}
          >
            <span className="truncate text-[0.85rem]">{it.name}</span>
            <span
              className="shrink-0 text-[0.8rem] tabular-nums"
              style={{ color: moveColor(it.change_pct) }}
            >
              {fmtPct(it.change_pct)}
            </span>
          </div>
        ))}
      </div>
    </Section>
  );
}
