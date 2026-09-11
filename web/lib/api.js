// 接口基地址。构建成静态文件之后跟 API 同源（nginx 把 /api 反代到
// uvicorn），所以默认空前缀；本地 npm run dev 时前端在 3000、API 在 8600，
// 用环境变量指过去。
const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

export async function apiGet(path) {
  const res = await fetch(`${BASE}${path}`, { cache: "no-store" });
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
}

// ── 格式化 ──────────────────────────────────────────────────────────────
// 这几个函数是从 Streamlit 那版的 _fmt_* 一一对应搬过来的，口径必须一致：
// 迁移期间两套前端会并存，同一个数字在两边显示得不一样比显示得丑更糟。

export function fmtPrice(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return Number(v).toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function fmtPct(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const n = Number(v);
  return `${n >= 0 ? "+" : ""}${n.toFixed(digits)}%`;
}

// 负号写在货币符号外面（-$744 而不是 $-744）——会计和行情软件的通行写法，
// Streamlit 那版 _fmt_usd_signed 修过同一个问题。
export function fmtUsdSigned(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const n = Number(v);
  const sign = n < 0 ? "-" : "+";
  return `${sign}$${Math.abs(n).toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

export function fmtUsd(v, digits = 0) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `$${Number(v).toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

// 涨跌色：红涨绿跌，0 走中性。跟 theme.py 同一套语义——这是"方向"，
// 不是"好坏"，别拿去表示成功失败。
export function moveColor(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) {
    return "var(--fa-neutral)";
  }
  const n = Number(v);
  if (n > 0) return "var(--fa-up)";
  if (n < 0) return "var(--fa-down)";
  return "var(--fa-neutral)";
}

// "MM-DD HH:mm"，用来标注"这个数字是什么时候取的"。Streamlit 那版在排行榜
// 现价后面加取价时间，就是为了解释同一支票在不同版块数字不一样——两边都是
// 对的，只是时间点不同。
export function fmtStamp(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const p = (x) => String(x).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
