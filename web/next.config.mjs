/** @type {import('next').NextConfig} */
const nextConfig = {
  // 静态导出：构建产物是一堆静态文件，由 nginx 直接发，服务器上不跑 Node
  // 运行时。这台 VPS 只有 1.9G 内存、2026-09-11 刚因为内存打满触发过一次
  // OOM（Streamlit + OpenClaw + Futu OpenD + 若干 cron 已经占掉大半），
  // 再常驻一个 Next SSR 进程是拿稳定性换一点点渲染便利，不划算。
  output: "export",
  // 静态导出下 next/image 的优化服务跑不起来，关掉优化走原生 img。
  images: { unoptimized: true },
  // 导出成 /sim/index.html 这种目录形式，nginx 不用额外配 try_files 规则。
  trailingSlash: true,
  // 新前端先挂在 /next 子路径下，跟正在跑的 Streamlit 并存：迁移期必须能
  // 随时对照两边，而且线上流量不受影响。等功能对齐、用户决定切换时，把这行
  // 去掉、nginx 的 location / 指向 out/ 即可。
  basePath: "/next",
  assetPrefix: "/next",
};

export default nextConfig;
