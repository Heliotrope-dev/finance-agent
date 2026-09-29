"use client";

import { useEffect, useState } from "react";
import { fetchMe, login, register, onAuthChange } from "../lib/auth";

// 登录墙。包住整站内容：未登录时原地渲染登录表单，不跳路由。
//
// 为什么不做成 /login 路由：这是静态导出，没有服务端重定向。做成路由的话，
// 用户直接打开 /sim/ 会先闪一下空页面再跳走，刷新、分享链接都要额外处理
// 回跳。原地渲染没有这些问题——地址栏保持在他本来要去的页面，登录成功后
// 那个页面直接就出来了。
//
// 行为跟 Streamlit 版一致：不登录什么都看不到（游客模式 2026-08-25 关掉了）。

export default function AuthGate({ children }) {
  const [email, setEmail] = useState(undefined); // undefined=还在确认，null=未登录

  useEffect(() => {
    let alive = true;
    const check = () =>
      fetchMe().then((e) => {
        if (alive) setEmail(e);
      });
    check();
    return onAuthChange(check);
  }, []);

  // 确认登录状态期间不渲染任何东西。这一步通常几十毫秒（localStorage 里有
  // token 时是一个 /api/auth/me 往返），闪一下登录表单再切成内容比空一下更糟。
  if (email === undefined) return null;
  if (email === null) return <LoginForm />;
  return children;
}

function LoginForm() {
  const [mode, setMode] = useState("login"); // login | register
  const [form, setForm] = useState({ email: "", pw: "", pw2: "" });
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null); // {type: "error"|"ok", text}

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });

  async function submit(e) {
    e.preventDefault();
    if (busy) return;
    setMsg(null);

    if (mode === "register") {
      // 这三条校验跟 Streamlit 版登录页一字不差，后端也再校验一遍。
      // 前端这层只是为了少一次往返，不是安全边界。
      if (!form.email || !form.email.includes("@")) {
        return setMsg({ type: "error", text: "请输入有效邮箱" });
      }
      if (form.pw.length < 6) {
        return setMsg({ type: "error", text: "密码至少6位" });
      }
      if (form.pw !== form.pw2) {
        return setMsg({ type: "error", text: "两次密码不一致" });
      }
    }

    setBusy(true);
    try {
      if (mode === "login") {
        await login(form.email, form.pw);
        // 登录成功不用做别的：login() 会广播，AuthGate 重新确认身份后
        // 直接渲染内容。
      } else {
        await register(form.email, form.pw);
        setMode("login");
        setForm({ email: form.email, pw: "", pw2: "" });
        setMsg({ type: "ok", text: "注册成功，用这个邮箱登录" });
      }
    } catch (err) {
      setMsg({ type: "error", text: err.message });
    } finally {
      setBusy(false);
    }
  }

  const tab = (key, label) => (
    <button
      type="button"
      onClick={() => {
        setMode(key);
        setMsg(null);
      }}
      className="pb-2 text-[0.88rem] transition-colors"
      style={{
        color: mode === key ? "var(--fa-text)" : "var(--fa-muted)",
        fontWeight: mode === key ? 600 : 400,
        borderBottom:
          mode === key ? "2px solid var(--fa-text)" : "2px solid transparent",
        marginBottom: "-1px",
      }}
    >
      {label}
    </button>
  );

  return (
    <div className="mx-auto w-full max-w-[340px] pt-16">
      <div className="pb-6 text-center">
        <div className="text-[1.15rem] font-semibold tracking-tight">
          Invest Agent
        </div>
        <div className="mt-1 text-[0.78rem]" style={{ color: "var(--fa-muted)" }}>
          行情 + 财务 + 新闻交叉验证
        </div>
      </div>

      <div
        className="flex gap-6"
        style={{ borderBottom: "1px solid var(--fa-border)" }}
      >
        {tab("login", "登录")}
        {tab("register", "注册")}
      </div>

      <form onSubmit={submit} className="mt-5 flex flex-col gap-3">
        <Field
          id="auth-email"
          label="邮箱"
          type="email"
          value={form.email}
          onChange={set("email")}
          placeholder="your@email.com"
          autoComplete="email"
        />
        <Field
          id="auth-pw"
          label={mode === "register" ? "密码（至少6位）" : "密码"}
          type="password"
          value={form.pw}
          onChange={set("pw")}
          autoComplete={mode === "login" ? "current-password" : "new-password"}
        />
        {mode === "register" && (
          <Field
            id="auth-pw2"
            label="确认密码"
            type="password"
            value={form.pw2}
            onChange={set("pw2")}
            autoComplete="new-password"
          />
        )}

        {msg && (
          <div
            className="text-[0.78rem]"
            style={{
              color: msg.type === "error" ? "var(--fa-up)" : "var(--fa-down)",
            }}
          >
            {msg.text}
          </div>
        )}

        <button
          type="submit"
          disabled={busy}
          className="mt-1 rounded py-2 text-[0.88rem] font-medium transition-opacity"
          style={{
            background: "var(--fa-text)",
            color: "var(--fa-surface)",
            opacity: busy ? 0.6 : 1,
            cursor: busy ? "default" : "pointer",
          }}
        >
          {busy ? "请稍候…" : mode === "login" ? "登录" : "注册账号"}
        </button>
      </form>

      <p
        className="mt-5 text-center text-[0.72rem] leading-relaxed"
        style={{ color: "var(--fa-faint)" }}
      >
        跟 math-agent 共用同一套账号，
        <br />
        那边注册过这里可以直接登录。
      </p>
    </div>
  );
}

function Field({ id, label, ...rest }) {
  return (
    <label htmlFor={id} className="flex flex-col gap-1">
      <span className="text-[0.75rem]" style={{ color: "var(--fa-muted)" }}>
        {label}
      </span>
      <input
        id={id}
        {...rest}
        className="rounded px-3 py-2 text-[0.88rem] outline-none"
        style={{
          background: "var(--fa-surface)",
          border: "1px solid var(--fa-border-2)",
          color: "var(--fa-text)",
        }}
      />
    </label>
  );
}
