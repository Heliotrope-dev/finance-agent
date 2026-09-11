"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

// 分区跟 Streamlit 那版保持一致的顺序和命名。持仓/自选需要登录态，这一版
// 先不做（登录逻辑还在 Streamlit 那边），列在这里只会点进去是空页面，
// 所以暂时不放——宁可少一个入口，也不要一个点了没反应的入口。
const TABS = [
  { href: "/", label: "首页" },
  { href: "/sim/", label: "AI模拟炒股" },
  { href: "/record/", label: "AI战绩墙" },
];

export default function Nav() {
  const pathname = usePathname();
  return (
    <nav
      className="mt-4 flex gap-6 overflow-x-auto"
      style={{ borderBottom: "1px solid var(--fa-border)" }}
    >
      {TABS.map((t) => {
        const active =
          t.href === "/" ? pathname === "/" : pathname.startsWith(t.href);
        return (
          <Link
            key={t.href}
            href={t.href}
            className="whitespace-nowrap pb-2 text-[0.88rem] transition-colors"
            style={{
              color: active ? "var(--fa-text)" : "var(--fa-muted)",
              fontWeight: active ? 600 : 400,
              borderBottom: active
                ? "2px solid var(--fa-text)"
                : "2px solid transparent",
              marginBottom: "-1px",
            }}
          >
            {t.label}
          </Link>
        );
      })}
    </nav>
  );
}
