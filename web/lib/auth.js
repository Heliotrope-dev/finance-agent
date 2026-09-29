// 登录状态。沿用 auth.py 那套 token（Supabase sessions 表，7 天），跟
// Streamlit 版、math-agent 共用同一个账号体系。
//
// token 存两份，是为了覆盖两条不同的进入路径：
// - localStorage：新前端自己登录时写的，fetch 时放进 Authorization 头，
//   跨源（本地 npm run dev 前端 3000 / API 8600）也能用；
// - Cookie fa_auth_tok：Streamlit 版登录时写的那个。同源部署下浏览器会自动
//   带上，所以"在 Streamlit 那边登录过，直接打开新前端"是登录态，不用重登。
//   迁移期两个前端并存，这条路径必须留着。

const KEY = "fa_auth_tok";

function readCookie(name) {
  if (typeof document === "undefined") return "";
  const hit = document.cookie
    .split("; ")
    .find((row) => row.startsWith(`${name}=`));
  return hit ? decodeURIComponent(hit.slice(name.length + 1)) : "";
}

export function getToken() {
  if (typeof window === "undefined") return "";
  // localStorage 可能抛（隐私窗口、站点数据被禁），不能让它带崩整个页面。
  try {
    const stored = window.localStorage.getItem(KEY);
    if (stored) return stored;
  } catch {
    /* 忽略，退回 cookie */
  }
  return readCookie(KEY);
}

function setToken(token) {
  try {
    window.localStorage.setItem(KEY, token);
  } catch {
    /* 存不了就只靠后端下发的 cookie，不影响当次会话 */
  }
}

function clearToken() {
  try {
    window.localStorage.removeItem(KEY);
  } catch {
    /* 同上 */
  }
  // 后端 logout 也会删 cookie，但那只在同源时生效；本地跨源开发时这里补一刀。
  document.cookie = `${KEY}=; Max-Age=0; path=/`;
}

// ── 登录状态变化的广播 ───────────────────────────────────────────────────
// AuthGate 要在登录成功后立刻重渲染。不引状态管理库——整个应用只有"登录/
// 未登录"这一个全局状态，一个 Set 够了。
const listeners = new Set();

export function onAuthChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function broadcast() {
  listeners.forEach((fn) => fn());
}

// ── 接口 ────────────────────────────────────────────────────────────────
const BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

async function post(path, body) {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(getToken() ? { Authorization: `Bearer ${getToken()}` } : {}),
    },
    // 让浏览器带上/接收 cookie：登录成功时后端会 Set-Cookie，同源部署下
    // 这份 cookie 就是"下次打开还在登录态"的依据。
    credentials: "include",
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try {
    data = await res.json();
  } catch {
    /* 后端异常时可能不是 JSON，下面统一按状态码处理 */
  }
  if (!res.ok) {
    // detail 是 FastAPI HTTPException 的字段，auth.py 的原始文案在里面。
    // 那些文案是刻意设计过的（防账号枚举），原样显示给用户，不要改写。
    throw new Error(data.detail || `请求失败（${res.status}）`);
  }
  return data;
}

export async function login(email, password) {
  const data = await post("/api/auth/login", { email, password });
  setToken(data.token);
  broadcast();
  return data;
}

export async function register(email, password) {
  return post("/api/auth/register", { email, password });
}

export async function logout() {
  try {
    await post("/api/auth/logout");
  } catch {
    // 后端删 session 失败不影响本地登出——本地 token 清掉，用户就已经
    // 进不去了，剩下的过期后自然失效。
  }
  clearToken();
  broadcast();
}

// 用当前 token 换取用户身份。401 时返回 null 而不是抛错：调用方（AuthGate）
// 只关心"能不能进"，不需要区分是没登录还是过期。
export async function fetchMe() {
  const token = getToken();
  if (!token) return null;
  try {
    const res = await fetch(`${BASE}/api/auth/me`, {
      headers: { Authorization: `Bearer ${token}` },
      credentials: "include",
      cache: "no-store",
    });
    if (!res.ok) {
      if (res.status === 401) clearToken();
      return null;
    }
    const data = await res.json();
    return data.email || null;
  } catch {
    // 网络错误不等于未登录，但这里也没法区分。返回 null 会退回登录页，
    // 用户重登一次即可——比卡在空白页强。
    return null;
  }
}

export { clearToken };
