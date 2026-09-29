import "./globals.css";
import Nav from "../components/Nav";
import AuthGate from "../components/AuthGate";
import UserBadge from "../components/UserBadge";

export const metadata = {
  title: "Invest Agent",
  description: "可验证、有纪律、会解释的 AI 投研助手",
};

export const viewport = {
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({ children }) {
  return (
    <html lang="zh-CN">
      <body>
        {/* 整站一个居中容器。Streamlit 那版的内容宽度由框架决定，这里显式
            定死最大宽度：行情类页面一行信息多，太宽会让视线横向跑太远。 */}
        <div className="mx-auto w-full max-w-[1120px] px-4 pb-24 sm:px-6">
          {/* 登录墙包住整站。未登录时这里渲染的是登录表单，页头/导航/页脚
              都不出现——登录页不需要那些，跟 Streamlit 版一致。 */}
          <AuthGate>
            <header className="flex items-baseline justify-between pt-7">
              <span className="text-[1.05rem] font-semibold tracking-tight">
                Invest Agent
              </span>
              <UserBadge />
            </header>
            <Nav />
            <main>{children}</main>
            <footer
              className="mt-16 pt-6 text-[0.72rem] fa-hairline-top"
              style={{ color: "var(--fa-faint)", borderTop: "1px solid var(--fa-border)" }}
            >
              仅供参考，不构成投资建议，请自行判断。
            </footer>
          </AuthGate>
        </div>
      </body>
    </html>
  );
}
