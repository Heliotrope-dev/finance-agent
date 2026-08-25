# Invest Agent

多市场（A股 / 港股 / 美股）行情查询 + 财务数据 + 新闻资讯交叉验证工具，同时也是我自己在用的个人投资 AI 助理——代码全部开源。详情页等大部分模块 AI 只做交叉核实和总结，不做黑箱荐股；首页"AI 投研候选"模块是唯一的例外，会明确给买入/卖出/持有/观望结论（这是我自己每天在看的东西，公开是为了透明，连历史准不准都摆出来，不构成投资建议）。

线上地址：[invest.heliotrope.online](https://invest.heliotrope.online)

## 设计原则

- **只摆事实，先讲依据**：详情页先看一手资讯和原始数据，AI 分析摆在后面，交叉核实新闻/财务/技术面是否一致，不直接给操作指令。
- **技术面信号本地算，不靠 AI 编**：均线、MACD、实时动量都是 pandas 本地算好喂给 AI，避免"技术面尚可"这类空话。
- **诚实的追踪记录**：每次分析记录当时价格和 AI 判断方向，满 7 天自动回填价格算方向一致率（按总体/市场/方向拆开统计），页面上明确标注不构成投资建议。
- **"AI 投研候选"是刻意分开的例外**：基本面为主、技术面辅助、额外看 52 周价格位置防止无脑追高/恐高。全市场候选池用 Futu 官方筛选接口做量化初筛，私有脚本每个工作日收盘后跑一次落库，不现场跑（一次全市场扫描约 40 次 AI 调用）。同样有"记录判断→回填价格→算一致率"机制，直接显示在首页，可被当场核实。

## 功能

- **AI 投研候选**（首页）：全市场量化初筛 + AI 综合判断，`advisor.py` 每日收盘后更新一次
- **多市场行情**：A股按涨跌幅、港股按人气榜、美股固定核心名单，`@st.fragment` 拆分刷新粒度
- **实时价格**：详情页/持仓每 3 秒自动刷新，红涨绿跌闪烁提示
- **分时图**：A股新浪分钟数据，港美股优先走本地 Futu OpenD 网关，无连接时退化日K
- **AI 深度分析**：资讯解读、财务摘要、大盘对比、技术面消息面交叉验证、综合评分，流式输出并缓存
- **指数详情页**：K线+成分股+AI分析，行业主题指数用手动维护的真实成分股名单
- **热门板块**：按成交额排序，A股走同花顺接口，港美股走Futu板块快照
- **持仓分析**：自动判断股票所属市场，环形图（多币种折算人民币）、今日收益实时刷新、并发拉取多只股票行情
- **登录/账号**：跟 math-agent 共用同一套 Supabase 账号体系，token 不写进 URL query params

## 项目结构

```
app.py              # 入口：登录/鉴权、行情+持仓分析+详情页的全部UI渲染
data_sources.py      # 所有外部数据源：AkShare/BaoStock/新浪/东财/财新/Futu OpenAPI
analysis.py          # DeepSeek流式分析（资讯/财务/大盘对比/技术面消息面交叉验证/总结）
charts.py            # K线/均线/MACD/成交量图表 + 本地统计指标计算（不经过AI）
tracker.py           # SQLite：分析记录+方向一致率、持仓(positions表)、搜索历史、投研候选(advice表)
advisor.py           # 私有脚本：全市场港美股量化筛选+AI综合判断，由OpenClaw cron每工作日跑一次
auth.py              # 登录鉴权（从math-agent移植，共用同一套Supabase账号体系）
theme.py             # 涨跌红绿色常量，app.py/charts.py统一从这里引用
data/track_record.db # 追踪记录/持仓/搜索历史，不入库
```

## 技术栈

- 数据源：[AkShare](https://akshare.akfamily.xyz) + [BaoStock](http://baostock.com)（A股主数据源）+ 新浪财经 + 东方财富公告中心 + 财新（兜底）+ [Futu OpenAPI](https://openapi.futunn.com)（港美股实时行情，需本地/服务器跑 OpenD 网关）
- 分析：DeepSeek（`deepseek-v4-flash`），流式输出
- 前端：Streamlit（`@st.fragment` 拆分刷新粒度，并发线程池加速持仓取数）
- 账号：Supabase　|　追踪记录：SQLite（不入库）
- 部署：VPS + Nginx + systemd，GitHub Actions 自动部署

## 本地运行

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
# 在 .streamlit/secrets.toml 填入 DEEPSEEK_API_KEY / SUPABASE_URL / SUPABASE_KEY
streamlit run app.py
```

港股/美股实时行情需要另外跑一个 [Futu OpenD](https://openapi.futunn.com/futu-api-doc/quick/opend-forward.html) 网关（`127.0.0.1:11111`），没有时自动退化成延迟行情/日K/财新新闻兜底。

## 已知限制

- 东财接口有限流保护（请求间隔+重试）；akshare 的 V8 反爬引擎和 BaoStock 会话状态都加了全局锁串行化
- 宽基指数成分股用"当前涨幅最大一批股票"代理，非严格官方名单；行业主题指数用手动维护名单，按季度调整
- 港美股实时数据依赖本地 Futu OpenD 网关，所有查询统一走一个常驻 worker 线程（跨线程调用会导致 SDK 内部状态错乱卡死），进程刚起来时连接握手要几十秒到几分钟
- 只支持股票和指数，不支持基金/债券/期货/期权

## 值得一提的踩坑

- **Futu 连接和调用不在同一线程会直接卡死不返回**：十几处调用点全部踩了同一个坑，改成一个常驻 worker 线程统一排队处理，从架构上保证连接和调用永远同一线程。后续又加了看门狗线程盯心跳，真卡死时自动换代自愈。
- **AI 分析概率性返回空内容**：DeepSeek 隐藏的思考过程和正式回答共用同一个 `max_tokens` 预算，思考过程吃满预算时正式回答一个字都不剩，看起来像接口失败实际是预算问题，多个调用点因此漏排查过。
- **详情页反射型 XSS 叠加 token 写回 URL，能被用来接管账号**：股票名称直接从URL参数渲染没转义，同时登录 token 又被拼进几乎所有跳转链接——独立 Agent 审查揪出，转义修复+token 改用浏览器端 `history.replaceState` 摘除，不再触发 Streamlit rerun。
- **内部页面切换时上一页内容残留**：加载遮罩注入的 JS 内容字节不变时 Streamlit 不会重新挂载，嵌入时间戳注释强制每次都重新执行。
- **三处数据源裸调用没有异常保护**，包括 A 股详情页默认打开就看到的分时图——接口一抖整页崩掉，补上兜底后统一走已有的降级展示。
- **GitHub Actions 配置时顺带发现 math-agent 的部署密钥已经失效一个多月**：服务器迁移后忘了同步更新部署密钥，静默失败没有任何提醒。

MIT License
