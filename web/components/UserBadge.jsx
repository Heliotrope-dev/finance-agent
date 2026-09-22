"use client";

import { useEffect, useState } from "react";
import { fetchMe, logout, onAuthChange } from "../lib/auth";

// 页头右侧的当前账号 + 登出。只在登录后渲染（整站被 AuthGate 包着，
// 未登录时根本走不到这里）。
export default function UserBadge() {
  const [email, setEmail] = useState(null);

  useEffect(() => {
    let alive = true;
    const check = () =>
      fetchMe().then((e) => {
        if (alive) setEmail(e);
      });
    check();
    return onAuthChange(check);
  }, []);

  if (!email) return null;

  return (
    <span className="flex items-baseline gap-3 text-[0.72rem]">
      {/* 邮箱可能很长，窄屏只显示 @ 前面那段，够用来确认"我登的是哪个号" */}
      <span style={{ color: "var(--fa-faint)" }}>
        <span className="hidden sm:inline">{email}</span>
        <span className="sm:hidden">{email.split("@")[0]}</span>
      </span>
      <button
        type="button"
        onClick={logout}
        className="transition-colors"
        style={{ color: "var(--fa-muted)", cursor: "pointer" }}
      >
        登出
      </button>
    </span>
  );
}
