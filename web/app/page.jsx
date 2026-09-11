import IndexStrip from "../components/IndexStrip";
import PlanList from "../components/PlanList";
import Leaderboard from "../components/Leaderboard";
import NewsList from "../components/NewsList";

// 首页顺序跟 Streamlit 那版重排后的顺序一致：指数条 → 今日可执行清单 →
// 排行榜 → 资讯。理由见前端审计第8条——用户打开投资类产品，第一件事想
// 知道的是"今天该做什么"，不是"世界长什么样"。
export default function Home() {
  return (
    <>
      <IndexStrip />
      <PlanList />
      <Leaderboard />
      <NewsList />
    </>
  );
}
