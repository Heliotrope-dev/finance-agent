"use client";

import useSWR from "swr";
import { apiGet } from "../lib/api";

export function useApi(path, options = {}) {
  return useSWR(path, apiGet, {
    revalidateOnFocus: false,
    dedupingInterval: 30_000,
    ...options,
  });
}

export function ApiState({ error, loading, empty, children }) {
  if (loading) return <p className="mt-5 text-sm text-[var(--fa-muted)]">加载中…</p>;
  if (error) return <p className="mt-5 text-sm text-[var(--fa-up)]">暂时取不到数据，请稍后重试。</p>;
  if (empty) return <p className="mt-5 text-sm text-[var(--fa-muted)]">暂无数据。</p>;
  return children;
}
