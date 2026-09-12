"""Invest Agent —— 行情+财务+新闻交叉验证，不做黑箱荐股。"""

import html
import json
import os
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, wait as _futures_wait
from pathlib import Path
import pandas as pd
import streamlit as st
import streamlit.components.v1 as _cv1
from contextlib import nullcontext as _nullcontext
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from data_sources import (
    get_crypto_quotes,
    get_crypto_regime,
    get_market_closures,
    get_institution_ratings,
    get_morningstar_view,
    get_capital_distribution,
    get_short_interest,
    get_institutional_holding,
    get_insider_trades,
    get_market_rank,
    get_ipo_calendar,
    get_earnings_dates,
    get_economic_events,
    get_rating_changes,
    cn_now,
    _MULTI_INDICES,
    get_stock_kline_futu,
    get_stock_intraday_futu,
    get_stock_intraday_a,
    get_index_history,
    get_index_intraday_futu,
    get_index_intraday_a,
    get_stock_history,
    get_stock_realtime,
    get_stock_realtime_futu_batch,
    check_stock_valid,
    get_financial_abstract,
    get_stock_news,
    get_stock_notices,
    get_benchmark_history,
    get_stock_name,
    get_index_news,
    get_futu_news,
    get_market_news,
    get_hot_market_news,
    get_global_indices,
    search_stock_by_name,
    get_multi_index_snapshot,
    get_multi_index_snapshot_slow,
    load_home_map_cache,
    get_market_breadth,
    get_limit_pool,
    get_hk_famous_movers,
    get_index_top_movers,
    get_southbound_flow,
    get_us_famous_movers,
    get_hot_sectors,
    get_sector_heatmap,
    get_macro_dashboard,
    get_macro_history,
    get_sector_constituents,
    get_hstech_constituents,
    resolve_symbol_by_name,
    detect_symbol_candidates,
    get_data_source_health,
    to_cny,
    _A_FUND_NAME_MAP,
)
from analysis import (
    cross_validate, summarize_financials, summarize_news, summarize_index_news, summarize_benchmark,
    extract_verdict, analyze_index, summarize_overall, extract_score,
)
from assistant import build_context as build_assistant_context, stream_reply as stream_assistant_reply
from tracker import (
    log_analysis, get_history, get_due_for_review, record_review, get_accuracy_stats, record_overall_score,
    get_advice_accuracy, get_recent_advice_outcomes, get_advice_outcome_summary,
    extract_score_breakdown,
    get_accuracy_trend, get_daily_accuracy, add_watch_only, is_position_tracked,
    add_search_history, get_search_history, get_latest_leaderboard, get_user_overview,
    get_latest_macro_briefs,
    get_position_advice, get_positions, upsert_position, reduce_position, delete_position,
    get_closure_notice_count, bump_closure_notice_count,
    get_latest_ipo_briefs,
    get_latest_ipo_performance,
    get_latest_portfolio_advice, get_max_capital, set_max_capital,
    get_simulated_orders, get_sim_agent_runs, get_sim_virtual_cash,
    get_equity_snapshots, get_period_pnl,
    add_price_alert, get_price_alerts, delete_price_alert, set_price_alert_enabled,
)
import sim_trader
import sim_agent
from charts import (
    build_fed_rate_path_chart,
    build_candlestick, build_intraday_line, compute_stats, compute_technical_signal, compute_realtime_signal,
    build_benchmark_comparison, build_return_histogram, build_multi_comparison, build_position_donut,
    build_fed_watch_chart, build_macro_series_chart,
    build_sim_equity_curve, build_sector_treemap, build_correlation_heatmap,
    build_sim_vs_benchmark, build_ipo_open_vs_close,
)
import portfolio_risk
import sim_metrics
import ipo_calc
from auth import (
    _check_user, _register_user, _create_token, _validate_token,
    _invalidate_token, _hash_pw, _user_exists,
)
from theme import UP_COLOR, DOWN_COLOR, NEUTRAL_COLOR, OK_COLOR, BAD_COLOR

for _k in ("SUPABASE_URL", "SUPABASE_KEY", "ADVISOR_EMAIL"):
    if _k not in os.environ:
        try:
            os.environ[_k] = st.secrets[_k]
        except Exception:
            pass

# initial_sidebar_state="collapsed"：2026-09-04把侧边栏整个撤掉了（内容挪进
# "我的"分区）。不往 st.sidebar 里写东西之后，Streamlit 依然会在左上角留一个
# 展开箭头，点开是一条空白栏——一个什么都没有的死入口，比留着更让人困惑。
# 这里连同下面 CSS 里的 stSidebar/stSidebarCollapsedControl 一起彻底隐掉。
st.set_page_config(page_title="Invest Agent", layout="wide", initial_sidebar_state="collapsed")

# ── 主题（移动端适配 + 涨跌红绿色统一）────────────────────────────────────────
# 这里原来还有一版深色模式，用户实测反馈"按了跟没按一样，很烦"——撤掉了，
# 只留CSS变量本身（数值固定为浅色，不再有深色分支），移动端适配和涨跌色号
# 统一这两块跟深色模式无关，继续保留。
_FA_BASE_CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

:root {
    /* 画布：只有三层——底、卡面、更浅的填充块 */
    --fa-bg:        #FAFAFB;
    --fa-surface:   #FFFFFF;
    --fa-fill:      #F3F4F6;
    --fa-border:    #EAEAEF;
    --fa-border-2:  #DBDCE3;

    /* 文字：四级，够用且不会滥用 */
    --fa-text:      #17181C;
    --fa-text-2:    #494C55;
    --fa-muted:     #82858E;
    --fa-faint:     #A8ABB3;

    /* 界面强调色刻意用墨色而不是彩色。整个页面唯一允许出现的饱和色是
       涨跌红绿——这是这类界面显专业的关键，一旦品牌色也是红的，涨跌就
       不再是页面上最醒目的信息了。 */
    --fa-ink:       #17181C;

    --fa-radius:    10px;
    --fa-radius-sm: 7px;
}

/* ── 排版基线 ─────────────────────────────────────────────────────────── */
html, body, .stApp, [data-testid="stAppViewContainer"] {
    background: var(--fa-bg) !important;
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'PingFang SC',
                 'Hiragino Sans GB', 'Microsoft YaHei', system-ui, sans-serif !important;
    color: var(--fa-text);
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
}
/* 等宽数字。财务界面里数字必须能上下对齐，比例数字会让一列价格看着参差
   不齐——这一条是这类页面"看着专业"最省力也最容易被忽略的一处。 */
.stApp, .stApp * { font-variant-numeric: tabular-nums; font-feature-settings: 'tnum' 1; }

[data-testid="stMain"], [data-testid="stMainBlockContainer"],
section.main, .main, .block-container,
[data-testid="stBottom"], .stBottom, [data-testid="stBottomBlockContainer"], footer {
    background: var(--fa-bg) !important;
}
header[data-testid="stHeader"] { background: transparent !important; box-shadow: none !important; height: 0 !important; }

/* Streamlit自带的右上角工具区（2026-09-11前端审计）：
   - stStatusWidget 是"Running… / Stop"那个小人+按钮。行情页每3秒刷新一次
     fragment，它就跟着闪一次，页面右上角一直在抖。
   - MainMenu 是那个 ⋮ 菜单，点开是"Made with Streamlit v1.59.2"这类给
     开发者看的东西，对访问者没有意义，而且展开时会压在内容上面。
   两个都只做视觉隐藏、不用 display:none——首屏加载遮罩那段JS（见上面
   _fa_loader）靠读 stStatusWidget 的 textContent 判断"跑完了没有"，
   把元素从渲染树里摘掉是能读到，但留着更保险，也免得以后谁改了那段
   探测逻辑又踩一次。 */
[data-testid="stStatusWidget"] {
    opacity: 0 !important; pointer-events: none !important;
    position: absolute !important; width: 1px !important; height: 1px !important;
    overflow: hidden !important;
}
#MainMenu, [data-testid="stMainMenu"], [data-testid="stToolbarActions"] { display: none !important; }

/* 顶部导航吸顶（2026-09-11前端审计）：行情页内容很长，滚到底部想切分区
   得往回滚很远。让导航那一条钉在顶部，背景用页面底色盖住下面滚过去的
   内容，不然文字会透上来叠在一起。z-index 取 90——低于右下角AI浮标
   (9999)，不跟它抢层级。 */
.st-key-fa_nav {
    position: sticky !important; top: 0 !important; z-index: 90 !important;
    background: var(--fa-bg) !important;
    padding-top: 6px !important;
}

/* 内容收窄居中。原来是整屏铺满，超宽屏上一行数字能拉到两千多像素，
   眼睛要横扫过去才读得完；收到1240再给足左右留白，行长回到舒适区间。
   底部 120px 是给右下角AI浮标让位：浮标 56px 高、距底 26px，实测会盖住
   最后一行的数字（中信金属的 -9.97%、模拟盘持仓的盈亏金额都被挡过）。 */
[data-testid="stMainBlockContainer"] {
    max-width: 1240px !important;
    padding: 26px 40px 120px !important;
}
@media (max-width: 900px) {
    [data-testid="stMainBlockContainer"] { padding: 18px 18px 108px !important; }
}

[data-testid="stHorizontalBlock"], [data-testid="stColumn"], [data-testid="stElementContainer"] {
    background: transparent !important;
}

/* ── 标题层级 ─────────────────────────────────────────────────────────── */
[data-testid="stMarkdownContainer"] h1 { font-size: 1.6rem; font-weight: 650; letter-spacing: -0.018em; color: var(--fa-text); margin: 0 0 14px; }
[data-testid="stMarkdownContainer"] h2 { font-size: 1.22rem; font-weight: 620; letter-spacing: -0.012em; color: var(--fa-text); margin: 30px 0 12px; }
[data-testid="stMarkdownContainer"] h3 { font-size: 1.02rem; font-weight: 600; letter-spacing: -0.008em; color: var(--fa-text); margin: 24px 0 10px; }
[data-testid="stMarkdownContainer"] p { color: var(--fa-text-2) !important; line-height: 1.68; }
/* 项目里所有小节标题都是 st.markdown("**标题**")，也就是一个只含<strong>的
   <p>。
   2026-09-12改（前端审计第14条"层级太平"）：上一版把它渲染成 0.825rem 的
   灰色大写眉标，出发点是"安静"，但实测的结果是标题和正文分不出来——审计
   原话"板块标题（宏观议题/投研观察排行榜）是14px的灰字，和正文分不开"。
   安静和没有层次是两回事：一页里十几个板块，如果标题不比正文重，读者就
   只能一行行读过去，没法先扫结构再决定看哪块。
   改成 1.1rem / 600 字重 / 正文色，并去掉 uppercase（对中文无效，对夹在
   中间的英文反而会把"IPO"之外的普通词也拉成全大写）。字距从 +0.05em 收到
   -0.01em：大字号配正字距会显得松垮，标题本来就该比正文紧。 */
[data-testid="stMarkdownContainer"] p > strong:only-child {
    display: block; font-size: 1.1rem; font-weight: 600; letter-spacing: -0.01em;
    color: var(--fa-text); margin: 34px 0 10px;
}
/* 数字统一用等宽数字（前端审计第14条最后一行）。比例字体里 1 比 0 窄一大截，
   上下两行价格的小数点对不齐，一列数字扫下来是锯齿状的。tabular-nums 只改
   数字的字形宽度，不影响中英文排版，所以可以全局开。 */
html, body, [data-testid="stAppViewContainer"] { font-variant-numeric: tabular-nums; }
/* 只给正文里真正的行内链接加下划线，并明确排除各种整卡可点的<a>。
   项目里推荐股/指数/板块/持仓卡片都是拿一个<a class="*-card-link">把若干
   <div>包起来的，而HTML规范不允许<p>里出现块级元素——浏览器解析到<div>
   时会提前把<p>闭掉，于是留下一个高度1px、内容为空的<a>孤儿节点。给它
   加border-bottom就会在卡片里凭空多出一条横线（实测在推荐股和指数卡片
   上各出现过一次）。按class排除掉，比靠结构选择器去猜稳。 */
/* 链接默认不画下划线，只在悬停时出现。页面上大量的可点内容是新闻标题和
   卡片，给它们全部常驻下划线会很吵；而且这些标题有的落在<p>里、有的不在，
   常驻下划线会变成"有的有有的没有"的不一致。默认干净、悬停给反馈更稳。 */
[data-testid="stMarkdownContainer"] a { text-decoration: none; border-bottom: none; }
[data-testid="stMarkdownContainer"] p > a:not([class*="card-link"]):hover {
    border-bottom: 1px solid var(--fa-border-2);
}

[data-testid="stCaptionContainer"], [data-testid="stCaptionContainer"] * {
    color: var(--fa-muted) !important; font-size: 0.79rem !important; line-height: 1.6 !important;
}
[data-testid="stWidgetLabel"] p { color: var(--fa-text-2) !important; font-size: 0.82rem !important; font-weight: 500 !important; }

/* ── 分隔 ─────────────────────────────────────────────────────────────── */
/* 分隔线上下留白 30px→46px（2026-09-12，用户反馈首页"栏目之间有点挤"）。
   这条线是首页各栏目之间唯一的分界，留白比线本身更能说明"这里换了一块"——
   线细、留白窄的时候，两个栏目在视觉上还是连着的，得靠读文字才分得开。 */
hr, [data-testid="stDivider"] hr { border: none !important; border-top: 1px solid var(--fa-border) !important; margin: 46px 0 !important; }

/* ── 顶部导航（st.radio 伪装成下划线标签页）────────────────────────────
   原来是红底白字的胶囊，五个并排像一排按钮，很难不显得像后台管理系统。
   改成印刷品式的下划线标签：不选中只是灰字，选中是墨色字+一条2px的墨线。 */
/* 这一组的父级是flex列容器且不拉伸，radio会缩成内容宽度，下划线也就只有
   一半宽。把这条链上的容器都撑满，那条分隔线才跟内容区左右对齐。 */
.st-key-fa_nav { align-items: stretch !important; }
.st-key-fa_nav [data-testid="stElementContainer"],
.st-key-fa_nav [data-testid="stRadio"],
.st-key-fa_nav [data-testid="stRadioGroup"] { width: 100% !important; }
.st-key-fa_nav [role="radiogroup"] {
    width: 100% !important; gap: 0 !important; align-items: flex-end !important;
    border-bottom: 1px solid var(--fa-border);
}
.st-key-fa_nav [data-testid="stRadioOption"],
.st-key-fa_nav [data-testid="stRadioOption"]:hover,
.st-key-fa_nav [data-testid="stRadioOption"]:has(input:checked) {
    padding: 0 0 12px !important; margin: 0 30px 0 0 !important;
    /* background 必须在这里显式写成透明：下面通用的行内radio规则会把选中项
       刷成墨色实底，而那条规则的选择器不带 .st-key-fa_nav，特异性虽然更低
       却是唯一声明了 background 的一条，不显式覆盖就会漏进来，导航标签变成
       一个黑方块。踩过一次。 */
    background: transparent !important; border: none !important;
    border-bottom: 2px solid transparent !important; border-radius: 0 !important;
    transition: border-color .16s ease;
}
/* 干掉单选圆点。Streamlit的结构是
   label > span>input + div > div > [圆点div, stMarkdownContainer]，
   圆点那层没有稳定的testid，只能反选"不是文字容器的那个兄弟"。 */
.st-key-fa_nav [data-testid="stRadioOption"] > div > div > div:not([data-testid="stMarkdownContainer"]) {
    display: none !important;
}
.st-key-fa_nav [data-testid="stRadioOption"] p {
    font-size: 0.92rem !important; font-weight: 500 !important; color: var(--fa-muted) !important;
    letter-spacing: 0.005em; transition: color .16s ease;
}
.st-key-fa_nav [data-testid="stRadioOption"]:hover p { color: var(--fa-text-2) !important; }
.st-key-fa_nav [data-testid="stRadioOption"]:has(input:checked) { border-bottom-color: var(--fa-ink) !important; }
.st-key-fa_nav [data-testid="stRadioOption"]:has(input:checked) p { color: var(--fa-text) !important; font-weight: 600 !important; }

/* 其余位置的横向 radio（市场切换、K线周期、走势范围）统一成安静的分段控件：
   去掉原生小圆点，做成下划线标签页，跟顶部导航同一套长相。
   2026-09-04改：原来选中态是"墨色实底+白字"的胶囊，页面上就是一块突兀的
   黑方块，跟这套灰白克制的底子撞得厉害。整站"选择一项"这件事本来就只该有
   一种视觉语言，而导航已经定了下划线这一种，次级选择器（市场、K线周期、
   区间）没有理由再造第二种更重的样式——分量还压过了导航本身。
   改成同款下划线：默认灰字无底色，选中只是字变墨色加粗、底下一条线。 */
[data-testid="stRadio"] [role="radiogroup"] { gap: 2px; align-items: stretch; }
[data-testid="stRadioOption"] {
    padding: 5px 2px !important; margin: 0 14px 0 0 !important;
    background: transparent !important;
    border: 0 !important; border-bottom: 2px solid transparent !important;
    border-radius: 0 !important;
    transition: border-color .14s ease, color .14s ease;
}
[data-testid="stRadioOption"] > div > div > div:not([data-testid="stMarkdownContainer"]) { display: none !important; }
[data-testid="stRadioOption"] p { font-size: 0.84rem !important; font-weight: 500 !important; color: var(--fa-muted) !important; }
[data-testid="stRadioOption"]:hover { background: transparent !important; }
[data-testid="stRadioOption"]:hover p { color: var(--fa-text-2) !important; }
[data-testid="stRadioOption"]:has(input:checked) {
    background: transparent !important; border-bottom-color: var(--fa-ink) !important;
}
[data-testid="stRadioOption"]:has(input:checked) p { color: var(--fa-text) !important; font-weight: 600 !important; }
div[data-testid="stButtonGroup"] button, div[data-testid="stButtonGroup"] [role="radio"] {
    background: var(--fa-surface) !important; border: 1px solid var(--fa-border) !important;
    color: var(--fa-muted) !important; border-radius: var(--fa-radius-sm) !important;
    font-size: 0.83rem !important; font-weight: 500 !important;
}
div[data-testid="stButtonGroup"] button[aria-checked="true"], div[data-testid="stButtonGroup"] [aria-checked="true"] {
    background: var(--fa-ink) !important; border-color: var(--fa-ink) !important; color: #fff !important;
}
div[data-testid="stButtonGroup"] p, div[data-testid="stButtonGroup"] span { color: inherit !important; }

/* ── 卡片与容器 ───────────────────────────────────────────────────────── */
/* 2026-09-05用户定的全局规则："现在我们项目里不要出现显眼的框框，保持透明统一"。
   这条改成在源头上生效，而不是逐个调用点去改：Streamlit 的 st.container(border=True)
   和 st.expander 都会画一个白底加完整边框的盒子，页面上一堆这种盒子摞在一起，
   跟本项目"透明底加一条发丝线"的基调是两套语言。之前是发现一处改一处（指数行、
   核心股、板块、内部人交易…），改到第五处就该明白这是个全局问题——在这里统一
   成透明底加一条底部发丝线，以后新写的代码不用再单独处理。
   分区之间的层次靠留白和标题字重表达，不靠画框。 */
[data-testid="stVerticalBlockBorderWrapper"] {
    background: transparent !important;
    border: none !important;
    border-radius: 0 !important;
}
/* 真正画边框的是内层的 stVerticalBlock，不是外面这层 BorderWrapper——
   第一版只改了 BorderWrapper，页面上白框纹丝不动，扒 DOM 才看到
   `1px solid rgba(23,24,28,.2)` 挂在内层。
   用 :not([class*="st-key-"]) 把这条限制在"Streamlit 自己画的框"上：
   项目里那些扁平行是 st.container(key=...) 建的，带 st-key 类、各自
   已经定义了想要的底部发丝线，不能被这条一并抹掉。 */
[data-testid="stVerticalBlock"]:not([class*="st-key-"]) {
    border: none !important;
    border-radius: 0 !important;
    background: transparent !important;
}
/* 无边框之后靠一条发丝线分隔相邻区块，层次不靠画框靠留白。 */
[data-testid="stVerticalBlockBorderWrapper"] > [data-testid="stLayoutWrapper"] > [data-testid="stVerticalBlock"]:not([class*="st-key-"]) {
    border-bottom: 1px solid var(--fa-border) !important;
    padding: 2px 0 12px !important;
}
/* 边框其实挂在内层的<details>上，不是 stExpander 这一层，套外层是没用的。 */
[data-testid="stExpander"] details {
    background: transparent !important;
    border: none !important;
    border-bottom: 1px solid var(--fa-border) !important;
    border-radius: 0 !important; box-shadow: none !important;
}
[data-testid="stExpander"] summary { padding: 11px 2px !important; font-size: 0.87rem !important; border-radius: var(--fa-radius) !important; }
[data-testid="stExpander"] summary:hover { background: var(--fa-fill) !important; }
[data-testid="stExpander"] summary p { font-weight: 500 !important; color: var(--fa-text-2) !important; }

/* ── 数字指标 ─────────────────────────────────────────────────────────── */
[data-testid="stMetric"] { background: transparent !important; padding: 0 !important; }
[data-testid="stMetricLabel"] p {
    font-size: 0.775rem !important; font-weight: 500 !important; color: var(--fa-muted) !important;
    letter-spacing: 0.035em;
}
[data-testid="stMetricValue"] {
    /* 2026-09-12：从 1.85rem 收到 1.5rem。实机看个股详情页，"最高/最低/今开"
       这三个次要参考数字几乎跟顶部主价格一样大，主次完全拉不开，而且三行
       大字把K线推到了首屏之外。1.5rem 仍然是明确的"指标数字"体量，但不再
       跟页面主角抢戏，各页面的垂直占用也跟着收了一截。 */
    font-size: 1.5rem !important; font-weight: 600 !important; letter-spacing: -0.028em !important;
    color: var(--fa-text) !important; line-height: 1.24 !important;
}
/* Streamlit 新版给 delta 加了一个带底色的圆角小胶囊，粉底/浅绿底在这套
   近乎无色的界面里显得很突兀，而且底色本身不传递任何额外信息——箭头和
   正负号已经说清楚方向了。去掉底色只留文字。 */
[data-testid="stMetricDelta"] {
    background: transparent !important; padding: 0 !important; margin-top: 2px !important;
    font-size: 0.82rem !important; font-weight: 500 !important;
}
[data-testid="stMetricDelta"] svg { width: 14px !important; height: 14px !important; }

/* ── 按钮 ─────────────────────────────────────────────────────────────── */
/* 按钮系统。全站只有三种按钮，各自职责清楚，不再每个页面自己写一套：
   次要（默认）= 白底发丝边框；主要 = 墨色实底；安静（tertiary）= 无边框弱化，
   用在图标按钮和返回这类不该抢戏的位置。 */
/* 次要按钮：透明底 + 发丝描边，不是"白盒子"。
   页面底色是 #FAFAFB，按钮如果填纯白，就会从底上浮出来一块，再加一圈边框，
   看着就是一个标准的表单控件方盒——这正是用户说的"大白框"。改成背景透明、
   直接吃页面底色，只留一条极淡的描边勾出可点范围；悬停时才填一块浅灰给出
   反馈。同一颗按钮，从"一块白方框"变成"一个轮廓"，页面立刻安静下来。 */
/* 下面这一组选择器是整套按钮系统的唯一入口。Streamlit 的按钮不止 st.button
   一种：表单提交键、弹层触发键各自是独立的 testid，只写 .stButton button 会
   漏掉它们，于是页面上就会出现"大部分按钮是新样式、个别还是旧样式"的割裂。
   全部并到同一组选择器上，样式只此一份，以后加新按钮也自动继承。 */
/* 2026-09-12：连那条描边也去掉。用户第二次提这件事——"项目里面还是存在按键
   的外框，这个我之前说过很丑，做成跟我们现在项目一样透明无框的那种风格"。
   上一版只把底色改透明、留了 1px 描边，页面上仍然是一个个小方框。
   现在改成纯文字按钮：常态只有文字，悬停时才浮出一块浅灰底作为可点反馈。
   为了不让它退化成"看不出能点"，文字用正文墨色 + 500 字重（比周围的说明
   文字重一档），左右 padding 保留，让悬停时那块底色是个规整的圆角块。 */
.stButton button,
[data-testid="stFormSubmitButton"] button,
[data-testid="stPopover"] button {
    background: transparent !important; border: none !important;
    color: var(--fa-text) !important; border-radius: var(--fa-radius-sm) !important;
    font-size: 0.845rem !important; font-weight: 500 !important; padding: 6px 12px !important;
    box-shadow: none !important;
    transition: background .15s ease, color .15s ease;
}
.stButton button:hover,
[data-testid="stFormSubmitButton"] button:hover,
[data-testid="stPopover"] button:hover {
    background: var(--fa-fill) !important; color: var(--fa-text) !important;
}
.stButton button:active,
[data-testid="stFormSubmitButton"] button:active { background: #E9EAEE !important; }
.stButton button:focus, .stButton button:focus-visible,
[data-testid="stFormSubmitButton"] button:focus,
[data-testid="stFormSubmitButton"] button:focus-visible,
[data-testid="stPopover"] button:focus,
[data-testid="stPopover"] button:focus-visible { box-shadow: none !important; outline: none !important; }
.stButton button:disabled,
[data-testid="stFormSubmitButton"] button:disabled {
    background: transparent !important; border: none !important;
    color: var(--fa-faint) !important;
}

/* "展开/更多/收起"这类按钮不是动作，是一个揭示更多内容的入口。它们大多是
   整行宽度，画成带边框的大方块时会在页面中间横出一道很重的横条。改成没有
   边框、没有底色的居中弱化文字，上面压一条发丝分割线——跟编辑类网站的
   "加载更多"是同一种处理，存在感刚好够点，又不会把版面切断。 */
/* 容器本身也要撑满一行。按钮上的 width:100% 撑的是它的父容器，而
   Streamlit 给按钮的外层容器默认是收缩宽度的——只给按钮加宽度，撑开的
   仍然是一个本来就很窄的盒子，文字看着还是贴在左边（"更多板块"就是这样，
   上面那条发丝线也只有短短一截）。 */
[class*="st-key-_detail_expand_btn"],
[class*="st-key-_idx_expand_btn"],
[class*="st-key-_ai_sim_runs_more"],
[class*="st-key-_ai_sim_orders_more"],
[class*="st-key-_more_limit_pool"],
[class*="st-key-_sectors_more_btn"],
[class*="st-key-_sectors_collapse_btn"],
[class*="st-key-_movers_collapse_btn"],
[class*="st-key-_home_news_more"],
[class*="st-key-_home_news_collapse"],
[class*="st-key-_movers_expand_btn"] {
    width: 100% !important;
}

[class*="st-key-_detail_expand_btn"] button,
[class*="st-key-_idx_expand_btn"] button,
[class*="st-key-_ai_sim_runs_more"] button,
[class*="st-key-_ai_sim_orders_more"] button,
[class*="st-key-_more_limit_pool"] button,
[class*="st-key-_sectors_more_btn"] button,
[class*="st-key-_sectors_collapse_btn"] button,
[class*="st-key-_movers_collapse_btn"] button,
[class*="st-key-_home_news_more"] button,
[class*="st-key-_home_news_collapse"] button,
[class*="st-key-_movers_expand_btn"] button {
    background: transparent !important; border: none !important;
    border-top: 1px solid var(--fa-border) !important;
    border-radius: 0 !important; color: var(--fa-muted) !important;
    font-size: 0.82rem !important; font-weight: 500 !important;
    padding: 13px 8px 6px !important; letter-spacing: .02em;
    width: 100% !important;          /* 撑满整行，文字才能真正居中 */
}
[class*="st-key-_detail_expand_btn"] button:hover,
[class*="st-key-_idx_expand_btn"] button:hover,
[class*="st-key-_ai_sim_runs_more"] button:hover,
[class*="st-key-_ai_sim_orders_more"] button:hover,
[class*="st-key-_more_limit_pool"] button:hover,
[class*="st-key-_sectors_more_btn"] button:hover,
[class*="st-key-_sectors_collapse_btn"] button:hover,
[class*="st-key-_movers_collapse_btn"] button:hover,
[class*="st-key-_home_news_more"] button:hover,
[class*="st-key-_home_news_collapse"] button:hover,
[class*="st-key-_movers_expand_btn"] button:hover {
    background: transparent !important; color: var(--fa-text) !important;
}

/* 主要动作：唯一填实底的按钮。一屏里只应该有一个，让"这一步该点哪"没有歧义。 */
/* 2026-09实测修复：按钮文字之前肉眼几乎看不见——登录/注册这类主要按钮
   墨色实底配白字，但白字只设在了<button>本身，按钮内层文字实际包在
   [data-testid="stMarkdownContainer"] p里，页面上游有一条
   `[data-testid="stMarkdownContainer"] p { color: var(--fa-text-2) !important }`
   全局规则，直接挂在这层<p>上，子元素的直接样式天然盖过父级<button>上
   继承来的白色，不管父级选择器特异度多高。必须连着内层p/div/span一起
   显式设成白色，只设按钮本身不够。 */
.stButton button[kind="primary"],
.stButton button[data-testid="stBaseButton-primary"],
[data-testid="stFormSubmitButton"] button[kind="primary"],
[data-testid="stFormSubmitButton"] button[data-testid="stBaseButton-primaryFormSubmit"] {
    background: var(--fa-ink) !important; border-color: var(--fa-ink) !important; color: #fff !important;
}
.stButton button[kind="primary"] p,
.stButton button[kind="primary"] div,
.stButton button[kind="primary"] span,
.stButton button[data-testid="stBaseButton-primary"] p,
.stButton button[data-testid="stBaseButton-primary"] div,
.stButton button[data-testid="stBaseButton-primary"] span,
[data-testid="stFormSubmitButton"] button[kind="primary"] p,
[data-testid="stFormSubmitButton"] button[kind="primary"] div,
[data-testid="stFormSubmitButton"] button[kind="primary"] span,
[data-testid="stFormSubmitButton"] button[data-testid="stBaseButton-primaryFormSubmit"] p,
[data-testid="stFormSubmitButton"] button[data-testid="stBaseButton-primaryFormSubmit"] div,
[data-testid="stFormSubmitButton"] button[data-testid="stBaseButton-primaryFormSubmit"] span { color: #fff !important; }
.stButton button[kind="primary"]:hover,
.stButton button[data-testid="stBaseButton-primary"]:hover,
[data-testid="stFormSubmitButton"] button[kind="primary"]:hover,
[data-testid="stFormSubmitButton"] button[data-testid="stBaseButton-primaryFormSubmit"]:hover {
    background: #000 !important; border-color: #000 !important; color: #fff !important;
}
/* 安静按钮：默认几乎看不见，悬停才浮出一块底。项目里的图标按钮（搜索/添加/
   对比/删除/返回）全部用它，不该在页面上摆一圈边框抢注意力。 */
/* 安静档：连描边都没有，只在悬停时浮出一块底。图标按钮和返回键走这一档。
   这一档默认色(--fa-muted)跟上面那条全局p规则(--fa-text-2)刚好都是灰调、
   肉眼分不出明显差异，实测没有"看不见"的问题，但hover态要变成--fa-text
   （比--fa-text-2更深），同样会被子级p盖掉，这里一并显式补上，不能只
   设按钮本身。 */
.stButton button[kind="tertiary"], .stButton button[data-testid="stBaseButton-tertiary"] {
    background: transparent !important; border: 1px solid transparent !important;
    color: var(--fa-muted) !important;
}
.stButton button[kind="tertiary"]:hover, .stButton button[data-testid="stBaseButton-tertiary"]:hover {
    background: var(--fa-fill) !important; border-color: transparent !important; color: var(--fa-text) !important;
}
.stButton button[kind="tertiary"]:hover p,
.stButton button[kind="tertiary"]:hover div,
.stButton button[kind="tertiary"]:hover span,
.stButton button[data-testid="stBaseButton-tertiary"]:hover p,
.stButton button[data-testid="stBaseButton-tertiary"]:hover div,
.stButton button[data-testid="stBaseButton-tertiary"]:hover span { color: var(--fa-text) !important; }

/* 宏观议题条目。跟站内其它列表同一套：发丝线分隔、折叠框无边框。 */
[class*="st-key-macro_"] {
    border: none !important; background: transparent !important;
    border-bottom: 1px solid var(--fa-border) !important;
    border-radius: 0 !important; padding: 10px 2px 4px !important;
}
[class*="st-key-macro_"] [data-testid="stExpander"] details {
    border: none !important; background: transparent !important; border-radius: 0 !important;
}
[class*="st-key-macro_"] [data-testid="stExpander"] summary {
    padding: 2px 0 !important; background: transparent !important;
}
[class*="st-key-macro_"] [data-testid="stExpander"] summary:hover { background: transparent !important; }
[class*="st-key-macro_"] [data-testid="stExpander"] summary p {
    font-size: 0.76rem !important; color: var(--fa-faint) !important; font-weight: 400 !important;
}
[class*="st-key-macro_"] [data-testid="stExpander"] summary:hover p { color: var(--fa-muted) !important; }
[class*="st-key-macro_"] [data-testid="stElementContainer"] { margin-bottom: 0 !important; }

/* AI每次决策记录。一屏五到三十条，每条都是一个带边框的白盒子时，方框本身
   就成了这一段最主要的视觉噪声。压成跟全站其它列表一样的发丝线分隔行。 */
[class*="st-key-sim_run_"] [data-testid="stExpander"] details {
    border: none !important; background: transparent !important; border-radius: 0 !important;
    border-bottom: 1px solid var(--fa-border) !important;
}
[class*="st-key-sim_run_"] [data-testid="stExpander"] summary {
    padding: 10px 0 !important; background: transparent !important; border-radius: 0 !important;
}
[class*="st-key-sim_run_"] [data-testid="stExpander"] summary:hover { background: transparent !important; }
[class*="st-key-sim_run_"] [data-testid="stExpander"] summary p {
    font-size: 0.84rem !important; color: var(--fa-text-2) !important; font-weight: 400 !important;
}
[class*="st-key-sim_run_"] [data-testid="stExpander"] summary:hover p { color: var(--fa-text) !important; }
[class*="st-key-sim_run_"] [data-testid="stElementContainer"] { margin-bottom: 0 !important; }

/* "我的"页底部"关于"里的两个折叠面板。默认是白底圆角带框的盒子，跟同一页
   上面那些发丝线分隔的行（最近搜索那几行）完全不是一套，在页尾突兀地冒出
   两个白盒子。这里把它们压成跟上面一模一样的行：无边框无底色、下面一条
   发丝线、标签字号字色跟"最近搜索"那几行对齐，点开才展开正文。 */
[class*="st-key-my_about_"] [data-testid="stExpander"] details {
    border: none !important; background: transparent !important; border-radius: 0 !important;
    border-bottom: 1px solid var(--fa-border) !important;
}
[class*="st-key-my_about_"] [data-testid="stExpander"] summary {
    padding: 9px 0 !important; background: transparent !important; border-radius: 0 !important;
}
[class*="st-key-my_about_"] [data-testid="stExpander"] summary:hover { background: transparent !important; }
[class*="st-key-my_about_"] [data-testid="stExpander"] summary p {
    font-size: 0.88rem !important; color: var(--fa-text-2) !important; font-weight: 400 !important;
}
[class*="st-key-my_about_"] [data-testid="stExpander"] summary:hover p { color: var(--fa-text) !important; }
[class*="st-key-my_about_"] [data-testid="stElementContainer"] { margin-bottom: 0 !important; }

/* 首页推荐股排行榜的条目。跟持仓/自选列表同一套：去掉卡片边框、发丝线分隔，
   行内那个"基本面/技术面/价格位置"折叠框也去掉边框和底色。 */
[class*="st-key-lb_row_"] {
    border: none !important; background: transparent !important;
    border-bottom: 1px solid var(--fa-border) !important;
    border-radius: 0 !important; padding: 16px 2px 10px !important;
}
[class*="st-key-lb_row_"] [data-testid="stExpander"] details {
    border: none !important; background: transparent !important; border-radius: 0 !important;
}
[class*="st-key-lb_row_"] [data-testid="stExpander"] summary {
    padding: 4px 0 0 !important; background: transparent !important;
}
[class*="st-key-lb_row_"] [data-testid="stExpander"] summary:hover { background: transparent !important; }
[class*="st-key-lb_row_"] [data-testid="stExpander"] summary p {
    font-size: 0.78rem !important; color: var(--fa-faint) !important; font-weight: 400 !important;
}
[class*="st-key-lb_row_"] [data-testid="stExpander"] summary:hover p { color: var(--fa-muted) !important; }

/* 持仓/自选列表的行。去掉卡片边框、改成发丝分隔的平铺行；行内那个
   "AI持仓判断"折叠框也一并去掉边框和底色，变成一行安静的可展开文字。
   目标是把"卡片里套卡片"压成一张干净的行情列表。 */
/* 行情页的指数行。跟 pos_row_ 完全同一套扁平样式：透明底、只留一条底部
   发丝线。单独写一条而不是并进上面那个选择器组，是因为指数行没有展开区，
   下面那些针对 stExpander 的规则对它没有意义，混在一起反而看不出哪条是
   给谁的。 */
/* 涨跌停池 / 港美股核心股 / 指数成分股的行，跟 pos_row_、idx_row_ 同一套扁平样式。 */
[class*="st-key-mv_row_"] {
    border: none !important; background: transparent !important;
    border-bottom: 1px solid var(--fa-border) !important;
    border-radius: 0 !important; padding: 10px 2px !important;
    gap: 0 !important;
}
[class*="st-key-mv_row_"] [data-testid="stElementContainer"] { margin-bottom: 0 !important; }
[class*="st-key-mv_row_"]:hover { background: rgba(23,24,28,0.015) !important; }

/* 热门板块行，跟本页其余列表同一套扁平样式。 */
[class*="st-key-sector_row_"] {
    border: none !important; background: transparent !important;
    border-bottom: 1px solid var(--fa-border) !important;
    border-radius: 0 !important; padding: 10px 2px !important;
    gap: 0 !important;
}
[class*="st-key-sector_row_"] [data-testid="stElementContainer"] { margin-bottom: 0 !important; }
[class*="st-key-sector_row_"]:hover { background: rgba(23,24,28,0.015) !important; }

[class*="st-key-idx_row_"] {
    border: none !important; background: transparent !important;
    border-bottom: 1px solid var(--fa-border) !important;
    border-radius: 0 !important; padding: 10px 2px !important;
    gap: 0 !important;
}
[class*="st-key-idx_row_"] [data-testid="stElementContainer"] { margin-bottom: 0 !important; }
[class*="st-key-idx_row_"]:hover { background: rgba(23,24,28,0.015) !important; }

[class*="st-key-pos_row_"] {
    border: none !important; background: transparent !important;
    border-bottom: 1px solid var(--fa-border) !important;
    border-radius: 0 !important; padding: 4px 2px 2px !important;
    gap: 0 !important;
}
/* 行内那条"观望 · 09-03"紧贴主行，不要另起一大段。Streamlit 默认给每个
   元素容器留了不小的竖向间距，一行里有两个块就会撑出很高的行高，二十来行
   叠起来整页就散了。 */
[class*="st-key-pos_row_"] [data-testid="stElementContainer"] { margin-bottom: 0 !important; }
[class*="st-key-pos_row_"] [data-testid="stExpander"] { margin-top: -6px !important; }
[class*="st-key-pos_row_"]:hover { background: rgba(23,24,28,0.015) !important; }
[class*="st-key-pos_row_"] [data-testid="stExpander"] details {
    border: none !important; background: transparent !important; border-radius: 0 !important;
}
[class*="st-key-pos_row_"] [data-testid="stExpander"] summary {
    padding: 2px 0 0 !important; background: transparent !important;
}
[class*="st-key-pos_row_"] [data-testid="stExpander"] summary:hover { background: transparent !important; }
[class*="st-key-pos_row_"] [data-testid="stExpander"] summary p {
    font-size: 0.76rem !important; color: var(--fa-faint) !important; font-weight: 400 !important;
}
[class*="st-key-pos_row_"] [data-testid="stExpander"] summary:hover p { color: var(--fa-muted) !important; }
/* 删除键平时隐去，指到这一行才浮出来——它是破坏性操作，不需要二十个红叉
   常驻在列表右侧。 */
[class*="st-key-pos_row_"] [class*="st-key-pos_del_"] button { opacity: 0; transition: opacity .15s ease; }
[class*="st-key-pos_row_"]:hover [class*="st-key-pos_del_"] button { opacity: 1; }

/* 图标按钮统一规格：36px 正圆、图标 1.12rem。原来各处自己写死 44px + 1.6rem，
   在这套克制的版式里显得又大又重，而且持仓页 44px、删除键 36px，同一个页面
   两种尺寸。这里收口成一份，各处不再重复定义。 */
[class*="st-key-pos_search_icon"] button, [class*="st-key-pos_compare_icon"] button,
[class*="st-key-pos_add_icon"] button, [class*="st-key-watch_search_icon"] button,
[class*="st-key-watch_add_icon"] button, [class*="st-key-pos_del_"] button,
[class*="st-key-detail_back_"] button, [class*="st-key-idx_back_"] button,
[class*="st-key-sector_back_"] button {
    height: 36px !important; min-height: 36px !important;
    width: 36px !important; min-width: 36px !important;
    padding: 0 !important; border-radius: 50% !important;
    display: flex !important; align-items: center !important; justify-content: center !important;
}
[class*="st-key-pos_search_icon"] span[data-testid="stIconMaterial"],
[class*="st-key-pos_compare_icon"] span[data-testid="stIconMaterial"],
[class*="st-key-pos_add_icon"] span[data-testid="stIconMaterial"],
[class*="st-key-watch_search_icon"] span[data-testid="stIconMaterial"],
[class*="st-key-watch_add_icon"] span[data-testid="stIconMaterial"],
[class*="st-key-pos_del_"] span[data-testid="stIconMaterial"],
[class*="st-key-detail_back_"] span[data-testid="stIconMaterial"],
[class*="st-key-idx_back_"] span[data-testid="stIconMaterial"],
[class*="st-key-sector_back_"] span[data-testid="stIconMaterial"] {
    font-size: 1.12rem !important;
}
/* 删除/取消关注是破坏性操作，平时保持中性，悬停才透出跌色作为警示——
   一上来就画成红色会让整个列表看着像满屏警告。 */
[class*="st-key-pos_del_"] button:hover span[data-testid="stIconMaterial"] { color: #D0342C !important; }

/* ── 输入 ─────────────────────────────────────────────────────────────── */
[data-testid="stTextInput"] input, [data-testid="stNumberInput"] input, [data-testid="stTextArea"] textarea {
    background: var(--fa-surface) !important; border-radius: var(--fa-radius-sm) !important;
    border-color: var(--fa-border-2) !important; color: var(--fa-text) !important; font-size: 0.88rem !important;
}
[data-testid="stTextInput"] input:focus, [data-testid="stNumberInput"] input:focus {
    border-color: var(--fa-ink) !important; box-shadow: none !important;
}
[data-baseweb="input"], [data-baseweb="base-input"] { background: var(--fa-surface) !important; border-radius: var(--fa-radius-sm) !important; }

/* ── 侧栏：已废弃，彻底隐藏 ───────────────────────────────────────────
   2026-09-04把侧边栏撤掉，内容全部挪进"我的"分区。这里连侧栏本体和它那个
   展开箭头一起隐掉——留一个点开是空白的箭头，比没有更糟。 */
[data-testid="stSidebar"],
[data-testid="stSidebarCollapsedControl"],
[data-testid="collapsedControl"] { display: none !important; }

[data-testid="stSidebar"] { background: var(--fa-surface) !important; border-right: 1px solid var(--fa-border) !important; }
[data-testid="stSidebarContent"] { padding-top: 22px !important; }
/* 侧栏里三个折叠面板原来是三个带边框的圆角大盒子，竖着堆起来很笨重。
   去掉边框和圆角，只留一条极淡的分隔线，让它读起来像一列目录而不是三个控件。 */
[data-testid="stSidebar"] [data-testid="stExpander"] details {
    border: none !important; border-top: 1px solid var(--fa-border) !important;
    border-radius: 0 !important; background: transparent !important;
}
[data-testid="stSidebar"] [data-testid="stExpander"] summary { padding: 11px 2px !important; }
[data-testid="stSidebar"] [data-testid="stExpander"] summary:hover { background: transparent !important; }
[data-testid="stSidebar"] [data-testid="stExpander"] summary p { color: var(--fa-text-2) !important; }
[data-testid="stSidebar"] [data-testid="stExpander"] summary:hover p { color: var(--fa-text) !important; }

/* ── 表格 ─────────────────────────────────────────────────────────────── */
[data-testid="stTable"] td, [data-testid="stTable"] th { border-color: var(--fa-border) !important; font-size: 0.85rem !important; }
[data-testid="stTable"] th { color: var(--fa-muted) !important; font-weight: 500 !important; }

/* ── 自定义类：给手写的卡片/行用 ──────────────────────────────────────── */
.fa-card {
    background: var(--fa-surface); border: 1px solid var(--fa-border);
    border-radius: var(--fa-radius); padding: 18px 20px;
}
.fa-eyebrow {
    font-size: 0.775rem; font-weight: 600; letter-spacing: 0.055em; text-transform: uppercase;
    color: var(--fa-muted); margin: 0 0 12px;
}
.fa-num { font-weight: 600; letter-spacing: -0.02em; }

/* ── 窄屏 ─────────────────────────────────────────────────────────────── */
@media (max-width: 768px) {
    .fa-flex-row { flex-wrap: wrap !important; }
    .fa-flex-row > div { flex: 1 1 auto !important; }
    [data-testid="stHorizontalBlock"] { flex-wrap: wrap !important; }
    [data-testid="stMetricValue"] { font-size: 1.5rem !important; }
    .st-key-fa_nav [data-testid="stRadio"] > div { overflow-x: auto; flex-wrap: nowrap !important; }
    .st-key-fa_nav [data-testid="stRadio"] label { margin-right: 18px !important; white-space: nowrap; }
    .stButton button { font-size: 0.8rem !important; padding: 6px 11px !important; }
    /* 右下角那个 AI 悬浮按钮是 position:fixed，不占文档流，窄屏上会直接压在
       正文最后几行上（2026-09-11前端审计："右下角的 AI 悬浮按钮会盖住正文"）。
       给主内容区留出一段底部安全区，滚到底时最后一行也不会被它挡住。 */
    section.main .block-container { padding-bottom: 96px !important; }
}
</style>
"""

# 2026-09-04前端重做之后，这段CSS里已经不含任何品牌红字面量了——界面色
# 统一成墨色，页面上唯一允许出现的饱和色是涨跌红绿（那些是各处inline写的，
# 从theme.py取值）。所以原来那个把"#e02020"替换成UP_COLOR的技巧不再需要，
# 直接注入即可，少一层看不见的字符串替换。
st.markdown(_FA_BASE_CSS, unsafe_allow_html=True)

# Plotly 图表的统一显示配置。默认会在图右上角浮出一条自带的工具条（相机/缩放/
# 十字光标/自动缩放/全屏那一排图标），它是 Plotly 的品牌痕迹——一眼就能看出
# "这张图是拿现成库画的"，而且那排图标的视觉语言（灰色线框小图标）跟这个项目
# 的其它部分完全不搭。这些功能对"看一眼走势"这个场景也几乎没用。统一关掉，
# 同时关掉双击缩放这类容易误触的交互，让图表回到"一张安静的图"。
# 图表一律只读：不缩放、不拖动、不显示工具栏。
# doubleClick 从 False 改回 "reset"：之前设成 False 是想禁掉双击缩放，但副作用
# 是把"双击复位"这个逃生口也堵死了——用户2026-09-13在新股散点图上误触缩放后
# 卡在一个空白区间退不回来。真正的开关是 charts._apply_chart_theme 里的
# fixedrange=True（轴范围锁死，框选/滚轮/双指全部失效），这里保留 reset 只是
# 万一某张图漏设 fixedrange 时还留一条退路。
_PLOTLY_CONFIG = {
    "displayModeBar": False, "scrollZoom": False, "doubleClick": "reset",
    "displaylogo": False, "staticPlot": False,  # staticPlot 会连 hover 一起关掉，不能开
}


# ── 加载中遮罩 ────────────────────────────────────────────────────────────────
# 这个app所有页面跳转（列表点进详情页、返回列表、切换市场等）走的都是真实的
# <a href="?...">整页导航，浏览器会先展示上一个页面最后一帧，再等Streamlit
# 把新页面整个渲染完替换上去——中间这段空档用户看到的是"新旧内容短暂重叠
# 闪一下"（math-agent那边遇到过同一个问题，已经用一层遮罩盖住这段过渡状态，
# 这里原样搬过来）。遮罩在Streamlit的状态组件不再显示"Running"时淡出移除，
# 不依赖固定延迟时间去猜多久算加载完。
# 总是渲染（不放在登录判断后面），让这个组件iframe每次页面跳转都重新挂载
# 执行一遍——这本身就是遮罩能在新页面重新出现的关键。
#
# 光靠下面这个components.v1.html()注入的遮罩div还不够彻底——它本身是在一个
# 独立iframe里异步加载/执行的，跟页面主体内容的渲染是两条不同步的时间线，
# 实测iframe加载/执行有自己的延迟，主体内容有时候会抢先画出来，遮罩后到，
# 反而先看到一下没盖住的内容再被盖住，等于制造了另一次"闪烁"。这里先用
# st.markdown（原生渲染在Streamlit自己的DOM里，跟页面主体是同一条渲染
# 时间线，没有iframe那层异步延迟）立刻把主体内容透明度设成0，用!important
# 保证优先级最高；下面iframe里的JS判断真正加载完成后，直接把这个<style>
# 标签整个删掉（不是覆盖，删除比设置内联样式更可靠——!important的规则内联
# style优先级压不过它，必须真删除这条规则本身）。
st.markdown(
    "<style id='_fa_loader_css'>[data-testid=\"stAppViewContainer\"]{opacity:0!important}</style>",
    unsafe_allow_html=True,
)
# 之前这个遮罩只在"真实整页导航"（点<a href>链接）时生效，切"行情/持仓"
# 这种纯靠st.radio触发的内部rerun时完全不出现——用户反馈"持仓页面还是有
# 上一页残留"，排查发现st.components.v1.html()传入的HTML/JS内容如果两次
# rerun之间字节完全相同，Streamlit前端不会重新挂载这个iframe（判定为"没变化"
# 直接跳过），脚本也就不会重新执行，遮罩自然不会在内部rerun时重新出现。
# 之前每次整页导航时内容恰好每次都是这同一份固定字符串，只有"完整刷新文档"
# 这种场景会绕开这层去重（浏览器整个重新加载，不存在"和上次一样跳过"这回事），
# 这就是"整页导航生效、内部切换不生效"这个差异的真正原因。改成把当前时间戳
# 埋进一个不可见的HTML注释里，保证每次rerun这段内容字符串都不一样，
# Streamlit就没法把它当成"没变化"跳过，每次rerun都会重新挂载、重新执行。
_cv1.html("""
<!-- __FA_LOADER_NONCE__ -->
<script>
(function() {
try {
    var doc = window.parent.document;
    var ov = doc.getElementById('_fa_loader');
    if (!ov) {
        ov = doc.createElement('div');
        ov.id = '_fa_loader';
        ov.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;z-index:99999;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:12px;transition:opacity 0.35s;background:#FAFAFB';
        ov.innerHTML = '<div style="width:26px;height:26px;border:2px solid #E4E4EA;border-top-color:#17181C;border-radius:50%;animation:_fa_spin 0.75s linear infinite"></div><div style="font-size:0.78rem;color:#A8ABB3;font-family:Inter,-apple-system,sans-serif;letter-spacing:.06em">加载中</div><style>@keyframes _fa_spin{to{transform:rotate(360deg)}}</style>';
        doc.body.appendChild(ov);
    }
    function _fa_removeHideCss() {
        var css = doc.getElementById('_fa_loader_css');
        if (css) css.remove();
    }
    var _ovTries = 0;
    var _ovIv = setInterval(function() {
        _ovTries++;
        if (_ovTries > 40) { clearInterval(_ovIv); _fa_removeHideCss(); if (ov) { ov.style.opacity='0'; setTimeout(function(){ if (ov) ov.remove(); },350); } return; }
        var status = doc.querySelector('[data-testid="stStatusWidget"]');
        var running = status && (status.textContent || '').indexOf('Running') !== -1;
        var app = doc.querySelector('[data-testid="stAppViewContainer"]');
        if (app && !running) {
            clearInterval(_ovIv);
            _fa_removeHideCss();
            setTimeout(function(){ if (ov) { ov.style.opacity='0'; setTimeout(function(){ if (ov) ov.remove(); },350); } }, 250);
        }
    }, 150);
} catch(e2) {}
})();
</script>
""".replace("__FA_LOADER_NONCE__", datetime.now().isoformat()), height=0)


def _show_login_page():
    st.markdown(
        "<div style='text-align:center;padding:60px 0 24px'>"
        "<div style='font-size:1.5rem;font-weight:600;margin:8px 0 4px'>Invest Agent</div>"
        "<div style='font-size:0.85rem;color:var(--fa-muted)'>行情 + 财务 + 新闻交叉验证</div>"
        "</div>",
        unsafe_allow_html=True,
    )
    _, mid, _ = st.columns([1, 2, 1])
    with mid:
        # 2026-08-25：游客模式入口按用户要求关掉了，安全优先——下面
        # guest_mode 相关的分支逻辑（第367行的登录墙判断、持仓/关注/AI分析
        # 历史那几处 if not logged_in 的降级展示）都还留着，只是没有任何
        # 入口能把 guest_mode 置成 True 了，全站恢复成"不登录什么都看不到"。
        # 如果以后要重新开放访客浏览，把下面这个按钮加回来就行，不用重写
        # 其它地方的判断逻辑。
        tab_l, tab_r = st.tabs(["登录", "注册"])
        with tab_l:
            _em = st.text_input("邮箱", key="li_email", placeholder="your@email.com")
            _pw = st.text_input("密码", type="password", key="li_pw")
            if st.button("登录", type="primary", use_container_width=True, key="do_login"):
                _ok, _msg = _check_user(_em, _pw)
                if _ok:
                    _tok = _create_token(_em)
                    # token 不写进 st.query_params——7天免登录凭证常驻在可见地址栏里
                    # 会被Nginx access log/浏览器历史明文留存。当次会话靠session_state
                    # 就够了，后续每个卡片链接会通过_auth_qs()从session_state重新
                    # 拼一份，不依赖当前地址栏残留的这一份。
                    st.session_state["logged_in"] = True
                    st.session_state["user_email"] = _em
                    st.session_state["_token"] = _tok
                    # localStorage继续写一份留作兜底/legacy；Cookie才是"下次
                    # 打开自动登录"真正依赖的那条路径——见下面"记住登录"那段
                    # 注释，st.context.cookies能在服务端直接读到它，不需要
                    # 再靠iframe强制刷新页面（那条路已经被浏览器拦掉了）。
                    _cv1.html(
                        f"""<script>try{{
                        window.parent.localStorage.setItem("fa_auth_tok","{_tok}");
                        var d = new Date(); d.setTime(d.getTime() + 7*24*60*60*1000);
                        window.parent.document.cookie = "fa_auth_tok={_tok}; expires=" + d.toUTCString() + "; path=/; SameSite=Lax; Secure";
                        }}catch(e){{}}</script>""",
                        height=1,
                    )
                    st.rerun()
                else:
                    st.error(_msg)
        with tab_r:
            _rem = st.text_input("邮箱", key="reg_email", placeholder="your@email.com")
            _rpw = st.text_input("密码（至少6位）", type="password", key="reg_pw")
            _rpw2 = st.text_input("确认密码", type="password", key="reg_pw2")
            if st.button("注册账号", type="primary", use_container_width=True, key="do_reg"):
                if not _rem or "@" not in _rem:
                    st.error("请输入有效邮箱")
                elif len(_rpw) < 6:
                    st.error("密码至少6位")
                elif _rpw != _rpw2:
                    st.error("两次密码不一致")
                elif _user_exists(_rem):
                    st.error("该邮箱已注册（跟 math-agent 共用同一套账号，那边注册过这里也能直接登）")
                else:
                    try:
                        _register_user(_rem, _hash_pw(_rpw))
                        st.success("注册成功，请切换到登录标签页")
                    except Exception as _e:
                        st.error(f"注册失败：{_e}")


# ── 记住登录（Cookie，7天）───────────────────────────────────────────────
# 之前这里是"读localStorage→塞进URL→800ms后用iframe强制刷新父页面"那一套，
# 2026-08-28实测在当前Chrome版本下彻底失效：iframe（components.v1.html创建
# 的沙盒iframe）读写window.parent.localStorage没问题，但
# window.parent.location.replace(...)会被浏览器当成"沙盒iframe试图导航
# 顶层页面"直接拦截，控制台报SecurityError——token其实一直是有效的（拿
# 同一个token单独调用_validate_token()验证过没问题），只是浏览器根本没
# 机会把它带着发一次新请求给服务器，页面永远卡在登录页。
#
# 改用Cookie：写入不需要导航权限，只需要access parent origin（跟
# localStorage是同一类同源访问权限，已经在用了，不受上面那条限制）；
# Cookie会由浏览器自动带在*每一次*请求里（包括第一次打开页面的那次
# HTTP请求），服务端用Streamlit自带的st.context.cookies直接读，完全
# 不需要JS参与"读取"这一步、也不需要强制刷新——从根上绕开了这个安全
# 限制，而且比原来的方案更快（省掉了800ms的人为延迟和一次额外的整页
# 重新加载）。旧的_auth query-param路径保留作为兜底（分享链接/禁用
# Cookie的场景）。
if not st.session_state.get("logged_in"):
    _cookie_token = st.context.cookies.get("fa_auth_tok")
    if _cookie_token:
        _auto_email = _validate_token(_cookie_token)
        if _auto_email:
            st.session_state["logged_in"] = True
            st.session_state["user_email"] = _auto_email
            st.session_state["_token"] = _cookie_token

_stored_token = st.query_params.get("_auth", "") or ""
if _stored_token and not st.session_state.get("logged_in"):
    _auto_email = _validate_token(_stored_token)
    if _auto_email:
        st.session_state["logged_in"] = True
        st.session_state["user_email"] = _auto_email
        st.session_state["_token"] = _stored_token
        _cv1.html(
            f"""<script>
            try {{
                var tok = {_stored_token!r};
                var d = new Date(); d.setTime(d.getTime() + 7*24*60*60*1000);
                window.parent.document.cookie = "fa_auth_tok=" + encodeURIComponent(tok) + "; expires=" + d.toUTCString() + "; path=/; SameSite=Lax; Secure";
                var url = new URL(window.parent.location.href);
                if (url.searchParams.get('_auth')) {{
                    url.searchParams.delete('_auth');
                    window.parent.history.replaceState(null, '', url.toString());
                }}
            }} catch(e) {{}}
            </script>""",
            height=1,
        )
    else:
        try:
            del st.query_params["_auth"]
        except Exception:
            pass
        _cv1.html(
            '<script>try{window.parent.localStorage.removeItem("fa_auth_tok");window.parent.document.cookie="fa_auth_tok=; max-age=0; path=/";}catch(e){}</script>',
            height=1,
        )

if not st.session_state.get("logged_in") and not st.session_state.get("guest_mode"):
    _show_login_page()
    st.stop()

# 主导航分区 ←→ 地址栏 slug。中文分区名直接进 URL 会被 percent-encode 成一长串
# 看不懂的 %XX，所以对外用英文短名。
_SLUG_BY_SECTION = {
    "首页": "home", "行情": "market", "持仓": "positions",
    "自选": "watchlist", "AI模拟炒股": "sim", "我的": "me",
}
_SECTION_BY_SLUG = {v: k for k, v in _SLUG_BY_SECTION.items()}

# 个股详情页有稳定的可分享地址。旧的 open_* 是一次性跳转参数，第一次进入
# 后会改写为 symbol/market/name/section 四个规范参数；后者不清理，刷新或复制
# 地址仍能回到同一只标的。认证令牌不放进规范地址，登录态由七天 Cookie 维持。
_route_symbol = st.query_params.get("symbol") or st.query_params.get("open_symbol")
if _route_symbol:
    _route_is_legacy = bool(st.query_params.get("open_symbol"))
    _route_market = st.query_params.get("market") or st.query_params.get("open_market", "A")
    _route_name = st.query_params.get("name") or st.query_params.get("open_name", _route_symbol)
    _route_section = st.query_params.get("section") or st.query_params.get("open_from")
    st.session_state["_detail_symbol"] = _route_symbol
    st.session_state["_detail_market"] = _route_market
    st.session_state["_detail_name"] = _route_name
    # 从卡片点进来的，"返回"要能回到原来那个分区，不是每次都弹回默认的
    # "行情"——整页导航会把session_state清空，"_active_section"记不住是从哪个
    # 分区点进来的，得靠这个参数显式带过来。历史上这里只认"pos"一个值、固定
    # 回"持仓"，自选拆成独立分区后就不够用了，改成直接带分区名。
    _from = _route_section
    _VALID_SECTIONS = ("首页", "行情", "持仓", "自选", "AI模拟炒股", "我的")
    if _from == "pos":          # 兼容还没刷新的旧页面里残留的老链接
        st.session_state["_active_section"] = "持仓"
    elif _from in _VALID_SECTIONS:
        st.session_state["_active_section"] = _from
    if _from:
        st.session_state["_detail_return_section"] = st.session_state.get("_active_section", "持仓")
    if _route_is_legacy:
        # 旧链接带 _auth 时也只使用一次；规范 URL 不泄露可登录令牌。
        st.query_params.clear()
        st.query_params.update({"symbol": _route_symbol, "market": _route_market, "name": _route_name})
        if _from:
            st.query_params["section"] = _from
        st.rerun()
if st.query_params.get("open_macro"):
    st.session_state["_macro_detail_name"] = st.query_params["open_macro"]
    st.query_params.clear()
    st.rerun()
if st.query_params.get("open_index_code"):
    st.session_state["_index_detail_code"] = st.query_params["open_index_code"]
    st.session_state["_index_detail_market"] = st.query_params.get("open_index_market", "A")
    st.session_state["_index_detail_name"] = st.query_params.get("open_index_name", "")
    st.query_params.clear()
    st.rerun()
if st.query_params.get("open_sector"):
    st.session_state["_sector_detail_name"] = st.query_params["open_sector"]
    st.session_state["_sector_detail_market"] = st.query_params.get("open_sector_market", "A")
    st.query_params.clear()
    st.rerun()


_BENCHMARK_NAMES = {"A": "沪深300", "HK": "恒生指数", "US": "标普500"}


def _fetch_news_items(keyword: str, symbol: str | None, market: str) -> tuple:
    """页面展示和AI分析要用同一份新闻源，不然会出现页面上一手资讯明明有
    （比如寒武纪的官方公告），AI资讯解读那栏却说"没有找到相关新闻"这种自相
    矛盾的情况。优先级：沪深官方公告（get_stock_notices，监管强制披露，永远
    免费）> 富途资讯搜索（get_futu_news，真按关键词匹配，港股/美股/沪深通吃，
    链接免费可读）> 财新关键词匹配（get_stock_news，兜底，有付费墙）。
    返回 (DataFrame, 来源标记："notices"/"futu"/"caixin")。
    """
    if market == "A" and symbol:
        try:
            notices = get_stock_notices(symbol)
        except Exception:
            notices = None
        if notices is not None and not notices.empty:
            return notices, "notices"

    try:
        futu_news = get_futu_news(keyword, max_count=8, symbol=symbol, market=market)
    except Exception:
        futu_news = None
    if futu_news is not None and not futu_news.empty:
        return futu_news, "futu"

    # 有证券代码却没有精确命中的富途资讯时，不能再退回名称模糊匹配。后者会
    # 重新把同名/同类基金的新闻塞进来，等于绕过了上面的精确过滤。
    if symbol:
        return pd.DataFrame(columns=["日期", "新闻标题", "分类", "url"]), "none"
    try:
        news = get_stock_news(keyword, limit=8)
    except Exception:
        news = None
    return news, "caixin"


def _completed_history_for_ai(hist: pd.DataFrame | None) -> pd.DataFrame:
    """Remove an exchange's not-yet-formed zero-volume daily bar before AI sees it.

    Some upstream feeds append the current session to daily history before the
    market opens.  That placeholder is useful neither as price history nor as
    evidence of "shrinking volume", so retain only completed bars for the
    model input.  Charts deliberately keep their original data; this is an
    analysis-input guard, not a display transformation.
    """
    if hist is None or hist.empty:
        return pd.DataFrame() if hist is None else hist
    if "成交量" not in hist.columns:
        return hist
    volume = pd.to_numeric(hist["成交量"], errors="coerce")
    return hist.loc[volume > 0].copy()


def _build_sparkline_svg(values: list, color: str, width: int = 60, height: int = 26) -> str:
    """持仓行情列表里那种"一眼看趋势"的迷你走势图——不用plotly（每行一个太重，
    列表长了会很卡），纯手算折线点位吐一段内联SVG，跟长桥/同花顺那种列表里的
    小图一个意思。
    """
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return "<span style='color:var(--fa-muted);font-size:0.7rem'>--</span>"
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or (abs(lo) * 0.01 or 1)
    n = len(vals)
    pts = [f"{(i / (n - 1) * width):.1f},{(height - (v - lo) / rng * height):.1f}" for i, v in enumerate(vals)]
    points_str = " ".join(pts)
    return (
        f"<svg width='{width}' height='{height}' viewBox='0 0 {width} {height}' style='display:block'>"
        f"<polyline points='{points_str}' fill='none' stroke='{color}' stroke-width='1.6' "
        f"stroke-linejoin='round' stroke-linecap='round'/></svg>"
    )


@st.cache_data(ttl=12 * 3600, show_spinner=False)
def _sparkline_closes_cached(symbol: str, market: str, day_key: str, days: int = 20) -> tuple:
    """迷你走势图用的日线收盘价，按自然日缓存，一天只真正取一次。

    真实故障（2026-09-04定位）：自选页"每隔一会儿就卡一下、迷你图集体消失"
    这个老毛病，根因就在这里。原来这个函数直接复用 get_stock_history，跟着
    它 ttl=1800 的缓存走；缓存一过期，列表刷新那一轮就要把 21 支自选的日线
    全部重取一遍。而港美股的历史查询在 data_sources 里是串到单个常驻 Futu
    worker 线程上排队的（见 _futu_call），外面用线程池并发也没用，实测 21 支
    串行要 12.57 秒——但取数那一批的 deadline 只有 4 秒。于是每 30 分钟必然
    出现一次：超时、大部分行拿不到数据、迷你图变空、页面明显卡顿，下一轮再
    重来一次。相比之下同一批的实时行情是批量查的，21 支只要 0.14 秒，完全
    不是瓶颈。

    修法就是把这份数据从"实时行情"的缓存节奏里摘出来单独管：迷你图画的是
    近 20 个交易日的日线收盘，一天最多变一次，没有任何理由跟着秒级行情的
    缓存一起失效。day_key 传当天日期，配合 12 小时 ttl，等于"每支每天最多
    取一次"，此后所有刷新都是内存命中（实测 0.01 秒）。

    返回 tuple 而不是 list：st.cache_data 返回的可变对象被调用方改到会污染
    缓存，元组从根上避免这个坑，调用方要 list 自己转。
    """
    try:
        end = cn_now().strftime("%Y%m%d")
        start = (cn_now() - timedelta(days=days * 2 + 10)).strftime("%Y%m%d")
        hist = get_stock_history(symbol, start, end, market=market)
        if hist is None or hist.empty:
            return ()
        return tuple(hist["收盘"].astype(float).tail(days).tolist())
    except Exception:
        return ()


def _fetch_sparkline_closes(symbol: str, market: str, days: int = 20) -> list:
    """取迷你图数据，并且"一旦拿到过就不会再变空"。

    上面那层缓存解决了"每 30 分钟重取一次"，这里再兜一层：万一某次真的取不到
    （网络抖动、Futu 断线、当天第一次取正好超时），不要让这一行的迷你图突然
    变成空白——那正是这个页面以前最难看的一种抖动。把最近一次成功的结果记在
    session 里，取不到就沿用上一次的形状。数据宁可旧一点，也不要闪。
    """
    closes = list(_sparkline_closes_cached(symbol, market, cn_now().strftime("%Y%m%d"), days))
    key = f"_spark_last_good_{symbol}_{market}"
    if closes:
        st.session_state[key] = closes
        return closes
    return st.session_state.get(key, [])


def _fmt_turnover(v) -> str:
    if v is None:
        return "—"
    try:
        v = float(v)
    except Exception:
        return "—"
    if v >= 1e8:
        return f"{v / 1e8:.2f}亿"
    if v >= 1e4:
        return f"{v / 1e4:.1f}万"
    return f"{v:.0f}"


def _run_concurrent_with_deadline(items: list, fn, timeout: float, max_workers: int = 8) -> dict:
    """一批任务并发跑、给整批一个统一的deadline（从submit那一刻算起，不是从"轮到
    检查它"那一刻）——持仓列表/组合对比/回看补录三处原来各自手写一份几乎一样的
    "ThreadPoolExecutor + concurrent.futures.wait(timeout) + shutdown(wait=False)"，
    抽成这一个共享helper，不用再维护三份重复代码。

    统一deadline是刻意的：三处原来都各自踩过同一个坑——按"逐个future.result(timeout=N)"
    实现时，每个future的超时窗口是从"轮到检查它"那一刻才开始计时，排在后面检查的
    future会绕过预期上限（前面几个如果很快返回，轮到最后一个时它已经跑了好几秒、
    检查时又给它一次全新的N秒，总耗时照样能远超预期，没起到限制作用）。改用
    concurrent.futures.wait() 给整批futures一个从submit那一刻算起的统一截止时间，
    时间到了还没完成的直接跳过，才是真正把整批总耗时卡在timeout附近。

    cancel_futures=True：shutdown时把"提交了但线程池还没轮到开始跑"的future取消掉，
    减少浪费。已经在执行中、卡在网络请求里的线程没法从Python这层强制中断——deadline
    到了之后它们会在后台自己跑完/超时再自然退出，不阻塞这个函数返回。这不是"无限
    堆积"的线程泄漏：各处传进来的fn目前都是走内部已有重试/超时保护的数据源接口
    （get_stock_realtime/get_stock_history等），最终都会自己返回，只是可能比这里
    的deadline稍晚，不是真正无界悬挂；如果以后有调用点传入没有自带超时的fn，这条
    保证就不成立了，调用方要自己确认。

    返回：{items里的位置索引 -> fn(item)的返回值}，deadline内没完成/抛异常的位置
    不会出现在字典里，调用方按"i in results"判断这一项是不是被跳过了。用位置索引
    而不是item本身做key，是因为item可能是dict这类不可hash的类型。
    """
    results: dict[int, object] = {}
    if not items:
        return results
    ex = ThreadPoolExecutor(max_workers=min(max_workers, len(items)))
    futures = {ex.submit(fn, item): i for i, item in enumerate(items)}
    done, _not_done = _futures_wait(list(futures.keys()), timeout=timeout)
    for fut in done:
        try:
            results[futures[fut]] = fut.result()
        except Exception:
            pass
    ex.shutdown(wait=False, cancel_futures=True)
    return results


def _auth_qs() -> str:
    """卡片链接的<a href="?...">会触发真正的整页导航（不是Streamlit的软rerun），
    URL的query string会被整个替换掉——如果不把登录用的_auth token也带上，
    跳转后session_state被清空，会先闪一下登录页，等localStorage自动登录的
    JS再刷新一次才恢复，两次整页刷新叠加体验很差。这里统一把当前token拼进
    每个卡片链接，跳转就是一步到位，不会闪登录页。
    """
    token = st.session_state.get("_token", "")
    return f"&_auth={urllib.parse.quote(token)}" if token else ""


def _resolve_add_symbol(q: str, market_code: str) -> str | None:
    """"新增持仓"用的名称→代码解析，沪深之前一直漏了——resolve_symbol_by_name
    只支持HK/US（内部的知名股名单和Futu模糊搜索都没有沪深这块），沪深market
    传进去必然返回None，退化成直接把"茅台"这种中文名当代码用，当然查不到。
    这里沪深单独先走search_stock_by_name（BaoStock按名称模糊匹配，真支持沪深），
    查不到再试_A_FUND_NAME_MAP——BaoStock按名称搜索只覆盖个股不含ETF/基金，
    "沪深300ETF"这类名字搜不到，用户反馈过这个问题。
    """
    q = q.strip()
    if market_code == "A":
        try:
            matches = search_stock_by_name(q)
        except Exception:
            matches = []
        if matches:
            return matches[0]["code"]
        fund_code = _A_FUND_NAME_MAP.get(q.lower())
        if fund_code:
            return fund_code
        return q if re.match(r"^\d{6}$", q) else None
    by_name = resolve_symbol_by_name(q, market_code)
    if by_name:
        return by_name
    return q.zfill(5) if market_code == "HK" else q.upper()


# 搜索/弹窗进入详情页也必须写入同一套规范 URL。此前只有卡片链接经过文件
# 顶部的 open_symbol 转换；搜索历史和添加持仓弹窗是直接改 session_state，
# 导致地址栏仍是首页，刷新或复制链接就丢了当前标的。
def _open_detail_route(symbol: str, market: str, name: str, section: str = "我的") -> None:
    """跳到个股详情并保留可刷新、可分享的规范地址。

    认证令牌故意不进入地址栏：登录态由七天 Cookie 维持，不能为了分享页面
    把可登录凭证留在浏览器历史或服务器访问日志里。
    """
    st.session_state["_detail_symbol"] = symbol
    st.session_state["_detail_market"] = market
    st.session_state["_detail_name"] = name
    st.session_state["_detail_return_section"] = section
    st.session_state["_active_section"] = section
    st.query_params.clear()
    st.query_params.update({"symbol": symbol, "market": market, "name": name, "section": section})
    st.rerun()


# 搜索历史的点击跳转在这里处理，而不是跟其它 open_* 参数一起放在文件开头：
# 这段要调用下面刚定义的 _resolve_add_symbol（搜索历史存的是用户当时输入的
# 原始词，"Microsoft"、"苹果"、"nike"，不是股票代码，得现查）。模块级代码
# 自上而下执行，放在文件开头会 NameError 直接把整页打挂。
# 搜索历史点进来的链接。这里跟 open_symbol 分开处理，因为搜索历史存的是
# 用户当时输入的原始词（"Microsoft"、"苹果"、"nike"），不是股票代码——代码
# 要现查才知道。解析放在跳转这一刻做而不是渲染列表时批量做：列表有八条，
# 渲染时全解析一遍要发八次行情请求，而用户通常只点其中一条。
if st.query_params.get("open_search"):
    _q = st.query_params["open_search"]
    _m = st.query_params.get("open_search_market", "A")
    _sym = _resolve_add_symbol(_q, _m)
    if _sym:
        _open_detail_route(_sym, _m, _q, "我的")
    else:
        # 查不到就别静默吞掉——这条历史可能是当时搜错的词，或者标的已经退市。
        st.session_state["_search_open_error"] = _q
    st.query_params.clear()
    st.rerun()


@st.cache_data(ttl=60, show_spinner=False)
def _get_hot_stock_names() -> set:
    """"新闻标红"用的交叉比对集合——不是新闻本身的热度分数（免费数据源里
    没有真实的新闻热度数据，实测过NewsAPI的totalResults当代理指标也不可靠：
    中文公司名基本查不到，英文名又被无关内容严重干扰，"Apple"两天内2000多条
    大多是无关内容，不是真的"苹果公司今天很火"），改用我们自己已有的、真实
    可靠的当日异动数据做交叉验证：新闻提到的公司如果今天正好在涨停/跌停
    股池、或者港股/美股核心股里涨跌幅超过一定幅度，就认为是"今天值得关注"，
    标题标红；不是新闻热度，是"新闻提到的标的今天股价表现是否异常"，如实
    是这个语义，不是编一个假热度。
    """
    names = set()
    try:
        up = get_limit_pool("up", limit=30)
        if up is not None and not up.empty:
            names.update(up["名称"].dropna().tolist())
    except Exception:
        pass
    try:
        down = get_limit_pool("down", limit=30)
        if down is not None and not down.empty:
            names.update(down["名称"].dropna().tolist())
    except Exception:
        pass
    try:
        hk = get_hk_famous_movers(15)
        if hk is not None and not hk.empty:
            names.update(hk[hk["涨跌幅"].abs() > 3]["名称"].dropna().tolist())
    except Exception:
        pass
    try:
        us = get_us_famous_movers(15)
        if us is not None and not us.empty:
            names.update(us[us["涨跌幅"].abs() > 3]["名称"].dropna().tolist())
    except Exception:
        pass
    return names


_NEWS_BOILERPLATE_WORDS = ("专享", "提前", "本文系", "独家", "转载", "原创", "免责")


def _strip_news_boilerplate(title: str) -> str:
    """剥掉资讯源自己加的发布方声明前缀。

    2026-09-13：首页八条新闻标题全部以"【本文系数据通用户提前专享】"开头，
    一屏看下去前十几个字完全一样，真正的标题被挤到后面。这段文字是资讯源的
    分发声明，跟内容无关。

    只剥"声明类"的方括号，不是见【】就删——【腾讯控股】【美联储】这种带信息
    的前缀要留着。判据是括号里出现了专享/转载/独家这类分发用词。
    只处理开头连续的几个，中间和结尾的一律不动。
    """
    s = str(title or "").strip()
    for _ in range(3):                      # 最多剥三层，防止异常数据无限循环
        if not s.startswith("【"):
            break
        end = s.find("】")
        if end < 0:
            break
        inner = s[1:end]
        if not any(w in inner for w in _NEWS_BOILERPLATE_WORDS):
            break                           # 带信息的前缀，留着
        s = s[end + 1:].lstrip()
    return s or str(title or "")


def _news_title_style(title: str, hot_names: set) -> str:
    """标题里提到了今天有异动的公司名就加重，否则用次级文字色。

    2026-09-04改：原来这里是"提到异动股就标红"。但红色在这个项目里从头到尾
    只有一个含义——涨（见theme.py）。一条新闻标题标红并不代表这条消息是利好，
    只代表它提到了某支今天在动的股票，两件事撞在同一个颜色上，读者第一眼会
    误读成"这是利涨的消息"。而且满屏红标题会把页面上真正该抢眼的涨跌数字淹掉。
    改成用字重和文字色深浅做区分：命中的更黑更重，没命中的退到次级灰。层级
    照样一眼分得出来，红色留给它唯一该有的含义。
    """
    if any(name and name in title for name in hot_names):
        return "color:var(--fa-text);font-weight:600"
    return "color:var(--fa-text-2)"


def _esc(s) -> str:
    """新闻标题/URL 等外部抓取内容拼进 HTML 前统一转义，防 XSS。"""
    if s is None:
        return ""
    return html.escape(str(s), quote=True)


_CN_TZ = timezone(timedelta(hours=8))


def _to_cn_dt(iso_str: str) -> datetime | None:
    """tracker.py里run_at/created_at统一存的是datetime.now(timezone.utc).isoformat()
    ——数据库存UTC是对的(不该跟着服务器本地时区存，见data_sources.cn_now()
    同一个教训)，但界面上给用户看的必须是北京时间，2026-09-01用户反馈AI
    模拟盘的收益曲线/决策记录时间戳显示的是UTC（比如凌晨4点多），跟他自己
    的时区对不上，这里统一转换。返回datetime对象而不是格式化字符串——
    图表x轴需要真正的时间类型，不能传字符串给Plotly猜。
    """
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_CN_TZ)
    except (ValueError, TypeError):
        return None


def _to_cn_time_str(iso_str: str) -> str:
    dt = _to_cn_dt(iso_str)
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else (iso_str or "")[:19].replace("T", " ")


def _quote_market_status(spot: dict, market: str) -> str:
    """根据交易所时区和交易时段描述报价状态，不能把昨收当“实时”。"""
    zone = {"A": "Asia/Shanghai", "HK": "Asia/Hong_Kong", "US": "America/New_York"}.get(market)
    if not zone:
        return "报价"
    tz = ZoneInfo(zone)
    now = datetime.now(tz)
    try:
        # 2026-09-11修：这里原来假设Futu的update_time统一按北京时间给出，
        # 不管哪个市场都先按Asia/Shanghai解释再转成交易所本地时区。真实
        # 故障（前端审计发现，用真实行情核对过）：可口可乐详情页显示
        # "已收盘 · 美东09-10 23:19"，而当时美东实际是09-11 11:19、正在
        # 盘中——现查Futu的get_market_snapshot证实，美股的update_time字段
        # 本来就是交易所当地时间（美东，naive，不带时区），不是北京时间；
        # 之所以港股/沪深这条路径一直没暴露问题，是因为香港/中国跟北京
        # 恰好同一个UTC+8时区，"按北京时间解释"这一步在数值上是空操作，
        # 只有美股(UTC-4/-5)才会因为这个错误假设多减/多减一次时区差，
        # 时间和日期都跟着错位。改成naive时间戳直接当成交易所本地时间，
        # 不再统一套一层"先当北京时间"的转换。
        stamp = pd.Timestamp(spot.get("更新时间")).to_pydatetime()
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=tz)
        else:
            stamp = stamp.astimezone(tz)
    except Exception:
        stamp = now

    local_time = now.time()
    weekday = now.weekday() < 5
    if market == "US":
        open_now = weekday and datetime.strptime("09:30", "%H:%M").time() <= local_time < datetime.strptime("16:00", "%H:%M").time()
    elif market == "HK":
        open_now = weekday and (datetime.strptime("09:30", "%H:%M").time() <= local_time < datetime.strptime("12:00", "%H:%M").time() or datetime.strptime("13:00", "%H:%M").time() <= local_time < datetime.strptime("16:00", "%H:%M").time())
    else:
        open_now = weekday and (datetime.strptime("09:30", "%H:%M").time() <= local_time < datetime.strptime("11:30", "%H:%M").time() or datetime.strptime("13:00", "%H:%M").time() <= local_time < datetime.strptime("15:00", "%H:%M").time())
    place = {"A": "北京时间", "HK": "香港", "US": "美东"}[market]
    if open_now and stamp.date() == now.date():
        return f"交易中 · {place} {stamp:%H:%M}"
    if weekday and local_time < datetime.strptime("09:30", "%H:%M").time():
        return f"盘前 · {place} {stamp:%H:%M}"
    return f"已收盘 · {place} {stamp:%m-%d %H:%M}"


def _fmt_price(value, default: str = "—") -> str:
    """价格按量级决定小数位，并统一加千分位（2026-09-11前端审计）。

    原来全站价格都是写死的 :.2f：BTC 显示成 77100.86（七万多的数字没有
    千分位，一眼读不出量级），DOGE/ARB 这类小币显示成 0.08 / 0.15（0.0823
    和 0.0849 看起来是同一个价，实际差 3%）。股票价格几乎都在个位到四位数
    之间，两位小数够用；加密货币横跨 0.0001 到 100000 六个数量级，必须按
    量级给小数位，不然要么精度丢光要么一堆无意义的零。
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    av = abs(v)
    if av >= 1:
        return f"{v:,.2f}"
    if av >= 0.01:
        return f"{v:,.4f}"
    return f"{v:,.6f}"


def _fmt_usd_signed(value, decimals: int = 0, default: str = "—") -> str:
    """带正负号的美元金额，负号写在货币符号外面。

    2026-09-11前端审计抓到 f"${v:+,.0f}" 会打出「$-156」——符号被 f-string
    塞进了 $ 后面。会计和行情软件的通行写法是 -$156：货币符号紧贴数字，
    符号在最外层。正数保留 + 号（这几处指标就是要一眼看出方向）。
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    sign = "-" if v < 0 else "+"
    return f"{sign}${abs(v):,.{decimals}f}"


def _clean_name(name) -> str:
    """去掉行情接口返回的名称里的补位空格。

    2026-09-11前端审计抓到「南 京 港」这种——沪深接口对三个字的名字会用
    全角空格补齐成四个字宽（老行情软件对齐用的习惯），原样渲染到网页上
    就变成了字中间带空格。

    2026-09-12再修（同一轮审计的另一条）：上一版把半角空格也一并删掉了，
    结果英文名跟着遭殃——"Meta Platforms" 被压成了 "MetaPlatforms"。
    这两种空格的性质完全不同：中文名里的空格必然是补位（中文词之间不写
    空格），英文名里的空格是词的分隔符，删了就是错别字。所以只有在名称
    含中文时才连半角空格一起删；纯英文名只做首尾strip和连续空格归一。
    """
    s = str(name or "").replace("　", "")
    has_cjk = any("一" <= ch <= "鿿" for ch in s)
    if has_cjk:
        return s.replace(" ", "")
    return " ".join(s.split())


def _sim_note_for_display(note: str) -> str:
    """决策记录标题里的note要给人看，不是给开发者看的。

    2026-09-11前端审计抓到：AI调用失败时，note存的是供应商返回的整段原文
    （Error code: 403 - {'error': {'code': 'AccountOverdueError', 'message':
    '...Request id: 02178894...'}}），这一整串被原样拼进了折叠框标题，
    用户看到的是一行英文报错加一串请求ID。这里把已知的几类失败翻译成一句
    中文，其余的截断；原文仍然留在数据库里，排查时照样查得到。
    """
    text = str(note or "")
    if not text:
        return ""
    lowered = text.lower()
    if "accountoverdue" in lowered or "arrearage" in lowered or "insufficient balance" in lowered:
        return "AI服务商账户余额不足，本次决策未执行"
    if "rate limit" in lowered or "429" in text or "too many requests" in lowered:
        return "AI服务商限流，本次决策未执行"
    if "timeout" in lowered or "timed out" in lowered:
        return "AI调用超时，本次决策未执行"
    if text.startswith("AI调用失败"):
        return "AI调用失败，本次决策未执行"
    # 未知情况：只保留前40个字，避免整段JSON糊在标题上。
    return text if len(text) <= 40 else text[:40] + "…"


def _chat_bubble(role: str, text: str) -> str:
    """AI咨询浮窗用的聊天气泡——用户明确要求跟主流AI聊天产品一致的经典
    样式：用户消息靠右、蓝底白字；AI回复靠左、白底黑字，不带任何头像图标
    （st.chat_message自带的默认头像是卡通小图标，跟"不许有emoji/装饰图标"
    的要求冲突，改成纯HTML拼这个气泡，不用chat_message）。
    用户消息走_esc转义（用户输入不可信，防XSS）；AI回复不转义、原样走
    markdown渲染——AI输出的**加粗**这类格式化要保留，且是本模块自己生成的
    内容，不是外部抓取的不可信文本。
    """
    is_user = role == "user"
    align = "flex-end" if is_user else "flex-start"
    # 2026-09-11：用户气泡从蓝色(#2563eb)改成墨色。用户定的全站规矩是
    # "主色调黑白灰，只有真正需要强调的元素才用彩色"——一个区分说话人的
    # 气泡底色不属于需要强调的信息，深浅对比已经足够分清谁是谁，蓝色在
    # 这套灰白界面里是唯一一块跟涨跌红绿无关的彩色，显得很突兀。
    bg = "#17181C" if is_user else "#f0f1f3"
    color = "#fff" if is_user else "#1a1a1a"
    body = _esc(text) if is_user else text
    return (
        f"<div style='display:flex;justify-content:{align};margin:6px 2px'>"
        f"<div style='max-width:82%;padding:8px 13px;border-radius:16px;background:{bg};"
        f"color:{color};font-size:0.88rem;line-height:1.5;white-space:pre-wrap;word-break:break-word'>"
        f"{body}</div></div>"
    )


def _typing_indicator_html() -> str:
    """AI气泡里的"正在想"三点跳动动画——2026-08-30新增。用户反馈发消息后
    在拿到第一个字之前整个界面"像死机一样"：这轮对话现在会先做一次不流式
    的探测请求判断要不要调用工具（见assistant.stream_reply），探测本身
    实测能花4-10秒，这段时间原来的代码从用户消息发出去到出现任何回应
    之间是真正意义上的空白，跟chat_input本身没包fragment导致的"发消息要
    整页重跑一遍"叠加在一起，体验上完全等同于卡死。这个函数只负责画气泡
    本身，样式跟_chat_bubble的assistant气泡对齐（同一个背景色/圆角），
    调用方在拿到第一个真实字符前用这个占位，拿到后立刻替换掉。
    """
    dots = "".join(
        f"<span style='display:inline-block;width:6px;height:6px;border-radius:50%;"
        f"background:#8a8a92;margin:0 2px;animation:_fa_typing 1.1s ease-in-out {i * 0.15}s infinite'></span>"
        for i in range(3)
    )
    return (
        "<style>@keyframes _fa_typing{0%,60%,100%{opacity:0.25;transform:translateY(0)}"
        "30%{opacity:1;transform:translateY(-3px)}}</style>"
        "<div style='display:flex;justify-content:flex-start;margin:6px 2px'>"
        "<div style='padding:11px 15px;border-radius:16px;background:#f0f1f3'>"
        f"{dots}</div></div>"
    )


def _safe_href(url) -> str:
    """新闻链接专用——_esc() 只做HTML实体转义，不管URL的scheme是什么，理论上
    如果新闻源（财新/富途资讯/东财公告）混进一条 url 字段是 "javascript:..."
    开头，_esc()不会拦下来，<a href='javascript:...'>依然会在用户点击时执行。
    这里只放行 http/https 两种scheme，其它一律换成 "#"（点了没反应，不会跳转
    也不会执行任何东西）——当前几个新闻源可信度都比较高，这只是防御性兜底，
    不是说已经发现过真实的恶意url。
    """
    s = _esc(url)
    if not re.match(r"^https?://", s, re.IGNORECASE):
        return "#"
    return s


def _news_to_summary(news) -> str:
    """喂给AI的新闻摘要——带上日期和分类，不只是光秃秃的标题，不然AI只能看着
    一行标题瞎总结，写不出具体内容，只能说"整体偏利好"这种空话。"""
    if news is None or news.empty:
        return "无相关新闻"
    return "\n".join(
        f"- [{r.get('日期', '') or '未知日期'}] ({r.get('分类', '') or '未分类'}) {r['新闻标题']}"
        for _, r in news.iterrows()
    )


def _render_overall_summary(raw_text: str):
    """总结性分析的展示——把AI输出末尾的[综合评分: 数字]标签解析出来，做成一条
    可视化打分条摆在文字前面，分数一眼看出偏多偏空，不用读完整段文字才知道结论；
    红涨绿跌是这个项目一贯的配色约定，这里偏多用红、偏空用绿，跟涨跌颜色语义保持一致。
    """
    import re
    score = extract_score(raw_text)
    display_text = re.sub(r"\[综合评分[：:]\s*\d{1,3}\]", "", raw_text).strip()

    if score is not None:
        if score >= 65:
            color, zone = UP_COLOR, "偏多"
        elif score <= 35:
            color, zone = DOWN_COLOR, "偏空"
        else:
            color, zone = "#888", "中性"
        st.markdown(
            f"<div style='margin-bottom:14px'>"
            + f"<div style='display:flex;align-items:baseline;gap:8px;margin-bottom:6px'>"
            + f"<span style='font-size:1.6rem;font-weight:700;color:{color}'>{score}</span>"
            + f"<span style='font-size:0.85rem;color:var(--fa-muted)'>/ 100 "
            + f"<span style='color:{color};font-weight:600'>{zone}</span></span>"
            + "</div>"
            + f"<div style='position:relative;height:6px;border-radius:3px;background:linear-gradient(to right,{DOWN_COLOR},#d8d8d8,{UP_COLOR})'>"
            + f"<div style='position:absolute;left:{score}%;top:-4px;width:14px;height:14px;"
            + f"border-radius:50%;background:#fff;border:3px solid {color};transform:translateX(-50%)'></div>"
            + "</div>"
            + "<div style='display:flex;justify-content:space-between;font-size:0.7rem;color:#aaa;margin-top:3px'>"
            + "<span>偏空</span><span>中性</span><span>偏多</span>"
            + "</div>"
            + "</div>",
            unsafe_allow_html=True,
        )
    st.markdown(display_text)


def _display_name(symbol: str, market: str, spot: dict) -> str:
    """给个股详情页AI模块用的展示名（拿去做新闻搜索关键词）——沪深优先用
    get_stock_name(symbol)（BaoStock查到的规范公司名），因为这个名字要
    拿去搜新闻，spot实时快照（Tencent/Futu）里的名称字段有时跟新闻源
    用的公司全称对不上，会影响新闻关键词命中率；港股/美股没有BaoStock
    覆盖，退回spot快照里的名称字段。

    注意：tracker.log_analysis 存的"name"字段跟这个不是同一个口径——那边
    只是历史记录里的展示标签，不需要为了新闻命中率特地查BaoStock规范名，
    直接用spot快照里的名称即可，两处刻意保留了不同的计算，不是遗漏。
    """
    return get_stock_name(symbol) if market == "A" else spot.get("名称", symbol)


def _stream_ai_text(gen, raise_on_error: bool = True) -> str:
    """AI流式输出的统一处理——不直接把生成器丢给st.write_stream(gen)，实测
    生成过程中一旦出错（API瞬时抖动之类），st.write_stream()会把异常悄悄
    吞掉、返回空字符串，缓存进session_state后页面上就是标题下面空空如也，
    连报错都看不见（"总结性分析"最早踩过这个坑）。手动逐块迭代、显式捕获
    异常，绝不会把空字符串当成正常结果存住。

    这里原来是两份几乎一样的代码（_write_stream_safe 给普通AI模块用，
    _stream_overall_summary 给"总结性分析"用），唯一实质区别是失败时怎么
    处理，现在用 raise_on_error 这一个参数统一表达：
    - raise_on_error=True（默认，给资讯解读/财务摘要/交叉验证这些普通模块
      用）：异常/空结果直接往外抛，让调用方自己的try/except接住去展示
      "分析失败"；成功时占位区保留完整文本（调用方不会再另外渲染一遍）。
    - raise_on_error=False（给"总结性分析"用）：异常/空结果不抛出，转成一句
      "汇总失败：..."的文本原样返回；占位区在结束时清空——因为调用方
      _render_overall_summary 会把返回的文本重新渲染成带评分条的样式，
      占位区留着原始文字会跟最终版本重复显示。
    """
    placeholder = st.empty()
    full_text = ""
    try:
        for chunk in gen:
            full_text += chunk
            placeholder.markdown(full_text + "▌")
    except Exception as e:
        if raise_on_error:
            raise
        placeholder.empty()
        return f"汇总失败：{e}"

    if not full_text.strip():
        if raise_on_error:
            raise RuntimeError("AI 没有返回任何内容")
        placeholder.empty()
        return "汇总失败：AI 没有返回任何内容，请点「重新分析」再试一次。"

    if raise_on_error:
        placeholder.markdown(full_text)
    else:
        placeholder.empty()
    return full_text


def _render_news_section(keyword: str, symbol: str | None = None, market: str = "A", is_index: bool = False):
    """一手资讯单独成块，标题不截断——是AI解读的依据来源，放在AI解读前面让用户
    自己先看一手材料。沪深优先用官方公告（监管强制披露，永远免费，比新闻评论
    更"一手"，点进去就是东财公告中心原文，不存在付费墙）；港股/美股没有对应的
    免费公告聚合源，退回财新新闻摘要（有付费墙，已经标注清楚）。

    指数（is_index=True）没有公司名可以精确匹配，用 get_index_news 单独处理——
    优先走富途资讯搜索（真按这个指数的名字搜，免费可读），连不上才退回财新
    严格关键词匹配，匹配不到就如实说没有，不再拿不相关的大盘资讯硬凑（详见
    get_index_news 的说明）。
    """
    st.subheader("最新资讯")

    if is_index:
        try:
            news, idx_source = get_index_news(keyword, limit=8)
        except Exception as e:
            st.caption(f"获取失败：{e}")
            return
        if news is None or news.empty:
            st.caption("暂无相关资讯")
            return
        idx_clickable = idx_source == "futu"
        _hot_names = _get_hot_stock_names()
        for _, r in news.iterrows():
            _title = r["新闻标题"]
            _title_style = _news_title_style(_title, _hot_names)
            _title_html = (
                f"<a href='{_safe_href(r.get('url', ''))}' target='_blank' style='{_title_style};text-decoration:none'>{_esc(_title)}</a>"
                if idx_clickable else f"<span style='{_title_style}'>{_esc(_title)}</span>"
            )
            st.markdown(
                f"<div style='margin:6px 0;font-size:0.9rem'>"
                f"<span style='color:var(--fa-muted);font-size:0.78rem'>{_esc(r.get('日期', '') or '')}</span>　"
                f"{_title_html}　"
                f"<span style='color:var(--fa-muted);font-size:0.75rem'>{_esc(r.get('分类', ''))}</span>"
                f"</div>",
                unsafe_allow_html=True,
            )
        return

    with st.spinner("正在获取最新资讯…"):
        news, source = _fetch_news_items(keyword, symbol, market)
    if news is None or news.empty:
        st.caption("暂无相关新闻")
        return

    clickable = source in ("notices", "futu")
    _hot_names = _get_hot_stock_names()
    for _, r in news.iterrows():
        date = r.get("日期") or ""
        title = r["新闻标题"]
        tag = r.get("分类", "")
        _title_style = _news_title_style(title, _hot_names)
        title_html = (
            f"<a href='{_safe_href(r.get('url', ''))}' target='_blank' style='{_title_style};text-decoration:none'>{_esc(title)}</a>"
            if clickable else f"<span style='{_title_style}'>{_esc(title)}</span>"
        )
        st.markdown(
            f"<div style='margin:6px 0;font-size:0.9rem'>"
            f"<span style='color:var(--fa-muted);font-size:0.78rem'>{_esc(date)}</span>　"
            f"{title_html}　"
            f"<span style='color:var(--fa-muted);font-size:0.75rem'>{_esc(tag)}</span>"
            f"</div>",
            unsafe_allow_html=True,
        )


def _financial_key_rows(fin, market: str = "A") -> list[dict]:
    """把东财的财务摘要原始表压成几行关键指标。

    2026-09-05用户要求"就说关键的就行"。原来这里是 st.dataframe(fin) 直接把
    整张原始表怼在页面上——49列里绝大多数是 SECUCODE / ORG_CODE /
    SECURITY_INNER_CODE / STD_REPORT_DATE 这类内部字段，还有几十行重复的
    会计准则和日期。那是数据库的样子，不是给人看的样子。

    列名在美股和港股之间不一致（归母净利润一个叫 HOLDER_PROFIT、一个叫
    PARENT_HOLDER_NETPROFIT），所以每个指标给一组候选列名依次尝试，取到
    哪个算哪个，取不到就留空——不同市场披露口径本来就有差异，硬凑一个数字
    比留空更糟。
    """
    if fin is None or getattr(fin, "empty", True):
        return []

    def pick(row, *names):
        for n in names:
            if n in row and row[n] is not None:
                v = row[n]
                if str(v).strip() not in ("", "nan", "None", "--"):
                    return v
        return None

    def num(v):
        try:
            f = float(v)
            return f if f == f else None
        except Exception:
            return None

    # 金额带上币种单位（2026-09-11前端审计："479.4亿"没有单位）。
    # 只给能确定的两个市场加：沪深报表一定是人民币，美股一定是美元；港股
    # 刻意留空——大量港股公司用人民币出报表，而这套数据里没有"报表币种"
    # 这个字段（项目早先就踩过一次坑：以为有，实际那个字段给的是交易币种，
    # 见 git 历史里"港股的财报币种一直是错的"那次修复）。宁可不标，也不
    # 标一个可能是错的单位——一个错的币种比没有币种更误导。
    _unit = {"A": "元", "US": "美元"}.get(market, "")

    def money(v):
        f = num(v)
        if f is None:
            return ""
        a = abs(f)
        if a >= 1e8:
            return f"{f / 1e8:,.1f}亿{_unit}"
        if a >= 1e4:
            return f"{f / 1e4:,.0f}万{_unit}"
        return f"{f:,.0f}{_unit}"

    def pct(v):
        f = num(v)
        return "" if f is None else f"{f:+.1f}%"

    def plain(v, digits=2):
        f = num(v)
        return "" if f is None else f"{f:,.{digits}f}"

    rows = []
    for _, r in fin.head(6).iterrows():
        d = r.to_dict()
        period = str(pick(d, "REPORT_DATE", "STD_REPORT_DATE") or "")[:10]
        kind = str(pick(d, "DATE_TYPE") or "")
        rows.append({
            "报告期": (period + (f"（{kind}）" if kind else "")),
            "营业收入": money(pick(d, "OPERATE_INCOME")),
            "营收同比": pct(pick(d, "OPERATE_INCOME_YOY")),
            "毛利率": (lambda f: "" if f is None else f"{f:.1f}%")(num(pick(d, "GROSS_PROFIT_RATIO"))),
            "归母净利": money(pick(d, "HOLDER_PROFIT", "PARENT_HOLDER_NETPROFIT")),
            "净利同比": pct(pick(d, "HOLDER_PROFIT_YOY", "PARENT_HOLDER_NETPROFIT_YOY")),
            "每股收益": plain(pick(d, "BASIC_EPS")),
            "ROE": (lambda f: "" if f is None else f"{f:.1f}%")(num(pick(d, "ROE_AVG", "ROE_YEARLY", "ROE"))),
        })
    return rows


def _render_module(module: str, symbol: str, market: str, hist, spot: dict):
    """AI 模块按需加载：每个模块独立缓存，点开哪个才跑哪个的 AI 调用，不会一次性全跑。

    非AI的部分（原始数据表格/图表/统计指标）每次都重新算一遍——这些本来就有
    @st.cache_data缓存，重算很便宜，不用塞进session_state。真正要缓存的只有
    AI生成的文字：第一次生成时用st.write_stream()流式显示（用户反馈"一下子
    蹦出来"不像实时生成，改成打字机效果），生成完的完整文本存进session_state；
    之后重新渲染这个模块时（比如切换K线周期触发的rerun）直接用session_state
    里存好的文本静态显示，不会又调一次AI、也不会重新流式播放一遍。
    """
    mod_key = f"_detail_mod_{symbol}_{market}_{module}"
    is_fresh = mod_key not in st.session_state

    if module == "news":
        stock_name = _display_name(symbol, market, spot)
        # 原始新闻列表已经在页面上方单独一块展示了（_render_news_section），
        # 这里不重复摆一次，只放AI解读，避免同一份数据在页面上出现两遍。
        if is_fresh:
            with st.spinner("正在获取资讯并生成AI解读…"):
                news, _ = _fetch_news_items(stock_name, symbol, market)
                news_summary = _news_to_summary(news)
                try:
                    ai_text = _stream_ai_text(summarize_news(symbol, news_summary))
                except Exception as e:
                    st.error(f"分析失败：{e}")
                    return
            st.session_state[mod_key] = {"ai_text": ai_text}
        else:
            st.markdown(st.session_state[mod_key]["ai_text"])

    elif module == "financial":
        fin = get_financial_abstract(symbol, market=market)
        if fin is not None and not fin.empty:
            _rows = _financial_key_rows(fin, market=market)
            if _rows:
                _cols = list(_rows[0].keys())
                _html = ["<div style='overflow-x:auto'><table style='border-collapse:collapse;width:100%;"
                         "font-size:0.84rem;font-variant-numeric:tabular-nums'>"]
                _html.append("<tr>" + "".join(
                    f"<th style='text-align:{'left' if c == '报告期' else 'right'};padding:0 12px 8px 0;"
                    f"font-weight:500;font-size:0.74rem;color:var(--fa-faint);"
                    f"border-bottom:1px solid var(--fa-border)'>{_esc(c)}</th>" for c in _cols) + "</tr>")
                for _r in _rows:
                    _tds = []
                    for c in _cols:
                        v = _r[c]
                        _col = "var(--fa-text)"
                        if c.endswith("同比") and v:
                            _col = UP_COLOR if v.startswith("+") else DOWN_COLOR
                        _tds.append(
                            f"<td style='text-align:{'left' if c == '报告期' else 'right'};"
                            f"padding:9px 12px 9px 0;color:{_col};"
                            f"border-bottom:1px solid var(--fa-border)'>{_esc(v)}</td>")
                    _html.append("<tr>" + "".join(_tds) + "</tr>")
                _html.append("</table></div>")
                st.markdown("".join(_html), unsafe_allow_html=True)
            if is_fresh:
                financial_summary = fin.head(10).to_string(index=False)
                st.caption("AI 解读")
                try:
                    with st.spinner("正在生成AI解读…"):
                        ai_text = _stream_ai_text(summarize_financials(symbol, financial_summary))
                except Exception as e:
                    st.error(f"分析失败：{e}")
                    return
                st.session_state[mod_key] = {"ai_text": ai_text}
            else:
                st.caption("AI 解读")
                st.markdown(st.session_state[mod_key]["ai_text"])
        else:
            st.caption("暂无财务数据。")

    elif module == "benchmark":
        end = cn_now().strftime("%Y%m%d")
        start = (cn_now() - timedelta(days=90)).strftime("%Y%m%d")
        benchmark = get_benchmark_history(start, end, market=market)
        bm_name = _BENCHMARK_NAMES[market]
        if benchmark is not None and not benchmark.empty:
            st.plotly_chart(
                build_benchmark_comparison(hist, benchmark, benchmark_name=bm_name),
                use_container_width=True, config=_PLOTLY_CONFIG,
            )
            if is_fresh:
                stock_pct = (float(hist.iloc[-1]["收盘"]) / float(hist.iloc[0]["收盘"]) - 1) * 100
                bm_pct = (float(benchmark.iloc[-1]["收盘"]) / float(benchmark.iloc[0]["收盘"]) - 1) * 100
                st.caption("AI 解读")
                try:
                    with st.spinner("正在生成AI解读…"):
                        ai_text = _stream_ai_text(summarize_benchmark(symbol, stock_pct, bm_name, bm_pct))
                except Exception as e:
                    st.error(f"分析失败：{e}")
                    return
                st.session_state[mod_key] = {"ai_text": ai_text}
            else:
                st.caption("AI 解读")
                st.markdown(st.session_state[mod_key]["ai_text"])
        else:
            st.caption("基准数据暂时获取不到。")

    else:  # "cross" —— 完整交叉验证
        ai_hist = _completed_history_for_ai(hist)
        stats = compute_stats(ai_hist)
        if stats:
            scol1, scol2, scol3, scol4 = st.columns(4)
            scol1.metric("区间收益率", stats.get("区间收益率", "—"))
            scol2.metric("年化波动率", stats.get("年化波动率", "—"))
            scol3.metric("最大回撤", stats.get("最大回撤", "—"))
            scol4.metric("夏普比率(简化)", stats.get("夏普比率(简化)", "—"))

        try:
            _intraday_for_signal = (
                get_stock_intraday_a(symbol) if market == "A" else get_stock_intraday_futu(symbol, market)
            )
        except Exception:
            _intraday_for_signal = None
        realtime_signal = compute_realtime_signal(spot, _intraday_for_signal)
        technical_summary = compute_technical_signal(hist) + " 【盘中实时信号】" + realtime_signal
        st.markdown(f"**技术面信号**：{technical_summary}")

        if hist is not None and not hist.empty:
            st.plotly_chart(build_return_histogram(hist), use_container_width=True, config=_PLOTLY_CONFIG)

        st.caption("AI 解读（交叉验证消息面、财务、技术面是否一致）")
        if is_fresh:
            if ai_hist.empty:
                st.warning("当前没有可用于分析的已完成日线数据，请在收盘后再试。")
                return
            with st.spinner("正在汇总财务/资讯/技术面数据并生成AI解读…"):
                history_summary = ai_hist.tail(20).to_string(index=False)
                if spot and spot.get("最新价"):
                    history_summary += (
                        f"\n\n实时行情快照：最新价{spot['最新价']}，今开{spot.get('今开')}，"
                        f"最高{spot.get('最高')}，最低{spot.get('最低')}，昨收{spot.get('昨收')}"
                    )
                history_summary += "\n\n统计指标：" + "，".join(f"{k}={v}" for k, v in stats.items())

                fin = get_financial_abstract(symbol, market=market)
                financial_summary = (
                    fin.head(10).to_string(index=False) if fin is not None and not fin.empty else "无可用数据"
                )
                stock_name = _display_name(symbol, market, spot)
                news, _ = _fetch_news_items(stock_name, symbol, market)
                news_summary = _news_to_summary(news)

                try:
                    ai_text = _stream_ai_text(
                        cross_validate(symbol, history_summary, financial_summary, news_summary, technical_summary)
                    )
                except Exception as e:
                    st.error(f"分析失败：{e}")
                    return
            current_price = spot.get("最新价") or float(hist.iloc[-1]["收盘"])
            verdict = extract_verdict(ai_text)
            # log_analysis存的"name"只是历史记录里的展示标签，口径跟上面
            # _display_name（专门为了新闻搜索命中率查BaoStock规范名）刻意
            # 不同——这里不需要那么讲究，直接用spot快照里的名称即可。
            log_name = spot.get("名称", symbol) if spot else symbol
            # 游客模式没有真实身份，不落库——"历史回看"/一致率追踪本来就是
            # 挂在账号下的个人功能，游客只看这次分析结果，不生成历史记录。
            if st.session_state.get("logged_in"):
                log_analysis(
                    st.session_state["user_email"], symbol, float(current_price), ai_text,
                    verdict=verdict, market=market, name=log_name,
                )
            st.session_state[mod_key] = {"ai_text": ai_text}
        else:
            st.markdown(st.session_state[mod_key]["ai_text"])


# 价格跳动时的一闪。三处改动：
# 1. 颜色原来硬编码成 rgba(224,32,32) / rgba(34,160,107)，是2026-09-04换色板
#    之前的旧红旧绿，跟现在页面上其它地方的涨跌色已经不是同一个色号了。改成
#    从 theme.py 的 UP_COLOR/DOWN_COLOR 换算，以后改色板这里自动跟着走。
# 2. 透明度 0.28 -> 0.13。0.28 在这套近乎无色的界面里是一整块明显的色斑，
#    "有变化"这个信息不需要这么大的动静；淡一半仍然一眼看得到，但不再是
#    页面上最抢眼的东西。
# 3. 从纯背景色改成从左往右的渐隐，并加上圆角——整块方形色块亮起来很生硬，
#    带一点方向感的渐隐更像"数字刚跳过一下"而不是"这一格被选中了"。
def _flash_css() -> str:
    up = _hex_to_rgba_css(UP_COLOR, 0.13)
    down = _hex_to_rgba_css(DOWN_COLOR, 0.13)
    return (
        "<style>"
        f"@keyframes priceFlashUp {{ 0% {{ background: linear-gradient(90deg, {up}, transparent); }}"
        " 100% { background: transparent; } }"
        f"@keyframes priceFlashDown {{ 0% {{ background: linear-gradient(90deg, {down}, transparent); }}"
        " 100% { background: transparent; } }"
        ".price-flash-up { animation: priceFlashUp 1.1s ease-out; border-radius: 6px; }"
        ".price-flash-down { animation: priceFlashDown 1.1s ease-out; border-radius: 6px; }"
        "</style>"
    )


def _hex_to_rgba_css(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


_PRICE_FLASH_CSS = None  # 延迟到第一次用的时候再生成，见下面的 _price_flash_css()


def _price_flash_css() -> str:
    global _PRICE_FLASH_CSS
    if _PRICE_FLASH_CSS is None:
        _PRICE_FLASH_CSS = _flash_css()
    return _PRICE_FLASH_CSS


def _fragment_alive(kind: str) -> bool:
    """自动刷新的 fragment 每次重跑前先问一句：我这块还在当前页面上吗？

    2026-09-05系统性处理"残影"和卡顿。Streamlit 的 @st.fragment(run_every=N)
    定时器在用户切走页面之后并不会停——它继续按点触发、继续往一个已经不属于
    当前页面的容器里重绘。两个后果：一是屏幕上留下上一个页面的碎片（本项目
    注释里反复记过的"切到持仓页面残留"，当时的处理是把几个 fragment 的
    run_every 撤掉，那是回避不是解决）；二是每次空转都真的去打一次富途批量
    行情，而富途的限流是"每30秒最多60次"，几个页面的定时器叠在一起空转，
    很容易把额度吃掉，反过来让还在看的那个页面卡住。

    这里统一加一道守卫：不属于当前上下文就立刻返回，什么都不画。既消掉残影，
    也把切走之后的无效请求全部省掉。
    """
    on_detail = bool(st.session_state.get("_detail_symbol"))
    on_index = bool(st.session_state.get("_index_detail_code"))
    on_sector = bool(st.session_state.get("_sector_detail_name"))
    sec = st.session_state.get("_active_section")
    if kind == "detail":
        return on_detail
    if kind == "index":
        return on_index
    # 分区级的块：任何详情页打开时都不该再刷新
    if on_detail or on_index or on_sector:
        return False
    if kind == "positions":
        return sec in ("持仓", "自选")
    if kind == "sim":
        return sec == "AI模拟炒股"
    return True


@st.fragment(run_every=3)
def _fmt_big(v: float | None, unit: str = "") -> str:
    """大数字换单位。市值动辄上万亿，原样打印一串数字没人读得出来。"""
    if v is None:
        return "—"
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1e12:
        return f"{sign}{a / 1e12:,.2f}万亿{unit}"
    if a >= 1e8:
        return f"{sign}{a / 1e8:,.2f}亿{unit}"
    if a >= 1e4:
        return f"{sign}{a / 1e4:,.2f}万{unit}"
    return f"{sign}{a:,.0f}{unit}"


def _render_key_metrics(spot: dict, market: str):
    """个股详情页的关键数据栏（升级路线图第6条"个股详情页补齐"）。

    2026-09-12。路线图点名缺的是：昨收、成交量、换手率、总市值、PE(TTM)、PB、
    股息率、52周高低。查下来其中大部分 Futu 快照本来就返回，2026-09-01 为了
    给AI模拟盘用已经接进 _futu_snapshot_row_to_dict 了，只是详情页从来没渲染
    ——这一页之前只有最高/最低/今开三个数。这次补上成交量/总市值/股息率/振幅
    四个映射，其余直接用现成字段。

    沪深走的是腾讯那条路径（不是Futu），拿不到估值类字段；缺的项直接不显示，
    不用"—"占位撑出一堆空格子——那会让人以为是加载失败。
    """
    rows: list[tuple[str, str]] = [
        ("最高", f"{spot['最高']:.2f}" if spot.get("最高") else None),
        ("最低", f"{spot['最低']:.2f}" if spot.get("最低") else None),
        ("今开", f"{spot['今开']:.2f}" if spot.get("今开") else None),
        ("昨收", f"{spot['昨收']:.2f}" if spot.get("昨收") else None),
        ("成交量", _fmt_big(spot.get("成交量"), "股") if spot.get("成交量") else None),
        ("成交额", _fmt_big(spot.get("成交额")) if spot.get("成交额") else None),
        ("换手率", f"{spot['换手率']:.2f}%" if spot.get("换手率") is not None else None),
        ("振幅", f"{spot['振幅']:.2f}%" if spot.get("振幅") is not None else None),
        ("量比", f"{spot['量比']:.2f}" if spot.get("量比") is not None else None),
        ("总市值", _fmt_big(spot.get("总市值")) if spot.get("总市值") else None),
        # PE 为负说明公司在亏损，此时"市盈率"这个比值没有估值含义（越亏损
        # 数值反而越接近0），直接写"亏损"比印一个 -12.3 更诚实。
        ("PE(TTM)", ("亏损" if spot["PE_TTM"] < 0 else f"{spot['PE_TTM']:.2f}")
                    if spot.get("PE_TTM") is not None else None),
        ("PB", f"{spot['PB']:.2f}" if spot.get("PB") is not None else None),
        ("股息率", f"{spot['股息率TTM']:.2f}%" if spot.get("股息率TTM") else None),
    ]
    cells = "".join(
        f"<div><div style='font-size:0.72rem;color:var(--fa-faint)'>{_esc(k)}</div>"
        f"<div style='font-size:0.92rem;font-weight:600;color:var(--fa-text);"
        f"font-variant-numeric:tabular-nums'>{_esc(v)}</div></div>"
        for k, v in rows if v
    )
    if cells:
        st.markdown(
            "<style>.fa-keymetrics{display:grid;grid-template-columns:repeat(5,1fr);"
            "gap:12px 14px;margin:6px 0 4px}"
            "@media (max-width:640px){.fa-keymetrics{grid-template-columns:repeat(3,1fr)}}</style>"
            f"<div class='fa-keymetrics'>{cells}</div>",
            unsafe_allow_html=True,
        )

    # 52周区间条。给的是一个"现在贵不贵"的空间感：只报52周最高/最低两个数字，
    # 读者还得自己算现价落在哪儿；画成一条带游标的线，一眼就知道是在高位还是
    # 低位。这跟打分里"价格位置"那一维用的是同一个概念，前后对得上。
    lo, hi, last = spot.get("52周最低"), spot.get("52周最高"), spot.get("最新价")
    if lo and hi and last and hi > lo:
        pos = max(0.0, min(1.0, (last - lo) / (hi - lo)))
        st.markdown(
            f"<div style='margin:10px 0 2px'>"
            f"<div style='display:flex;justify-content:space-between;"
            f"font-size:0.72rem;color:var(--fa-faint);margin-bottom:4px'>"
            f"<span>52周最低 {lo:.2f}</span>"
            f"<span>处于 {pos:.0%} 分位</span>"
            f"<span>52周最高 {hi:.2f}</span></div>"
            f"<div style='position:relative;height:4px;border-radius:2px;"
            f"background:linear-gradient(to right,var(--fa-border),#C6C9CD)'>"
            f"<div style='position:absolute;left:{pos * 100:.1f}%;top:-3px;"
            f"width:2px;height:10px;background:var(--fa-text);"
            f"transform:translateX(-1px)'></div></div></div>",
            unsafe_allow_html=True,
        )


def _render_price_header(symbol: str, market: str):
    """价格区块单独做成 fragment，每3秒自己刷新，不带动AI模块、新闻这些重的部分
    一起重跑——之前全页面每30秒整体rerun一次，观感上像"每隔一阵闪一下"，跟
    同花顺那种数字持续跳动的实时感完全不一样。数字真变了就闪一下背景色，
    让"活着"这件事肉眼可见，不是纯靠脑补更新时间戳。
    """
    if not _fragment_alive("detail"):
        return

    try:
        spot = get_stock_realtime(symbol, market=market)
    except Exception:
        spot = {}
    if not (spot and spot.get("最新价")):
        st.caption("实时价格暂时取不到。")
        return

    change = spot["最新价"] - spot.get("昨收", spot["最新价"])
    change_pct = change / spot["昨收"] * 100 if spot.get("昨收") else 0
    color = UP_COLOR if change >= 0 else DOWN_COLOR

    flash_key = f"_last_price_{symbol}_{market}"
    prev = st.session_state.get(flash_key)
    st.session_state[flash_key] = spot["最新价"]
    flash_class = ""
    if prev is not None and prev != spot["最新价"]:
        flash_class = "price-flash-up" if spot["最新价"] > prev else "price-flash-down"

    st.markdown(
        _price_flash_css()
        + f"<div class='{flash_class}' style='margin:12px 0;padding:4px 8px;border-radius:6px'>"
        + f"<span style='font-size:2rem;font-weight:700;color:{color}'>{spot['最新价']:.2f}</span>&nbsp;&nbsp;"
        + f"<span style='font-size:1.1rem;color:{color}'>{change:+.2f} ({change_pct:+.2f}%)</span>"
        + "</div>",
        unsafe_allow_html=True,
    )
    _src = "Futu 报价" if spot.get("数据源") == "Futu实时" else "延迟报价"
    st.caption(f"{_src} · {_quote_market_status(spot, market)}")
    _render_key_metrics(spot, market)

    if not st.session_state.get("logged_in"):
        st.caption("登录后可关注/管理个人持仓")
    else:
        _tracked_now = is_position_tracked(st.session_state["user_email"], symbol)
        if _tracked_now:
            if st.button("取消关注", key="pos_toggle"):
                delete_position(st.session_state["user_email"], symbol)
                st.rerun()
        else:
            if st.button("关注", key="pos_toggle"):
                add_watch_only(st.session_state["user_email"], symbol, spot.get("名称", symbol), market=market)
                st.rerun()


@st.fragment(run_every=3)
def _render_index_price_header(name: str, market: str):
    """指数版的实时价格区块，逻辑跟_render_price_header一样，独立的 fragment。"""
    if not _fragment_alive("index"):
        return

    try:
        idx_snap = next((i for i in get_multi_index_snapshot(market) if i["名称"] == name), None)
    except Exception:
        idx_snap = None
    if not idx_snap:
        st.caption("实时行情暂时取不到。")
        return

    color = UP_COLOR if idx_snap["涨跌"] >= 0 else DOWN_COLOR
    flash_key = f"_last_price_idx_{name}_{market}"
    prev = st.session_state.get(flash_key)
    st.session_state[flash_key] = idx_snap["最新"]
    flash_class = ""
    if prev is not None and prev != idx_snap["最新"]:
        flash_class = "price-flash-up" if idx_snap["最新"] > prev else "price-flash-down"

    st.markdown(
        _price_flash_css()
        + f"<div class='{flash_class}' style='margin:12px 0;padding:4px 8px;border-radius:6px'>"
        + f"<span style='font-size:2rem;font-weight:700;color:{color}'>{idx_snap['最新']:,.2f}</span>&nbsp;&nbsp;"
        + f"<span style='font-size:1.1rem;color:{color}'>{idx_snap['涨跌']:+.2f} ({idx_snap['涨跌幅']:+.2f}%)</span>"
        + "</div>",
        unsafe_allow_html=True,
    )


def _inject_pos_card_css():
    """pos-card-link 这个class的样式——多个板块（持仓/成分股/涨跌停池/核心股
    榜）共用同一个class做卡片点击跳转，样式只需要注入一次，但每个板块渲染时
    不一定确定其它板块的注入代码有没有跑过，重复调用这个函数是幂等的，
    不会有副作用。
    """
    st.markdown(
        "<style>"
        "a.pos-card-link, a.pos-card-link:link, a.pos-card-link:visited {"
        "  text-decoration: none !important; color: inherit !important;"
        "  display: block; cursor: pointer;"
        "}"
        "a.pos-card-link:hover { opacity: 0.85; }"
        "</style>",
        unsafe_allow_html=True,
    )


def _render_stock_movers_cards(df, market: str):
    """把一份"代码/名称/最新价/涨跌幅"的行情表渲成一叠可点击卡片（红涨绿跌，
    点击跳去那只股票详情页）——涨跌停池、港股/美股核心股榜、指数成分股都是
    这个形态，抽成公共函数不用每处各写一遍。df为空时调用方自己处理提示语，
    这里不管。

    价格变了背景闪一下红/绿：跟持仓列表(_render_position_rows)、详情页
    价格区块(_render_price_header)同一套_PRICE_FLASH_CSS机制，用户反馈"行情
    里的股票也要有这个效果"——调用方（涨跌停池/港股核心股/美股核心股这几个
    fragment）都已经是run_every=3自动刷新，数据变了这里自然就能跟着闪。
    """
    _inject_pos_card_css()
    st.markdown(_price_flash_css(), unsafe_allow_html=True)
    for _, row in df.iterrows():
        mv_symbol = str(row["代码"])
        mv_color = UP_COLOR if row["涨跌幅"] >= 0 else DOWN_COLOR
        href = (
            f"?open_symbol={urllib.parse.quote(mv_symbol)}"
            f"&open_market={urllib.parse.quote(market)}"
            f"&open_name={urllib.parse.quote(str(row['名称']))}"
            f"{_auth_qs()}"
        )
        flash_key = f"_mv_last_price_{mv_symbol}_{market}"
        prev = st.session_state.get(flash_key)
        st.session_state[flash_key] = row["最新价"]
        flash_class = ""
        if prev is not None and prev != row["最新价"]:
            flash_class = "price-flash-up" if row["最新价"] > prev else "price-flash-down"
        # 跟指数行同一次改动（2026-09-04）：这批行（涨跌停池、港美股核心股、
        # 指数成分股共用这一段）原来也是 st.container(border=True) 的白底卡片，
        # 跟持仓、排行榜那套"透明底加一条底部发丝线"的扁平行不是一个长相。
        # 用户要求"改到位啊，下面热门股票和板块也一起改掉"。
        with st.container(key=f"mv_row_{market}_{mv_symbol}"):
            st.markdown(
                f"<a class='pos-card-link' href='{href}' target='_self'>"
                f"<div class='fa-flex-row {flash_class}' style='display:flex;align-items:center;border-radius:4px'>"
                f"<div style='flex:2;font-weight:600;color:var(--fa-text);text-decoration:none'>"
                f"{_esc(_clean_name(row['名称']))}（{_esc(mv_symbol)}）</div>"
                f"<div style='flex:1;text-align:right;font-weight:600;color:{mv_color}'>{_fmt_price(row['最新价'])}</div>"
                f"<div style='flex:1;text-align:right;color:{mv_color}'>{row['涨跌幅']:+.2f}%</div>"
                f"</div></a>",
                unsafe_allow_html=True,
            )


_HSTECH_ASOF = "2026-06-08"  # _HSTECH_CONSTITUENTS名单的生效日期，手动维护，见data_sources.py里的说明


def _render_index_top_movers(market: str, index_name: str = ""):
    """指数详情页的"成分股"板块。宽基指数（上证/深证/创业板/恒生/国企/标普/
    纳指/道琼斯）用get_index_top_movers那套"这个市场涨幅最大的股票"代理
    指标——不是严格的官方成分股清单，但对宽基指数勉强说得过去。

    "恒生科技"这种行业主题指数走单独的真实成分股路径（get_hstech_
    constituents）：之前也用这套代理指标，结果混进了优然牧业（乳业）、
    布鲁可（玩具）这些跟科技毫不相关的公司；改成完全不展示后用户又反馈
    "之前虽然错但至少有内容，现在啥都没有"——两头都不是想要的效果，最后
    查了真实的恒生科技成分股名单手动维护起来（见data_sources.py），比
    "编数据"和"不展示"都更贴近用户实际想要的东西。默认显示前10，点
    "展开"再显示到前30，卡片点击直接跳去那只股票的详情页。
    """
    if index_name == "恒生科技":
        try:
            movers = get_hstech_constituents(limit=30)
        except Exception:
            movers = None
        if movers is None or movers.empty:
            st.caption("需要 Futu OpenD 连接，暂不可用")
            return
        st.caption(
            f"恒生科技成分股，按当日涨跌幅排。名单截至 {_HSTECH_ASOF}，"
            f"指数公司按季调整，可能与最新官方名单有出入。"
        )
        expand_key = f"_movers_expand_{market}_hstech"
        show_n = 30 if st.session_state.get(expand_key) else 10
        _render_stock_movers_cards(movers.head(show_n), market)
        if len(movers) > 10:
            if not st.session_state.get(expand_key):
                if st.button("展开（前30）", key=f"_movers_expand_btn_{market}_hstech"):
                    st.session_state[expand_key] = True
                    st.rerun()
            else:
                if st.button("收起", key=f"_movers_collapse_btn_{market}_hstech"):
                    st.session_state[expand_key] = False
                    st.rerun()
        return

    try:
        movers = get_index_top_movers(market, limit=30, index_name=index_name)
    except Exception:
        movers = None
    if movers is None or movers.empty:
        st.caption("暂时获取不到数据。")
        return

    # 之前是 f"_movers_expand_{market}"，只带市场不带指数名——同一市场下
    # 切换不同指数（比如沪深的上证指数/深证成指/创业板指）会共享同一个展开
    # 状态，在一个指数里点过"展开"，切到同市场另一个指数也变成展开状态。
    # 恒生科技分支（上面）已经正确带了 "_hstech" 后缀，这里补上指数名区分。
    _movers_qualifier = index_name or "default"
    expand_key = f"_movers_expand_{market}_{_movers_qualifier}"
    show_n = 30 if st.session_state.get(expand_key) else 10
    _render_stock_movers_cards(movers.head(show_n), market)

    if len(movers) > 10:
        if not st.session_state.get(expand_key):
            if st.button("展开（前30）", key=f"_movers_expand_btn_{market}_{_movers_qualifier}"):
                st.session_state[expand_key] = True
                st.rerun()
        else:
            if st.button("收起", key=f"_movers_collapse_btn_{market}_{_movers_qualifier}"):
                st.session_state[expand_key] = False
                st.rerun()


@st.fragment
def _render_index_snapshot(mkt_code: str):
    """"行情"tab顶部的指数快照卡片。做成fragment的原因见_render_a_share_overview
    开头的注释——本质是同一个问题：这几个区块以前全挤在同一段代码里，点其中
    任何一个的交互按钮都会连带其余区块一起重新拉一遍数据。

    之前试过给这个fragment加run_every=3做涨跌闪烁，结果导致"从行情切到
    持仓"出现页面残留——猜测是这个fragment自己的自动刷新定时器切换页面
    后仍在后台继续触发，把已经不该存在的旧内容重新塞回DOM里。残留问题
    优先级更高，撤回run_every，闪烁效果的代码保留（不会触发，也无副作用），
    等找到不会导致残留的方案再说。

    2026-08-30修复第一版：这个页面本身就是要给"较新"的行情数据，get_multi_
    index_snapshot偶尔慢（港股/美股实测出现过接近10秒），点市场切换时
    Streamlit整页rerun，在数据回来之前页面停在上一个市场的旧卡片上、没有
    任何提示，用户会以为看到的还是新选市场的数据，其实是没刷新的旧数字——
    比单纯等待更容易造成误导。加个spinner，至少明确告诉用户"正在查，这
    不是最终结果"。

    2026-08-30修复第二版：跟用户明确确认过，接受首页地图那份预热缓存
    （warm_home_cache.py每分钟更新，最多1分钟旧）的新鲜度用来换稳定秒开，
    这里改成优先读同一份缓存（load_home_map_cache）——三处（首页地图/
    这里/AI咨询窗）现在共用同一份数据，不用各自另开一条查询路径。缓存
    没有或太旧（预热脚本没跑上）才退回上面第一版那套spinner+实时查询。
    """
    cached = load_home_map_cache(max_age_sec=90)
    if cached:
        idx_list = cached["snaps"].get(mkt_code, [])
    else:
        with st.spinner(f"加载{mkt_code}行情..."):
            try:
                idx_list = get_multi_index_snapshot(mkt_code)
            except Exception:
                idx_list = []

    _idx_code_by_name = dict(_MULTI_INDICES.get(mkt_code, []))

    if not idx_list:
        st.caption("指数数据暂时获取不到。")
        return

    st.markdown(
        _price_flash_css()
        + "<style>"
        "a.idx-card-link, a.idx-card-link:link, a.idx-card-link:visited {"
        "  text-decoration: none !important; color: inherit !important;"
        "  display: block; cursor: pointer;"
        "}"
        "a.idx-card-link:hover { opacity: 0.85; }"
        "</style>"
        "<div class='fa-flex-row' style='display:flex;padding:4px 8px;font-size:0.78rem;color:var(--fa-muted)'>"
        "<div style='flex:2.4'>指数</div>"
        "<div style='flex:1;text-align:right'>最新</div>"
        "<div style='flex:1;text-align:right'>涨幅</div>"
        "<div style='flex:1;text-align:right'>涨跌</div>"
        "</div>",
        unsafe_allow_html=True,
    )
    for idx in idx_list:
        color = UP_COLOR if idx["涨跌"] >= 0 else DOWN_COLOR
        idx_code = _idx_code_by_name.get(idx["名称"], "")
        href = (
            f"?open_index_code={urllib.parse.quote(idx_code)}"
            f"&open_index_market={urllib.parse.quote(mkt_code)}"
            f"&open_index_name={urllib.parse.quote(idx['名称'])}"
            f"{_auth_qs()}"
        )
        flash_key = f"_idx_snap_last_{idx['名称']}_{mkt_code}"
        prev = st.session_state.get(flash_key)
        st.session_state[flash_key] = idx["最新"]
        flash_class = ""
        if prev is not None and prev != idx["最新"]:
            flash_class = "price-flash-up" if idx["最新"] > prev else "price-flash-down"
        # 2026-09-04用户反馈"行情那边的按钮没修，要修成透明化的跟其他的保持
        # 一致"。原来用 st.container(border=True)，Streamlit 会画一个白底加
        # 完整边框的卡片——整站别处（持仓、排行榜、成分股）早就统一成了
        # "透明底 + 一条底部发丝线"的扁平行，只有这里还是老样式，三个白框
        # 摞在一起在这套灰白底子上特别扎眼。改成带 key 的容器，复用
        # st-key-idx_row_ 的扁平样式（跟 pos_row_ 同一套）。
        with st.container(key=f"idx_row_{mkt_code}_{idx['名称']}"):
            st.markdown(
                f"<a class='idx-card-link' href='{href}' target='_self'>"
                f"<div class='fa-flex-row {flash_class}' style='display:flex;align-items:center;border-radius:4px'>"
                f"<div style='flex:2.4;font-weight:600;color:var(--fa-text);text-decoration:none'>{_esc(idx['名称'])}</div>"
                f"<div style='flex:1;text-align:right;font-weight:600;color:{color}'>{idx['最新']:,.2f}</div>"
                f"<div style='flex:1;text-align:right;color:{color}'>{idx['涨跌幅']:+.2f}%</div>"
                f"<div style='flex:1;text-align:right;color:{color}'>{idx['涨跌']:+.2f}</div>"
                f"</div></a>",
                unsafe_allow_html=True,
            )


@st.fragment
def _render_a_share_overview():
    """沪深大盘统计+涨停/跌停股池。之前这几块和指数快照、热门板块全部挤在
    "行情"tab同一段代码里——点"显示更多（前30）"这一个按钮，会触发整个
    tab重新rerun，连带指数快照、热门板块这些跟这次点击完全无关的区块也要
    重新拉一遍数据（其中指数快照缓存只有25秒，涨停跌停池等未必命中缓存），
    这是页面交互感觉卡顿的主要原因。拆成独立fragment后，点这个按钮只会
    重新跑这一个区块。

    之前试过加run_every=3做涨跌闪烁，结果导致"从行情切到持仓"出现页面
    残留（这几个fragment自己的定时器猜测在切页后仍在后台触发，把旧内容
    重新塞回DOM）。残留问题优先级更高，撤回run_every，闪烁效果的代码
    保留（不会触发，也无副作用）。
    """
    try:
        breadth = get_market_breadth()
    except Exception:
        breadth = {}
    if breadth:
        bcols = st.columns(6)
        # 家数是"多少只股票"，必须是整数。2026-09-11前端审计：页面上显示成
        # 324.0 / 4862.0，因为上游把它当float传下来、st.metric照单全收。
        # 活跃度是百分比，保留两位并补上%；它的口径是"当日有成交的股票占比"，
        # 光一个数字看不出来量的是什么，加一行说明。
        for col, key in zip(bcols, ["上涨", "下跌", "涨停", "跌停", "平盘", "活跃度"]):
            raw = breadth.get(key)
            if raw is None or raw == "":
                col.metric(key, "—")
                continue
            if key == "活跃度":
                try:
                    col.metric(key, f"{float(raw):.2f}%", help="当日有成交的股票占全市场的比例")
                except (TypeError, ValueError):
                    col.metric(key, str(raw), help="当日有成交的股票占全市场的比例")
            else:
                try:
                    col.metric(key, f"{int(round(float(raw))):,}")
                except (TypeError, ValueError):
                    col.metric(key, str(raw))
        # 接口本来就返回统计时刻，之前没往页面上放——审计提的"每个模块都要
        # 标数据时间"，这一块的数据现成就有，先把有的标上。
        if breadth.get("统计日期"):
            st.caption(f"统计时间 {breadth['统计日期']}")

    st.divider()
    up_col, down_col = st.columns(2)
    show_n = 30 if st.session_state.get("_show_more_limit_pool") else 10
    with up_col:
        st.markdown("**涨停股池**")
        try:
            up_pool = get_limit_pool("up", show_n)
            if up_pool is not None and not up_pool.empty:
                _render_stock_movers_cards(up_pool, "A")
            else:
                st.caption("暂时没有数据。")
        except Exception as e:
            st.caption(f"获取失败：{e}")
    with down_col:
        st.markdown("**跌停股池**")
        try:
            down_pool = get_limit_pool("down", show_n)
            if down_pool is not None and not down_pool.empty:
                _render_stock_movers_cards(down_pool, "A")
            else:
                st.caption("暂时没有数据。")
        except Exception as e:
            st.caption(f"获取失败：{e}")
    if not st.session_state.get("_show_more_limit_pool"):
        if st.button("显示更多（前30）", key="_more_limit_pool"):
            st.session_state["_show_more_limit_pool"] = True
            st.rerun()


@st.fragment
def _render_crypto_overview():
    """虚拟货币行情。

    2026-09-05新增。跟另外三个市场最大的差别是它24小时不休市——周末和
    夜间，行情页的另外三栏全是昨天的死数据，这一栏是唯一还在动的，所以
    顶部直接点出这一点，省得用户对着一个"周六还在变的价格"犯嘀咕。

    刻意不做run_every自动刷新：项目里那几个run_every的fragment是页面残影
    和富途限流的主要来源（见_fragment_alive的注释），而虚拟货币这一栏的
    数据本身带20秒缓存，用户切回来就是新的，不需要再叠一层定时器。
    """
    try:
        df = get_crypto_quotes()
    except Exception:
        df = None

    if df is None or df.empty:
        st.caption("虚拟货币行情暂时取不到。")
        return

    up = int((df["涨跌幅"] > 0).sum())
    down = int((df["涨跌幅"] < 0).sum())
    st.markdown(
        f"<div style='font-size:0.76rem;color:var(--fa-faint);margin:2px 0 10px'>"
        f"24小时连续交易，无涨跌停 · 当前 {len(df)} 个主流币种：{up} 涨 / {down} 跌 · "
        f"均为美元计价</div>",
        unsafe_allow_html=True,
    )

    # 市场结构指标。价格答不了"现在在轮动哪一层"，主导率和ETF流向能答：
    # 主导率下降说明资金在往山寨扩散、风险偏好抬升；ETF净流入是场外增量
    # 资金在进场。这两个比20个币的涨跌幅更能说明市场处在什么阶段。
    try:
        reg = get_crypto_regime()
    except Exception:
        reg = {}
    if reg:
        seg = []
        if reg.get("BTC主导率"):
            seg.append(
                f"<span style='color:var(--fa-text);font-weight:600'>"
                f"BTC主导率 {reg['BTC主导率']:.1f}%</span>"
                f"<span style='color:var(--fa-faint)'>（下降=资金往山寨扩散，"
                f"上升=避险回流BTC）</span>")
        if reg.get("ETF净流"):
            seg.append(
                f"<span style='color:var(--fa-text);font-weight:600'>"
                f"ETF资金流 {reg['ETF净流']}</span>"
                f"<span style='color:var(--fa-faint)'>（场外增量资金的正规入口）</span>")
        if seg:
            st.markdown(
                "<div style='font-size:0.76rem;margin:0 0 14px;line-height:1.9'>"
                + "<br>".join(seg) + "</div>",
                unsafe_allow_html=True)
    _render_stock_movers_cards(df[["代码", "名称", "最新价", "涨跌幅"]], "CC")


def _render_hk_overview():
    """港股南向资金+核心股，独立fragment，原因同_render_a_share_overview
    （包括撤回run_every=3的原因——切到持仓页面残留）。"""
    try:
        south = get_southbound_flow()
    except Exception:
        south = None
    if south:
        _s_color = UP_COLOR if south["净买额"] >= 0 else DOWN_COLOR
        st.markdown(
            f"<div style='margin:4px 0 12px'>南向资金净买额　"
            f"<span style='color:{_s_color};font-weight:700;font-size:1.2rem'>"
            f"{south['净买额']:+.2f}亿</span></div>",
            unsafe_allow_html=True,
        )
    st.markdown("**港股核心股（按涨跌幅排）**")
    try:
        hk_movers = get_hk_famous_movers(15)
        if hk_movers is not None and not hk_movers.empty:
            _render_stock_movers_cards(hk_movers, "HK")
        else:
            st.caption("暂时获取不到数据。")
    except Exception as e:
        st.caption(f"获取失败：{e}")


@st.fragment
def _render_us_overview():
    """美股核心股，独立fragment，原因同_render_a_share_overview
    （包括撤回run_every=3的原因——切到持仓页面残留）。"""
    st.markdown("**美股核心股**")
    try:
        us_movers = get_us_famous_movers(15)
        if us_movers is not None and not us_movers.empty:
            _render_stock_movers_cards(us_movers, "US")
        else:
            st.caption("暂时获取不到数据。")
    except Exception as e:
        st.caption(f"获取失败：{e}")


@st.fragment
def _render_market_extras(market: str):
    """行情页的补充板块：异动榜、热度榜、新股。

    2026-09-05新增。行情页原本有指数、涨跌停池（沪深）、核心股（港美股）、
    热门板块，覆盖的是"整体怎么样"和"板块怎么样"，缺的是"今天哪几支特别不
    一样"。异动榜和热度榜正好补这个：一个按涨跌幅、一个按关注度，两个口径
    挑出来的往往不是同一批——涨得多不一定有人看，热度高不一定在涨，两个都
    列出来，差异本身就是信息。

    美股开盘前额外显示盘前榜：盘前异动是当天开盘方向最早的线索，而这个时段
    正常行情接口是没有数据的。
    """
    import datetime as _dt

    _blocks = []
    try:
        movers = get_market_rank("top_movers", market, count=8)
    except Exception:
        movers = []
    try:
        hot = get_market_rank("hot", market, count=8)
    except Exception:
        hot = []
    # 美东 04:00-09:30 是盘前时段，换算成北京时间大致是 16:00-21:30（夏令时）。
    # 用宽一点的窗口，早出现半小时比漏掉强。
    premarket = []
    if market == "US":
        _h = _dt.datetime.now(_CN_TZ_APP).hour
        if 15 <= _h <= 22:
            try:
                premarket = get_market_rank("us_premarket", count=8)
            except Exception:
                premarket = []

    # 新股清单在这里就取，而不是等到函数末尾——2026-09-13 实测发现的真bug：
    # 原来这一段末尾才取新股，但上面这句 `if not (movers or hot or premarket):
    # return` 在它之前，于是只要异动榜和热度榜都空，整个新股清单就跟着消失。
    # 沪深正好每次都踩中：这个账号没有沪深行情权限，两个榜单都拿不到，所以
    # 行情页沪深那栏的"新股上市"从加上去那天起就没显示过。
    #
    # 这是这个项目里反复出现的同一类问题——一块内容因为**另一块**内容的数据源
    # 失败而静默消失，页面上看不出任何异常，只像是"今天没有新股"。
    try:
        ipos = get_ipo_calendar(market, limit=6) if market in ("HK", "A", "US") else []
    except Exception:
        ipos = []
    # 政府银债/债券不是股票 IPO，不应混进"新股上市"。它们没有同一套招股价、
    # 绿鞋或首日表现语义，展示在这里会和首页认购专区互相矛盾。
    ipos = [ip for ip in ipos if "银债" not in str(ip.get("name") or "")
            and "债券" not in str(ip.get("name") or "")]

    if not (movers or hot or premarket or ipos):
        return

    def _rows(title, items, sub=""):
        st.markdown(
            f"<div style='font-size:0.76rem;color:var(--fa-faint);margin:2px 0 6px'>"
            f"{title}{('　' + sub) if sub else ''}</div>",
            unsafe_allow_html=True,
        )
        for it in items:
            _cp = it.get("change_pct")
            _c = UP_COLOR if (_cp or 0) > 0 else (DOWN_COLOR if (_cp or 0) < 0 else "var(--fa-muted)")
            _price = f"{it['price']:,.2f}" if it.get("price") is not None else ""
            _chg = f"{_cp:+.2f}%" if _cp is not None else ""
            _href = (
                f"?open_symbol={urllib.parse.quote(it['symbol'])}"
                f"&open_market={urllib.parse.quote(it.get('market') or market)}"
                f"&open_name={urllib.parse.quote(it.get('name') or it['symbol'])}"
                f"&open_from={urllib.parse.quote('行情')}{_auth_qs()}"
            )
            st.markdown(
                f"<a class='pos-card-link' href='{_href}' target='_self'>"
                f"<span style='display:flex;align-items:baseline;padding:8px 2px;"
                f"border-bottom:1px solid var(--fa-border)'>"
                f"<span style='flex:2.4;color:var(--fa-text);font-size:0.86rem'>"
                f"{_esc(it.get('name') or it['symbol'])}"
                f"<span style='color:var(--fa-faint);font-size:0.76rem'> {_esc(it['symbol'])}</span></span>"
                f"<span style='flex:1;text-align:right;color:var(--fa-text);font-size:0.84rem'>{_price}</span>"
                f"<span style='flex:1;text-align:right;color:{_c};font-size:0.84rem'>{_chg}</span>"
                f"</span></a>",
                unsafe_allow_html=True,
            )

    # "今日异动"这个标题必须跟着它下面的两个榜单一起有无。上面那句 return 的
    # 条件加进 ipos 之后，沪深会走到这里但两个榜单都是空的——不加这层判断就会
    # 留下一个光秃秃的标题，比不显示更像出了故障。
    if movers or hot:
        st.markdown("<div style='height:26px'></div>", unsafe_allow_html=True)
        st.markdown("**今日异动**")
        _cols = st.columns(2 if (movers and hot) else 1)
        _i = 0
        if movers:
            with _cols[_i]:
                _rows("涨跌幅榜", movers)
            _i += 1
        if hot:
            with _cols[min(_i, len(_cols) - 1)]:
                _rows("热度榜", hot, "按用户关注度排，跟涨跌幅是两个口径")

    if premarket:
        st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)
        _rows("美股盘前异动", premarket, "开盘方向最早的线索")

    # 三个市场都列新股，口径保持一致（用户2026-09-13要求 沪深/美股跟港股对齐）。
    # ipos 在函数开头就取好了，原因见那里的注释。
    if ipos:
        st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)
        # 沪深的 list_time 常常是 N/A（还在申购、上市日未定），这是沪深的正常
        # 状态不是缺数据，标题里说明一下，免得看到一排空日期以为坏了。
        _ipo_note = {
            "A": "新股上市 · 打新中签后才配售，上市日多为待定",
            "HK": "新股上市 · 认购期内可申购",
            "US": "新股上市 · 美股不设散户打新，仅作日程参考",
        }.get(market, "新股上市")
        st.markdown(
            f"<div style='font-size:0.76rem;color:var(--fa-faint);margin:2px 0 6px'>{_esc(_ipo_note)}</div>",
            unsafe_allow_html=True,
        )
        # 币种和叫法都跟着市场走。之前三个市场统一写"招股价"且不带币种，港股
        # 美股并排看分不清 15.00 是港币还是美元；"招股"是港股/沪深的说法，
        # 美股那边首页已经统一叫"发行价"，两处不一致会让人以为是两个字段。
        _ccy = {"A": "", "HK": "HK$", "US": "$"}.get(market, "")
        _price_label = "发行价" if market == "US" else "招股价"
        for ip in ipos:
            _pr = ""
            if ip.get("price_min") and ip.get("price_max"):
                _pr = (f"{ip['price_min']:,.2f}" if ip["price_min"] == ip["price_max"]
                       else f"{ip['price_min']:,.2f}-{ip['price_max']:,.2f}")
            elif ip.get("ipo_price"):
                _pr = f"{ip['ipo_price']:,.2f}"
            st.markdown(
                f"<div style='display:flex;align-items:baseline;gap:10px;padding:8px 2px;"
                f"border-bottom:1px solid var(--fa-border)'>"
                f"<span style='color:var(--fa-faint);font-size:0.78rem;min-width:76px'>"
                # 上市日为空不是缺数据——沪深申购期内上市日本来就没定，
                # 留白会被当成"数据没拉到"，写"待定"才是事实。
                f"{_esc(ip.get('list_date') or '待定')}</span>"
                f"<span style='flex:1;color:var(--fa-text);font-size:0.86rem'>{_esc(ip['name'])}"
                f"<span style='color:var(--fa-faint);font-size:0.76rem'> {_esc(ip['symbol'])}</span></span>"
                f"<span style='color:var(--fa-text-2);font-size:0.8rem'>"
                f"{_esc((_price_label + ' ' + _ccy + _pr) if _pr else '')}</span>"
                f"<span style='color:var(--fa-faint);font-size:0.76rem;min-width:78px;text-align:right'>"
                f"{('每手 ' + format(int(ip['lot_size']), ',')) if ip.get('lot_size') else ''}</span></div>",
                unsafe_allow_html=True,
            )


@st.fragment
def _render_macro_strip():
    """行情页顶部的跨资产温度计：VIX / 美债10年期 / 美元指数 / 黄金 / 原油 / 铜。

    2026-09-12新增（升级路线图第5条，行情页做成"市场全景"）。原来的行情页
    只有股票：指数、板块、异动。但决定今天该不该加仓的，一半以上不在股票
    里——VIX 说的是恐慌程度，美债收益率是所有资产定价的锚，美元强弱直接压
    着黄金和原油。这几个数字看一眼要跳三个网站，放在最上面一行才有意义。

    放在市场单选之上：这一条跟选沪深/港股/美股无关，它是全球共用的背景板。
    挂在某个市场下面会让人误以为"这是美股的VIX"。
    """
    try:
        rows = get_macro_dashboard()
    except Exception:
        rows = None
    if not rows:
        return

    cards = []
    for name, r in rows.items():
        chg, unit = r.get("涨跌"), r.get("单位") or ""
        if chg is None:
            continue
        color = UP_COLOR if chg >= 0 else DOWN_COLOR
        if r.get("口径") == "rate":
            # 收益率：数值本身就是百分数，变动按基点念。"4.96% / +1.3bp"
            # 才是债券的读法；写成"+0.26%"会被当成"美债涨了0.26%"，差两个
            # 数量级。
            value_txt = f"{r['最新']:.2f}{unit}"
            delta_txt = f"{r.get('bp变动', 0.0):+.1f}bp"
        else:
            value_txt = f"{r['最新']:,.2f}"
            delta_txt = f"{r['涨跌幅']:+.2f}%"
        # 单位（美元/盎司、美元/桶…）单独一行小字。收益率那一项的单位是"%"，
        # 已经跟在数值后面了，不重复再写一行。
        unit_html = ""
        if unit and r.get("口径") != "rate":
            unit_html = (
                f"<div style='font-size:0.66rem;color:var(--fa-faint)'>{_esc(unit)}</div>"
            )
        # 整张卡片包在一个真正的 <a> 里，点进宏观详情页。跟项目里推荐股/指数/
        # 持仓卡片是同一套做法：整页导航而不是 st.button——Streamlit 的按钮在
        # 这种密集横排里会各自占一个 block，排版就散了，而且点击要等一次
        # rerun 往返。
        href = f"?open_macro={urllib.parse.quote(name)}{_auth_qs()}"
        cards.append(
            f"<a class='macro-card-link' href='{href}' target='_self'>"
            f"<div style='font-size:0.72rem;color:var(--fa-faint);white-space:nowrap'>"
            f"{_esc(name)}</div>"
            f"<div style='font-size:0.98rem;font-weight:650;color:{color};"
            f"font-variant-numeric:tabular-nums;white-space:nowrap'>{value_txt}</div>"
            f"<div style='font-size:0.72rem;color:{color};"
            f"font-variant-numeric:tabular-nums'>{delta_txt}</div>"
            f"{unit_html}"
            f"</a>"
        )
    if not cards:
        return
    # 等分网格铺满整行（用户2026-09-12反馈"一行塞满间隔一致"）。
    st.markdown(
        "<style>.fa-macro-strip{display:grid;grid-template-columns:repeat(6,1fr);"
        "gap:10px 12px;padding-bottom:12px;border-bottom:1px solid var(--fa-border);"
        "margin-bottom:20px}"
        "@media (max-width:640px){.fa-macro-strip{grid-template-columns:repeat(3,1fr)}}"
        # 卡片链接不要继承正文链接的颜色和下划线，它就是一块可点的区域。
        "a.macro-card-link,a.macro-card-link:link,a.macro-card-link:visited{"
        "display:block;text-decoration:none;border-bottom:none;color:inherit;"
        "padding:4px 6px;margin:-4px -6px;border-radius:7px;"
        "transition:background .12s ease}"
        "a.macro-card-link:hover{background:rgba(23,24,28,0.045)}</style>"
        "<div class='fa-macro-strip'>" + "".join(cards) + "</div>",
        unsafe_allow_html=True,
    )


# 每个宏观品种的"这是什么、为什么要看它"。写死在代码里而不是让AI临时生成：
# 这些是稳定的常识，不会变，没必要每次进页面烧一次token、也没必要冒AI写错的
# 风险（比如把美债收益率和债券价格的方向说反，这种错误初学者看不出来）。
_MACRO_EXPLAIN = {
    "VIX恐慌指数": (
        "芝加哥期权交易所用标普500期权价格反推出来的「未来30天预期波动率」，"
        "俗称恐慌指数。它不预测方向，只衡量市场认为未来会有多颠簸。",
        "经验区间：20以下算平静，20-30是明显不安，30以上通常对应真正的恐慌行情。"
        "它跟股指几乎总是反向——股市暴跌时VIX飙升。看它的意义在于，当VIX很低时"
        "市场对坏消息毫无防备，这种时候的「一切都好」反而最脆弱。",
    ),
    "美债10年期": (
        "美国10年期国债收益率，全球资产定价的锚。几乎所有估值模型的分母里都有它。",
        "注意收益率和债券价格是反向的：收益率涨=债券在被卖。收益率上行会压制"
        "成长股估值（未来现金流折现变少），也会抬高企业融资成本。这里的变动按"
        "基点（bp）计，1bp = 0.01个百分点——这是债券市场的通用读法，"
        "说「涨了0.3%」在这里是有歧义的。",
    ),
    "美元指数": (
        "美元对一篮子主要货币（欧元占比最大，其次日元、英镑等）的加权汇率。",
        "美元强弱直接压制以美元计价的资产：美元走强时，黄金、原油、大宗商品"
        "通常承压，新兴市场资金也倾向流出。持有港股的人尤其要留意——港币挂钩"
        "美元，所以美元的走势就是你这部分仓位的汇率背景。",
    ),
    "黄金": (
        "COMEX黄金期货主力合约，美元/盎司。",
        "传统避险资产，同时也是对实际利率的反向押注：实际利率（名义利率减通胀）"
        "越低，持有不生息的黄金的机会成本越小。所以看黄金要配合上面的美债收益率"
        "和美元指数一起看，单看金价本身很难解释它为什么动。",
    ),
    "原油": (
        "NYMEX WTI原油期货主力合约，美元/桶。",
        "既是能源成本也是需求温度计。油价大涨会推高通胀预期、进而影响利率路径；"
        "而在没有供给冲击的情况下油价下跌，往往是全球需求转弱的早期信号。"
        "区分这两种情形（供给冲击还是需求走弱）比记住油价本身重要得多。",
    ),
    "铜": (
        "COMEX铜期货主力合约，美元/磅。",
        "被称作「铜博士」（Dr. Copper），因为它广泛用于建筑、电网、家电和电动车，"
        "需求变化几乎同步反映全球制造业景气。它是这六个里最纯粹的实体经济指标——"
        "不像黄金那样掺杂避险情绪，也不像原油那样频繁被地缘政治打断。",
    ),
}

_MACRO_RANGE_OPTIONS = {
    "近1月": ("1mo", "1d"),
    "近6月": ("6mo", "1d"),
    "近1年": ("1y", "1d"),
    "近5年": ("5y", "1wk"),
}


def _render_macro_detail(name: str):
    """宏观品种详情页：VIX / 美债10年期 / 美元指数 / 黄金 / 原油 / 铜。

    2026-09-12新增。用户要求首页那条宏观横栏的六个数字要能点进来，
    "跟其他的行情详情界面一样"。

    没有复用 _render_index_detail：那个函数从头到尾绑死在 A/HK/US 三个市场上
    （get_multi_index_snapshot / get_index_intraday_a / get_index_intraday_futu /
    get_index_history 全都只认这三个），而这六个品种在富途那边没有行情权限、
    数据走的是 Yahoo。硬塞进去要给四个函数各开一个 MACRO 分支，改动面比单独
    写一个渲染函数大得多，还会把那条已经稳定的路径搅浑。

    K线图复用 build_candlestick，所以这一页的观感跟个股/指数详情页是一致的。
    """
    if st.button("", icon=":material/arrow_back:", key=f"macro_back_{name}",
                 type="tertiary", help="返回"):
        st.session_state.pop("_macro_detail_name", None)
        st.session_state["_active_section"] = "首页"
        st.rerun()

    try:
        snap = (get_macro_dashboard() or {}).get(name)
    except Exception:
        snap = None

    group = (snap or {}).get("分类", "")
    unit = (snap or {}).get("单位", "")
    st.markdown(
        f"""
        <div style='padding:2px 0 14px;border-bottom:1px solid var(--fa-border);margin-bottom:20px'>
            <div style='font-size:1.34rem;font-weight:650;letter-spacing:-.022em;
                        color:var(--fa-text);line-height:1.3'>{_esc(name)}</div>
            <div style='font-size:.78rem;color:var(--fa-faint);margin-top:4px;
                        letter-spacing:.03em'>{_esc(group)}{(' · ' + _esc(unit)) if unit else ''}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if snap:
        chg = snap.get("涨跌") or 0.0
        color = UP_COLOR if chg >= 0 else DOWN_COLOR
        if snap.get("口径") == "rate":
            value_txt = f"{snap['最新']:.2f}{unit}"
            delta_txt = f"{snap.get('bp变动', 0.0):+.1f}bp"
        else:
            value_txt = f"{snap['最新']:,.2f}"
            delta_txt = f"{chg:+,.2f}（{snap['涨跌幅']:+.2f}%）"
        st.markdown(
            f"<div style='display:flex;align-items:baseline;gap:14px;margin-bottom:6px'>"
            f"<span style='font-size:2.1rem;font-weight:650;letter-spacing:-.02em;"
            f"color:{color};font-variant-numeric:tabular-nums'>{value_txt}</span>"
            f"<span style='font-size:1.1rem;color:{color};"
            f"font-variant-numeric:tabular-nums'>{delta_txt}</span></div>",
            unsafe_allow_html=True,
        )
    else:
        st.caption("实时报价暂时取不到，下面的历史走势仍然可看。")

    st.divider()
    range_label = st.radio("周期", list(_MACRO_RANGE_OPTIONS), index=2,
                           horizontal=True, key=f"_macro_range_{name}")
    rng, interval = _MACRO_RANGE_OPTIONS[range_label]

    with st.spinner("加载K线数据..."):
        try:
            hist = get_macro_history(name, rng, interval)
        except Exception:
            hist = None
    if hist is None or hist.empty:
        st.caption("历史数据暂时取不到。")
    else:
        # VIX、美债收益率、美元指数是算出来的指数不是撮合出来的合约，没有成交量
        # （Yahoo 返回 0）。画一栏全空的柱状图看上去像坏了，所以按数据本身决定
        # 画不画那一栏，而不是按品种名硬编码——万一以后 Yahoo 补了数据，这里
        # 自动就跟上了。
        has_volume = float(pd.to_numeric(hist["成交量"], errors="coerce").fillna(0).sum()) > 0
        st.plotly_chart(
            build_candlestick(hist, show_volume=has_volume),
            use_container_width=True, config=_PLOTLY_CONFIG,
        )

    exp = _MACRO_EXPLAIN.get(name)
    if exp:
        st.divider()
        st.markdown("**这是什么**")
        st.write(exp[0])
        st.markdown("**怎么看**")
        st.write(exp[1])


@st.fragment
def _render_sector_heatmap(market: str):
    """板块热力图：面积=成交额，颜色=涨跌幅。

    2026-09-12接上（升级路线图第5条）。get_sector_heatmap 这个数据源
    2026-09-06 就写好了，但一直没有任何地方渲染，等于白拉一次接口。

    放在"热门板块"列表之前：热力图回答"今天钱往哪个方向走"（一眼看形状），
    列表回答"具体是哪几个板块、涨了多少"（要读数字）。先看形状再读数字，
    顺序上是从面到点的，跟下面"板块→成分股"是同一个收敛方向。
    """
    try:
        df = get_sector_heatmap(market, limit=30)
    except Exception:
        df = None
    if df is None or df.empty:
        return
    fig = build_sector_treemap(df)
    if fig is None:
        return
    st.markdown("**板块热力图**")
    # 必须标明分类口径。审计第11条：沪深这张图里"通信设备 +1.90%"，紧挨着的
    # 「热门板块」写"通信设备 +0.42%"，同一页两个数打架。查下来不是bug——
    # 热力图走富途，板块名带"Ⅱ"是申万二级；热门板块走同花顺，是另一套行业
    # 分类，同名不同成分（美股两处完全一致可以佐证）。但界面一个字都没说，
    # 用户没法知道这是两套分类而不是数据错了。
    _src = "申万二级行业（富途）" if market == "A" else "行业板块（富途）"
    st.caption(
        f"面积=成交额，深浅=涨跌幅度，涨跌看方块上的正负号。"
        f"取成交额前16的板块，分类是{_src}——跟下面「热门板块」不是同一套，数值对不上正常。"
    )
    st.plotly_chart(fig, use_container_width=True, config=_PLOTLY_CONFIG)


@st.fragment
def _render_hot_sectors(market: str):
    """"热门板块"——按热度（成交额代理）排序的行业板块，3×3宫格展示前9名，
    "更多板块"展开到前30。点击板块卡片会跳到该板块的成分股列表（复用
    get_sector_constituents + _render_stock_movers_cards），成分股本身
    再点进去就是已有的个股详情页（走势+AI分析）——板块这一层不需要单独
    造一套K线/AI分析，成分股列表是已有能力的自然延伸。

    做成fragment：点"更多板块/收起"之前会带动整个"行情"tab（指数快照、
    涨停跌停池等）一起重新拉一遍数据，明明只是想展开这一个板块列表。
    """
    try:
        sectors = get_hot_sectors(market, limit=30)
    except Exception:
        sectors = None
    if sectors is None or sectors.empty:
        st.caption("暂时获取不到板块数据；指数和个股行情仍可独立正常更新。")
        return

    expand_key = f"_sectors_expand_{market}"
    show_n = 30 if st.session_state.get(expand_key) else 9
    shown = sectors.head(show_n).reset_index(drop=True)

    # 2026-09-04用户反馈"热门板块的布局也跟前面的内容保持一致，感觉这样的
    # 框框很突兀"。原来是3×3宫格的卡片，即使把白底改成透明，九个带边框的方块
    # 摞在一起跟这一页其余部分（指数、涨跌停池、核心股全是一行一条发丝线的
    # 扁平列表）还是两套语言。改成同一套扁平行：板块名在左，涨跌幅和热度排名
    # 靠右，行与行之间一条发丝线。
    #
    # 顺带解决了宫格本身的一个毛病：三列等宽，但板块名长短差很多（"元件"两个
    # 字和"数码解决方案服务"七个字挤在同宽的格子里），列表布局天然没这个问题。
    for idx, row in shown.iterrows():
        s_color = UP_COLOR if row["涨跌幅"] >= 0 else DOWN_COLOR
        inner = (
            f"<div style='display:flex;align-items:center'>"
            f"<div style='flex:3;font-weight:600;color:var(--fa-text)'>{_esc(str(row['板块']))}</div>"
            f"<div style='flex:1;text-align:right;color:{s_color};font-weight:600'>{row['涨跌幅']:+.2f}%</div>"
            f"<div style='flex:1;text-align:right;color:var(--fa-faint);font-size:0.78rem'>热度第{idx + 1}名</div>"
            f"</div>"
        )
        with st.container(key=f"sector_row_{market}_{idx}"):
            href = (
                f"?open_sector={urllib.parse.quote(str(row['板块']))}"
                f"&open_sector_market={urllib.parse.quote(market)}"
                f"{_auth_qs()}"
            )
            st.markdown(
                "<style>a.sector-card-link, a.sector-card-link:link, a.sector-card-link:visited {"
                "text-decoration:none !important; color:inherit !important; display:block; cursor:pointer;"
                "}</style>"
                f"<a class='sector-card-link' href='{href}' target='_self'>{inner}</a>",
                unsafe_allow_html=True,
            )

    if len(sectors) > 9:
        if not st.session_state.get(expand_key):
            if st.button("更多板块", key=f"_sectors_more_btn_{market}"):
                st.session_state[expand_key] = True
                st.rerun()
        else:
            if st.button("收起", key=f"_sectors_collapse_btn_{market}"):
                st.session_state[expand_key] = False
                st.rerun()


def _render_sector_detail(name: str, market: str):
    """板块详情页——只展示成分股列表，复用_render_stock_movers_cards，每只
    成分股点进去就是已有的个股详情页（走势+AI分析）。板块本身不需要单独的
    K线/AI分析，这里不重新造轮子。
    """
    # 返回键用真正的图标，不用"←"这个文字字形。文字箭头要靠加大字号加粗才看得清，
    # 放大后字形本身的粗细、基线、居中都跟旁边的图标按钮对不齐，是很容易露怯的
    # 一处；换成 material 图标，尺寸和对齐交给统一的图标按钮规格处理。
    if st.button("", icon=":material/arrow_back:", key=f"sector_back_{name}_{market}", type="tertiary", help="返回行情"):
        for k in ("_sector_detail_name", "_sector_detail_market"):
            st.session_state.pop(k, None)
        st.session_state["_active_section"] = "行情"
        st.rerun()

    # 详情页页眉。跟首页一样去掉了通栏红底——标题本身用字号和字重就能站住，
    # 满屏的品牌红反而会把下面真正要看的涨跌红压掉。底部一条细线做分隔。
    st.markdown(
        f"""
        <div style='padding:2px 0 14px;border-bottom:1px solid var(--fa-border);margin-bottom:20px'>
            <div style='font-size:1.34rem;font-weight:650;letter-spacing:-.022em;
                        color:var(--fa-text);line-height:1.3'>{_esc(name)}</div>
            <div style='font-size:.78rem;color:var(--fa-faint);margin-top:4px;
                        letter-spacing:.03em'>{market}股 · 行业板块</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.divider()


    try:
        cons = get_sector_constituents(market, name, limit=30)
    except Exception:
        cons = pd.DataFrame()
    if cons.empty:
        if market == "A":
            st.caption("暂时取不到这个板块的成分股，稍后再试")
        else:
            st.caption("暂时获取不到这个板块的成分股，可能是 Futu 连接暂时不可用，稍后再试。")
        return
    _render_stock_movers_cards(cons, market)


_HOME_MAP_MARKERS = [
    # (指数名, 所在市场, 纬度, 经度, 标签横向偏移px, 标签纵向偏移px)
    #
    # 2026-09-12重做。用户反馈"世界地图上有些点位没对上"——印度SENSEX的字
    # 飘到了阿拉伯海、新加坡STI飘到了中南半岛、富时100飘到了北大西洋。
    #
    # 根因不是坐标错了（下面这些经纬度本来就是对的），是标签的锚点：原来
    # iconAnchor=[34,38] 把锚点放在标签盒子的正下边缘，于是整个38px高的标签
    # 完全悬在坐标点的正上方。zoom=2 下 38px 差不多是10个纬度，等于每个标签
    # 都被系统性地推到了自己城市的北边十度，而地图上又没有任何东西标出真实
    # 位置，读者只能把文字本身当成位置。
    #
    # 改法是把"位置"和"文字"拆开：真实经纬度上画一个小圆点（位置的唯一
    # 事实来源），文字挂在圆点下方，需要避让时只挪文字、不挪点。所以下面
    # 的经纬度全部回到真实交易所城市，一个都不再为了排版而偏移；
    # 后两个字段是纯像素级的标签偏移，只影响文字，不影响圆点。
    #
    # 需要避让的还是那两处：
    #   美国：纽交所和纳斯达克都在曼哈顿，zoom=2 下相距不到1像素，共用一个
    #         圆点是事实不是偷懒。两个标签纵向叠，间距压到刚好不重叠。
    #   欧洲：伦敦和法兰克福只差1.4纬度/8.8经度，标签必然压住，一左一右拉开。
    #
    # 2026-09-12 第二轮：道琼斯按用户要求去掉（美国只留标普500和纳斯达克100），
    # 少一个标签之后美国那一摞从三层变两层，最下面那个不会再被推到加勒比海去
    # ——上一版三层叠下来，纳斯达克的标签离纽约的圆点有160像素，看上去就像
    # "纳斯达克在古巴"。
    #
    # 经纬度这一轮全部核到交易所自己的地址，不再用城市中心近似：
    # 恒生是全图最难摆的一个：香港被上海（正上方29像素）和新加坡（正下方61
    # 像素）夹住，而标签盒有38像素高，往上往下都会贴到别人的圆点上。
    # 用"到自己圆点的距离 vs 到次近圆点的距离"量过三版：
    #   dy=+10 → 标签下沿落在新加坡圆点上方十几像素，看着像标的是新加坡
    #   dy=0   → 45 vs 55，几乎等距，还是分不清归谁
    #   dy=-23 → 平移到圆点左边、垂直居中，自己44/次近59
    # 最后一版：恒生左移到 -30（离自己圆点30、离新加坡57，比值1.9），同时把
    # 上证整个抬到它自己圆点的**上方**（dy=-46，标签下沿正好压在圆点上）。
    #
    # 为什么上证必须抬上去：上海和香港两个圆点只差29像素，而 fitBounds 设了
    # maxZoom:2，桌面端永远是 zoom2，这个间距不会随窗口变宽而拉开。上证的标签
    # 只要还挂在自己圆点下方，它的中心就正好落在香港圆点那条纬度上——量出来
    # 自己29、次近31，比值1.08，等于分不清归谁。抬到上方之后变成 20 vs 50。
    # 上证同时左移6像素，给日经的标签让开8像素。
    # 负的 dy 表示往上抬——"标签挂在圆点下方"这条规则对其余九个都成立，
    # 只有挤在一起的这两个例外。
    ("恒生指数", "HK", 22.2847, 114.1583, -30, -23),    # 港交所 中环交易广场
    ("上证指数", "A", 31.2222, 121.5083, -6, -46),       # 上交所 浦东南路528号
    # 纽交所和纳斯达克共用曼哈顿这一个圆点，两个标签左右分开而不是上下叠。
    # 上一版是纵向叠（dy=0/40），纳斯达克那个落到圆点下方80多像素，在宽屏上
    # 看过去像标在墨西哥——用户反馈"纳斯达克位置太离谱"。左右各让开38像素之后
    # 两个标签都紧贴在圆点下面，中间还留8像素缝。
    ("标普500", "US", 40.7069, -74.0113, -38, 0),        # 纽交所 华尔街11号
    ("纳斯达克100", "US", 40.7069, -74.0113, 38, 0),     # 纳斯达克也在曼哈顿，共用圆点
    ("日经225", "GLOBAL", 35.6837, 139.7788, 18, 0),     # 东交所 日本桥兜町
    # 伦敦和法兰克福只差25像素(zoom2)，标签必须左右让开，但让得太多就会飘到
    # 大西洋上、离自己的圆点太远。±30 是"两个标签不重叠"和"还看得出属于谁"
    # 之间的折中：伦敦标签右缘到法兰克福标签左缘还剩约33像素。
    ("富时100", "GLOBAL", 51.5155, -0.0983, -30, 0),     # 伦交所 Paternoster Square
    ("德国DAX", "GLOBAL", 50.1155, 8.6796, 32, 10),      # 法兰克福交易所 Börsenplatz
    ("印度SENSEX", "GLOBAL", 18.9296, 72.8331, -8, 0),   # 孟买交易所 Dalal Street
    ("巴西IBOVESPA", "GLOBAL", -23.5475, -46.6340, 0, 0),# B3 圣保罗
    ("澳大利亚ASX200", "GLOBAL", -33.8650, 151.2094, 10, 0),  # 澳交所 悉尼
    ("新加坡STI", "GLOBAL", 1.2789, 103.8536, 0, 6),     # 新交所 Shenton Way
]

# 恒生指数/上证指数/标普500/纳斯达克100这4个能查到腾讯行情接口
# (qt.gtimg.cn) 对应的代码——实测这个接口带 access-control-allow-origin: *
# 响应头，浏览器JS可以直接跨域fetch，不用经过我们自己的Streamlit后端。
# 东财/Yahoo那7个国际指数没有这个响应头，浏览器直接fetch会被CORS拦下来，
# 只能停留在"页面加载时的服务端快照"，做不到这几个的秒级实时刷新——
# 这是浏览器安全机制的硬限制，不是不想做。（道琼斯按用户要求去掉了，
# 换成保留纳斯达克100，美股只留标普+纳斯达克两个。）
_HOME_MAP_TENCENT_CODE = {
    "恒生指数": "r_hkHSI", "上证指数": "r_sh000001",
    # 2026-09-12：道琼斯按用户要求从地图上去掉（"美国我们就留个纳斯达克100和
    # 标普500好了"），这里也一并移除——留着只会让轮询白拉一次它的行情，而
    # 地图上已经没有对应的 marker 可以更新，tcMarkers 里根本查不到这个名字。
    # _MULTI_INDICES 里的道琼斯不动，行情页和指数详情页照常有它。
    "标普500": "usINX", "纳斯达克100": "usNDX",
}

def _render_home_map():
    """首页世界地图——Leaflet.js + OpenStreetMap 免费瓦片（不需要API key/信用卡），
    在几个指数所在交易所城市的真实经纬度上放小图标，图标里显示指数名+当前点数+
    涨跌幅（红涨绿跌）。

    恒生指数/上证指数/标普500/纳斯达克100这4个走浏览器JS直接每3秒
    fetch腾讯行情接口（见_HOME_MAP_TENCENT_CODE的说明），原地更新图标，
    不牵扯Streamlit的rerun——这样既有实时跳动效果，又不会重蹈"行情"tab
    那几个卡片加run_every=3导致切页残留的覆辙（那次是Python侧的fragment
    定时器在背后继续触发；这次刷新完全在iframe内部的JS里自己完成，跟
    Streamlit的脚本重跑机制没有任何关系，理论上不会有同类残留风险）。
    其余7个国际指数（Yahoo Finance源，CORS不开放）保持页面加载时的
    服务端快照，不会跳动。道琼斯没有单独放在地图标记里（见
    _HOME_MAP_MARKERS 的说明，美股只保留标普+纳斯达克两个）。

    2026-08-30修复第一版：原来这里直接调get_multi_index_snapshot（3秒缓存，
    内部又是刻意串行拉，见该函数docstring），三个市场顺序叠加，实测
    经常20秒以上打不开首页——而且这段等待期间是一片空白，连个转圈动画
    都没有，用户反馈"很多地方长时间显示加载中，卡死在那边很丑"。这正是
    assistant.py早先复用错函数踩过的同一个坑（见get_multi_index_
    snapshot_slow的docstring）——首页地图跟AI助手一样，都是"打开时
    查一次快照，不需要3秒级跳动"的场景（4个核心指数本来就有独立的
    浏览器JS每3秒直接跳动，不依赖这次服务端快照），换成60秒缓存的
    _slow版本+并发（用已有的_run_concurrent_with_deadline，8秒截止），
    一个市场慢/卡不连累另外两个。这一版把20秒压到3秒左右，但"3秒"仍然是
    每个访问者都要付的成本。

    2026-08-30修复第二版：加了warm_home_cache.py，每分钟由系统crontab
    独立跑一次（不是Streamlit进程自己的定时任务），把三个市场+国际指数
    提前查好写进data/home_map_cache.json。这里改成优先读这个文件——
    读到90秒以内的新鲜数据就直接用，完全不用等网络，只有文件缺失/太旧
    （预热脚本没跑上）时才退回上面第一版那套并发查+8秒截止的兜底路径，
    不会比修复前更差。详见warm_home_cache.py开头的说明（包括这么做的
    代价：预热脚本会不看有没有真人访问都固定按分钟去查免费接口）。
    """
    snaps: dict[str, list[dict]] = {}
    global_idx: dict = {}
    cached = load_home_map_cache(max_age_sec=90)
    if cached:
        snaps, global_idx = cached["snaps"], cached["global_idx"]
    else:
        with st.spinner("加载全球指数..."):
            snap_results = _run_concurrent_with_deadline(
                ["A", "HK", "US"], get_multi_index_snapshot_slow, timeout=8, max_workers=3
            )
        snaps = dict(zip(["A", "HK", "US"], [snap_results.get(i, []) for i in range(3)]))
        try:
            global_idx = get_global_indices()
        except Exception:
            global_idx = {}

    # 地图图标点进对应指数详情页——只有恒生指数/上证指数/标普500/纳斯达克100这4个
    # 有真正的详情页数据支撑（_MULTI_INDICES里的A/HK/US市场，K线/成分股/AI分析全套都有）。
    # 其余7个国际指数走的是Yahoo Finance(get_global_indices)，现有详情页架构
    # (_render_index_detail/get_multi_index_snapshot)只认A/HK/US这三个市场，
    # 没有对应的K线/成分股数据源，硬点进去打不开一个能用的详情页，所以先只给
    # 这4个能查到code的指数加跳转，其余7个先保持不可点击。
    href_by_name: dict[str, str] = {}
    for name, mkt, _, _, _, _ in _HOME_MAP_MARKERS:
        if mkt == "GLOBAL":
            continue
        code = dict(_MULTI_INDICES.get(mkt, [])).get(name)
        if code:
            href_by_name[name] = (
                f"?open_index_code={urllib.parse.quote(code)}"
                f"&open_index_market={urllib.parse.quote(mkt)}"
                f"&open_index_name={urllib.parse.quote(name)}"
                f"{_auth_qs()}"
            )

    markers_js = []
    marker_coords = []
    dotted = set()
    anchor_by_name: dict[str, list[int]] = {}
    for name, mkt, lat, lon, dx, dy in _HOME_MAP_MARKERS:
        if mkt == "GLOBAL":
            idx = global_idx.get(name)
        else:
            idx = next((i for i in snaps.get(mkt, []) if i["名称"] == name), None)
        if not idx:
            continue
        color = UP_COLOR if idx["涨跌"] >= 0 else DOWN_COLOR
        # 标签尺寸缩小过一版——用户反馈"标签能小点的话就不会挤一起了"，
        # 从 padding 4px 8px/font-size 0.75rem 缩到 2px 5px/0.6rem，
        # iconSize 从 [90,50] 缩到 [68,38]，但没有缩到看不清的程度
        # （名称+点数+涨跌幅三行还是各自独占一行，只是整体更紧凑）。
        # 标签去掉白底、边框和投影，直接把字写在地图上。原来每个指数都是一个
        # 带边框带阴影的小白卡，十一个白卡浮在一张全彩地图上，卡片本身成了
        # 主要的视觉噪声。底图换成近白的 Positron 之后，深色字直接压在上面
        # 就够清楚了；再加一层白色文字描边兜底，保证落在海面或深一点的地块上
        # 也读得清。
        inner = (
            f"<div style='padding:1px 3px;font-size:0.62rem;white-space:nowrap;line-height:1.3;"
            f"text-shadow:0 1px 2px rgba(255,255,255,.95),0 0 4px rgba(255,255,255,.9)'>"
            f"<div style='font-weight:600;color:#17181C'>{name}</div>"
            f"<div style='color:{color};font-weight:700'>{idx['最新']:,.2f}</div>"
            f"<div style='color:{color};font-size:0.58rem'>{idx['涨跌幅']:+.2f}%</div>"
            f"</div>"
        )
        marker_coords.append([lat, lon])
        # 真实位置上的小圆点。这是"这个指数在哪"的唯一事实来源——文字可以为了
        # 排版左右挪，圆点不能。墨色不用涨跌红绿：它表达的是位置不是方向，
        # 染成红绿会多出一组跟数字重复、又跟全站黑白灰调子打架的色块。
        # 三个美股指数共用纽约一个坐标，圆点只画一次（它们确实是同一个地方）。
        if (lat, lon) not in dotted:
            dotted.add((lat, lon))
            markers_js.append(
                "L.circleMarker([%s, %s], {radius: 3, color: '#FFFFFF', weight: 1.5,"
                " fillColor: '#17181C', fillOpacity: 1, interactive: false}).addTo(map);"
                % (lat, lon)
            )
        href = href_by_name.get(name)
        if href:
            # target='_top'：这个地图本身渲染在st.components.v1.html的iframe里，
            # 普通<a>点击只会在iframe内部跳转、看不到效果，_top让浏览器在最外层
            # 文档导航，才能真正带动Streamlit主页面的query params跳转到详情页。
            label = f"<a href='{href}' target='_top' style='cursor:pointer;text-decoration:none'>{inner}</a>"
        else:
            label = inner
        # iconAnchor 是"图标盒子里的哪个点对准这个经纬度"。x 用 34-dx（盒宽68
        # 的一半，再按需要左右挪）；y 用 -8-dy，负值表示锚点在盒子上边缘之上，
        # 于是整块文字挂在圆点下方 8px 处，而不是像以前那样整个压在点的上方。
        anchor_x = 34 - dx
        anchor_y = -8 - dy
        anchor_by_name[name] = [anchor_x, anchor_y]
        if name in _HOME_MAP_TENCENT_CODE:
            # 存进tcMarkers，供后面的JS轮询按名字找到这个marker原地更新图标。
            markers_js.append(
                "tcMarkers[%s] = L.marker([%s, %s], {icon: L.divIcon({html: %s, className: '', iconSize: [68, 38], iconAnchor: [%s, %s]})}).addTo(map);"
                % (json.dumps(name), lat, lon, json.dumps(label), anchor_x, anchor_y)
            )
        else:
            markers_js.append(
                "L.marker([%s, %s], {icon: L.divIcon({html: %s, className: '', iconSize: [68, 38], iconAnchor: [%s, %s]})}).addTo(map);"
                % (lat, lon, json.dumps(label), anchor_x, anchor_y)
            )

    if not markers_js:
        st.caption("指数数据暂时获取不到，地图先不展示。")
        return

    # 只对"当前正在交易的市场"启用浏览器端的腾讯轮询。
    #
    # 审计第8条：同一个指数，首页地图 7,656.98 / 行情页 7,656.41，纳指也差了
    # 十几点。根因不是缓存，是**两个数据提供商**——行情页走
    # get_multi_index_snapshot（富途/akshare），首页地图这四个核心指数额外挂了
    # 浏览器每3秒直连腾讯的轮询。盘中两家差几分钱无所谓（下一秒就变），收盘后
    # 就变成"静态数据还对不上"，用户来回切会直接怀疑数据。
    #
    # 收盘后停掉轮询，地图就保持服务端那份跟行情页同源的快照，两边自然一致；
    # 盘中照常3秒跳动，功能不打折。
    _sim = __import__("sim_agent")
    try:
        _open_now = set(_sim._open_markets())
    except Exception:
        _open_now = set()          # 拿不到就当全部休市，宁可不跳动也不要不一致
    _marker_market = {n: m for n, m, *_ in _HOME_MAP_MARKERS}
    _pollable = {
        name: code for name, code in _HOME_MAP_TENCENT_CODE.items()
        if _marker_market.get(name) in _open_now
    }
    tencent_codes = list(_pollable.values())
    code_to_name = {v: k for k, v in _pollable.items()}

    map_html = f"""
    <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
    <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
    <style>
      /* 瓦片去色+提亮+压对比，得到一张近白的灰阶底图。saturate(0)负责去色，
         brightness/contrast 把 OSM 那种偏重的米黄陆地推到接近页面底色，
         最后一点 opacity 让它整体退到背景层次，不跟前景数据抢。 */
      #home-map .leaflet-tile-pane {{
          filter: saturate(0) brightness(1.14) contrast(.86);
          opacity: .82;
      }}
      /* 窄屏把标签再调小一档。手机上即使视野已经按 fitBounds 收进来了，
         十二个标签挤在三百多像素宽的图上仍然偏密，字号降下来能明显缓解，
         同时行高压紧、去掉描边的模糊半径以免小字发虚。 */
      @media (max-width: 640px) {{
          #home-map .leaflet-marker-icon > div {{
              font-size: .52rem !important; line-height: 1.2 !important;
              text-shadow: 0 1px 1px rgba(255,255,255,.95) !important;
          }}
          #home-map .leaflet-marker-icon > div > div:last-child {{ font-size: .48rem !important; }}
      }}
      /* 署名条也压淡，它是合规必需但不该有存在感。 */
      #home-map .leaflet-control-attribution {{
          background: rgba(255,255,255,.6) !important; font-size: 9px !important;
      }}
      #home-map .leaflet-control-attribution a {{ color: #A8ABB3 !important; }}
      #home-map .leaflet-control-attribution {{ color: #A8ABB3 !important; }}
    </style>
    <div id="home-map" style="height:420px;border-radius:8px;overflow:hidden"></div>
    <script>
    // 这张图是"一张会自己刷新数字的静态图"，不是可操作的地图——所有交互
    // 全部关掉。用户2026-09-12原话："那个图片我们就定死不要放大缩小移动，
    // 就是一张图上面实时刷新数据，不然我误触的话很难受。"
    //
    // 上一版只关了缩放、留着 dragging:true，结果在触屏和触控板上滑动页面时
    // 很容易把整张图拖偏，而且没有任何"复位"的入口——拖歪了就一直歪着。
    // 现在连拖拽和惯性都关掉，地图永远停在同一个视角；标记上的数字照常
    // 每3秒更新（那条链路在 iframe 内部的 JS 里，跟交互无关，不受影响）。
    var map = L.map('home-map', {{
        scrollWheelZoom: false, doubleClickZoom: false, touchZoom: false,
        boxZoom: false, keyboard: false, zoomControl: false,
        dragging: false, inertia: false, tap: false,
        // zoomSnap:0 允许小数级缩放。默认Leaflet只按整数档缩放，而手机上
        // "整数档2太大、整数档1又太小"，中间没有可选值，只能二选一将就。
        // 缩放交互虽然关了，fitBounds 仍然要按容器宽度算出小数级的缩放。
        zoomSnap: 0, zoomDelta: 0.25,
    }}).setView([12, 25], 2);
    // 光标也改回默认箭头——Leaflet 默认给地图容器挂 grab 手型，暗示"可以拖"，
    // 而现在拖不动，手型会变成一个假承诺。
    map.getContainer().style.cursor = 'default';
    // 底图仍然用免费的 OpenStreetMap 瓦片，但在前端把它整体转成灰阶。
    //
    // 标准 OSM 是蓝海+米黄陆地+彩色路网的全彩底图，跟这套灰白黑的界面撞得很
    // 厉害——一张全彩地图摆在页面最上方，真正该抢眼的涨跌红绿反而被它压住。
    // 先试过换成 CARTO Positron（业界常用的极简灰底图），但实测它现在已经
    // 要 API key 了，不带 key 的瓦片整片打上"API KEY REQUIRED"水印，直接
    // 不可用。与其为一张背景图引入一个需要注册和额度管理的外部依赖，不如
    // 继续用零门槛的 OSM，然后用一层 CSS 滤镜把它去色并提亮——效果就是想要的
    // 那张近白灰底图，而且深浅可以自己调，不受第三方样式变更影响。
    // 滤镜只作用在瓦片层(.leaflet-tile-pane)，我们自己的指数标签在 marker 层，
    // 不会被一起去色，涨跌红绿照常。
    L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
        attribution: '&copy; OpenStreetMap contributors', maxZoom: 8
    }}).addTo(map);
    var tcMarkers = {{}};
    {' '.join(markers_js)}
    // 标签默认以坐标点为中心(iconAnchor=[34,38]，Leaflet据此设inline的
    // margin-left:-34px)，靠近地图左右边界的标记因此有一半落在容器外，被
    // overflow:hidden裁掉——实测左边"标普500"只剩"谱500"、右边"日经225"
    // 被切掉13px。这里不动标记位置，只把越界的那几个标签沿水平方向推回
    // 容器内（改margin-left即可，等价于临时改iconAnchor的x）。
    // 为什么不用fitBounds自适应：试过，经度跨度在zoom=2下塞不进容器宽度，
    // Leaflet会退到zoom=1，亚洲那一簇指数全糊成一团，比裁切更难看。
    function clampLabels() {{
        var el = document.getElementById('home-map');
        var mapLeft = el.getBoundingClientRect().left, W = el.clientWidth;
        document.querySelectorAll('#home-map .leaflet-marker-icon').forEach(function(ic) {{
            // 每个标签的默认 margin-left 现在各不相同（Leaflet 按各自的
            // iconAnchor.x 设，而锚点带了每个标记自己的横向偏移），不能再像
            // 以前那样一律还原成 -34px——那会把所有标签的避让偏移抹平，
            // 伦敦和法兰克福立刻叠回一起。第一次见到这个元素时把 Leaflet 设
            // 的原始值记在 dataset 上，之后每次都还原到它再量。
            // setIcon 会整个换掉 DOM 元素，新元素没有 dataset，自然会重新
            // 从 Leaflet 的新值捕获一次，不用手动失效。
            if (ic.dataset.baseMl === undefined) {{
                ic.dataset.baseMl = ic.style.marginLeft || '-34px';
            }}
            ic.style.marginLeft = ic.dataset.baseMl;
            var base = parseFloat(ic.dataset.baseMl) || -34;
            var b = ic.getBoundingClientRect();
            var left = b.left - mapLeft, right = b.right - mapLeft, shift = 0;
            if (left < 2) shift = 2 - left;
            else if (right > W - 2) shift = (W - 2) - right;
            if (shift) ic.style.marginLeft = (base + shift) + 'px';
        }});
    }}
    clampLabels();
    // 视野按容器宽度自适应。
    //
    // 手机上真实故障（2026-09-04用户截图）：地图固定 setView([12,25], 2)，
    // zoom=2 的世界宽度是1024px，而手机容器只有约350px——也就是说一屏只看得到
    // 约123度经度，而标记本身跨了225度（纽约-74到悉尼151）。绝大多数标记
    // 落在可视范围外，再被下面的 clampLabels 一路往里推，最后全堆在一起：
    // 纳斯达克/标普叠成一团，日经和上证、恒生和印度也各自重叠。
    // （跨度原来是251度，2026-09-12去掉道琼斯[-100]之后西边界收到了纽约。）
    //
    // 改成 fitBounds：让Leaflet按容器实际宽度算出"刚好装下所有标记"的缩放。
    // maxZoom:2 保证桌面上不会比原来更放大（桌面宽度本来就装得下，算出来
    // 就是2，视觉跟之前一致）；窄屏则自动缩到1点几，把整个世界收进来。
    // 这跟早些时候试过又撤掉的那版 fitBounds 不是一回事——那次撤是因为当时
    // 标签还是带边框的大白卡、需要的间距大，fitBounds 会一路缩到 zoom1 让
    // 亚洲那簇糊成一团；现在标签是小号透明文字，占地小得多，加上下面窄屏
    // 还会再调小字号，缩放降一点不会糊。
    var _bounds = L.latLngBounds({json.dumps(marker_coords)});
    function fitAll() {{
        var narrow = document.getElementById('home-map').clientWidth < 640;
        map.fitBounds(_bounds, {{
            padding: narrow ? [10, 14] : [30, 24],
            maxZoom: 2, animate: false,
        }});
    }}
    fitAll();
    // 容器尺寸变化时让Leaflet重新测量（否则瓦片留白），重新适配视野并收边。
    window.addEventListener('resize', function() {{
        map.invalidateSize(); fitAll(); clampLabels();
    }});
    // 拖动地图会把原本在中间的标记带到边界上，同样要重新收边。
    map.on('moveend', clampLabels);

    var codeToName = {json.dumps(code_to_name)};
    var hrefByName = {json.dumps(href_by_name)};
    var anchorByName = {json.dumps(anchor_by_name)};
    function fmtNum(n) {{ return n.toLocaleString(undefined, {{minimumFractionDigits: 2, maximumFractionDigits: 2}}); }}
    function updateTcMarkers() {{
        fetch('https://qt.gtimg.cn/q={",".join(tencent_codes)}')
            .then(function(r) {{ return r.text(); }})
            .then(function(text) {{
                var needClamp = false;
                text.split(';').forEach(function(line) {{
                    line = line.trim();
                    if (!line) return;
                    var eq = line.indexOf('=');
                    if (eq < 0) return;
                    var code = line.substring(0, eq).replace('v_', '');
                    var name = codeToName[code];
                    if (!name || !tcMarkers[name]) return;
                    var val = line.substring(eq + 1).replace(/^"|"$/g, '');
                    var fields = val.split('~');
                    if (fields.length < 35) return;
                    var last = parseFloat(fields[3]);
                    var changeAmt = parseFloat(fields[31]);
                    var changePct = parseFloat(fields[32]);
                    if (isNaN(last) || isNaN(changePct)) return;
                    var color = changeAmt >= 0 ? '{UP_COLOR}' : '{DOWN_COLOR}';
                    // 这份必须跟上面首次渲染那段保持完全一致的样式，否则
                    // 3秒一次的行情刷新会把这几个标签又变回带框的白卡。
                    var inner = "<div style='padding:1px 3px;font-size:0.62rem;white-space:nowrap;line-height:1.3;"
                        + "text-shadow:0 1px 2px rgba(255,255,255,.95),0 0 4px rgba(255,255,255,.9)'>"
                        + "<div style='font-weight:600;color:#17181C'>" + name + "</div>"
                        + "<div style='color:" + color + ";font-weight:700'>" + fmtNum(last) + "</div>"
                        + "<div style='color:" + color + ";font-size:0.58rem'>" + (changePct >= 0 ? '+' : '') + changePct.toFixed(2) + "%</div>"
                        + "</div>";
                    // 3秒轮询刷新图标时也要重新套上跳转链接，不然刷新一次链接就消失了
                    var href = hrefByName[name];
                    var html = href
                        ? "<a href='" + href + "' target='_top' style='cursor:pointer;text-decoration:none'>" + inner + "</a>"
                        : inner;
                    // 锚点必须用这个标记自己的那一对，不能写死 [34,38]。
                    // 写死的后果是：首屏渲染的锚点是对的，3秒后第一次行情
                    // 刷新就把这四个标记（恒生/上证/标普/纳斯达克）打回旧锚点，
                    // 标签又整个跳到坐标点上方去——页面看起来"过一会儿自己就
                    // 歪了"，而且只歪这四个，非常难查。
                    var anc = anchorByName[name] || [34, -8];
                    tcMarkers[name].setIcon(L.divIcon({{html: html, className: '', iconSize: [68, 38], iconAnchor: anc}}));
                    needClamp = true;
                }});
                // setIcon整个重建了divIcon，Leaflet会把margin-left重置回默认的
                // -34px，边缘那几个标签一刷新就又被裁掉——重建过就重新收一次边。
                if (needClamp) clampLabels();
            }})
            .catch(function(e) {{}});
    }}
    // 全部休市时 codeToName 是空的，不要起这个定时器——否则会每3秒对
    // 'q=' 发一次无效请求，而且拿回来的东西也没有 marker 可以更新。
    if (Object.keys(codeToName).length > 0) {{
        setInterval(updateTcMarkers, 3000);
    }}
    </script>
    """
    _cv1.html(map_html, height=440)


_ADVICE_EMAIL = os.environ.get("ADVISOR_EMAIL", "")  # advisor.py 私人脚本写advice表时用的固定账号，跟当前登录访客无关
_ADVICE_ACTION_COLOR = {"买入": UP_COLOR, "卖出": DOWN_COLOR, "持有": NEUTRAL_COLOR, "观望": NEUTRAL_COLOR}
# 2026-09-04把判断输出升级成机构研报格式后新增的几段（投资期限/目标价/
# 估值方法/多头逻辑/空头逻辑/关键假设/催化剂/证伪条件）。解析器是按段名定位、
# 位置排序切段的，新增段名向后兼容——2026-09-04之前的老记录没有这些段，
# 解析出来就是没有，不会报错也不会串段。
_ADVICE_SECTIONS = (
    "结论", "投资期限", "目标价",
    # 2026-09-12加进来的两段（前端审计第14条的后续）：它们本来就在AI输出里，
    # 但不在这张段名表里，于是被并进了前一段"目标价"——排行榜那行元信息
    # 因此拖着一整串"维度打分：基本面20/22 · …综合得分：81分"。现在维度分
    # 已经画成迷你条形图了，文字版留在那里纯属重复。列进来只是为了让解析器
    # 把它们切出去（parts里会有这两个键，页面不渲染），不是要显示它们。
    "维度打分", "综合得分", "置信度",
    "基本面", "技术面", "价格位置",
    "多头逻辑", "空头逻辑", "关键假设与催化剂", "证伪条件", "理由",
)

_DISCLAIMER_SENTENCE = "仅供参考，不构成投资建议，请自行判断。"


def _strip_disclaimer(text: str) -> str:
    """剥掉AI每段分析末尾那句固定的免责声明。

    这句是advisor.py的prompt里硬性要求AI附上的（"最后必须附一句"），所以它
    落在数据库里、每条分析都带。单看一条没问题，但排行榜一屏十几张卡并排时
    同一句重复十几遍，重复到一定次数人眼就自动跳过了，反而不如只说一次有效。
    只在"同一屏会出现很多条"的地方剥掉，榜单末尾统一补一次；个股详情页那种
    一次只显示一条的地方不动，保持原样。
    """
    return (text or "").replace(_DISCLAIMER_SENTENCE, "").strip()


def _labeled_line(label: str, value: str) -> str:
    """"标签：内容"这种一行式的小标题，用HTML显式画，不要写成 markdown 的
    `**标签**：内容`。

    2026-09-04踩的坑：全局CSS里把"只含一个<strong>的段落"渲染成了小节眉标
    （display:block、大写字距、灰色），用来统一处理项目里大量的
    st.markdown("**小节标题**")。但CSS的 :only-child 只看元素子节点、不看
    文本节点——`**标签**：内容` 生成的 <p><strong>标签</strong>：内容</p> 里，
    <strong> 依然是唯一的元素子节点，于是被误判成小节标题：标签被顶成独立
    一行，后面那个冒号还孤零零掉到下一行开头。CSS 层面没法区分这两种情况
    （没有"存在文本兄弟节点"这个选择器），所以改在调用侧规避。
    """
    return (
        f"<div style='padding:3px 0;line-height:1.75'>"
        f"<span style='font-weight:600;color:var(--fa-text)'>{_esc(label)}</span>"
        f"<span style='color:var(--fa-text-2)'>：{_esc(value)}</span></div>"
    )


_CN_TZ_APP = timezone(timedelta(hours=8))

_PORTFOLIO_SECTIONS = (
    "总体评估", "集中度风险", "行业集中", "市场敞口",
    "宏观适配", "逐支跟踪", "新增配置建议", "操作建议",
)


def _parse_portfolio_text(text: str) -> dict:
    """把组合分析按段名切开。跟 _parse_advice_text 同一套约定（段名+中文
    冒号），只是段名清单不同——组合分析和单支判断是两种不同的输出格式，
    共用一个段名清单会互相误切。

    段名必须出现在行首才算数：正文里完全可能出现"市场敞口"这四个字（比如
    "市场敞口已经说明了这层风险"），如果不限定行首，那句话会被当成新一段
    的开头，把前一段拦腰截断。
    """
    parts: dict[str, str] = {}
    text = text or ""
    positions = []
    # 同样容忍 markdown 加粗（见 _parse_advice_text 里那条注释：同一个根因
    # 已经在三个地方咬过了）。段名仍然限定在行首——正文里完全可能出现
    # "市场敞口"这四个字，不限行首会把前一段拦腰截断。
    for name in _PORTFOLIO_SECTIONS:
        m = re.search(r"(?:^|\n)\s*\*{0,2}" + re.escape(name) + r"\*{0,2}\s*[：:]", text)
        if m:
            positions.append((m.start(), name, m.end()))
    positions.sort()
    for i, (idx, name, body_start) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        body = text[body_start:end].strip()
        if body:
            parts[name] = body
    return parts


def _clean_ai_markdown(text: str) -> str:
    """把模型输出里的 markdown 残渣清掉，再交给分段解析。

    2026-09-05：换到 DeepSeek-V3 之后，排行榜第4第5名底部出现孤立的 ****，
    "维度打分："后面还会换行，导致原本一行的元信息在页面上裂成两行、跟前
    三名长得不一样。

    这些不是模型"写错了"，是它比之前的模型更爱用 markdown 修饰——提示词里
    要求的是内容格式，修饰符加不加它有自由。与其反复去调提示词赌它听话，
    不如在渲染前把修饰统一清掉：解析和展示都只认纯文本，模型加不加星号都
    得到同样的结果。

    只清修饰，不动内容：孤立的星号行删掉，行首行尾的加粗标记去掉，段名后
    紧跟的换行折回来（"维度打分：\n基本面26/30" -> "维度打分：基本面26/30"）。
    """
    if not text:
        return ""
    out = []
    for line in text.split("\n"):
        stripped = line.strip()
        # 整行只有星号（**、****、***** 这种）：纯粹是没配对的修饰符，删掉
        if stripped and set(stripped) == {"*"}:
            continue
        out.append(line)
    cleaned = "\n".join(out)
    # 段名后面紧跟换行的，把内容折回同一行——否则"维度打分："和数值会分成两行
    cleaned = re.sub(r"([：:])[ \t]*\n+[ \t]*(?=\S)", r"\1", cleaned)
    # 去掉成对但跨行残留的加粗标记
    cleaned = re.sub(r"\*\*(\s*)\*\*", r"\1", cleaned)
    # 落单的加粗标记也要清（2026-09-12，前端审计"AI深度分析里 **+8.55% 的
    # 星号没渲染出来，直接显示了"）。上面那条只处理成对的，模型偶尔会写一个
    # 开头的 ** 却忘了收尾，剩下的这个孤儿标记在按纯文本渲染的地方就会原样
    # 显示出来。按行判断：一行里 ** 出现奇数次说明必然有落单的，整行清掉；
    # 偶数次是正常配对，留着不动（有些调用方是走 st.markdown 渲染的，配对
    # 的加粗在那边是有效果的）。
    cleaned = "\n".join(
        (ln.replace("**", "") if ln.count("**") % 2 else ln)
        for ln in cleaned.split("\n")
    )
    cleaned = _normalize_dimension_line(cleaned)
    return cleaned.strip()


# 六个维度的名字。顺序写死，因为归一化后要按这个顺序输出——AI 列出来的
# 顺序偶尔会变，而排行榜上五张卡片的维度顺序不一致会很扎眼。
_DIM_NAMES = ("基本面", "价格位置", "技术面", "筹码面", "分析师预期", "数据确定性")


def _normalize_dimension_line(text: str) -> str:
    """把"维度打分"那段统一成一行，不管模型写成什么格式。

    2026-09-06 用户反馈"为啥每次的第四名和第五名长得不太一样"。第4名
    (Meta) 是紧凑单行，第5名 (Nutanix) 被渲染成了带括号说明的 markdown
    列表，五张卡片排在一起格式明显不齐。

    根因是模型对格式有自由裁量：提示词要求的是
    "维度打分：基本面X/22 · 价格位置X/20 · ..."，但它有时会写成

        维度打分
        ：- 基本面 16/22（盈利能力改善但高基数下增速或放缓）
        - 价格位置 12/20（52周区间70%分位…）

    这不算写错——六项分数都在，只是加了列表符号和括号注解。反复调提示词
    赌它每次都听话是没有尽头的，不如在渲染前把格式抹平：只要六项分数能被
    提取出来，就重新拼成标准的一行。

    括号里的注解一并丢掉。前三名没有注解，第四五名有，留着一样会造成格式
    不齐；而且那些注解在下面的"多空逻辑/基本面"展开区里本来就有。
    """
    if not text or "维度打分" not in text:
        return text

    # 冒号可能跟"维度打分"之间隔着换行（实测模型会写成"维度打分\n：- 基本面"），
    # 所以这里允许中间有空白。第一版要求冒号紧跟，整段就没匹配上。
    # 先把加粗标记整个抹掉再匹配。模型写过"维度打分**：- 基本面…"这种
    # 单侧星号，_clean_ai_markdown 只处理成对跨行的，漏掉了它，导致段名
    # 正则匹配不上、整段原样返回——五条里有两条因此还是乱的。
    text = text.replace("**", "")

    m = re.search(r"维度打分\s*[：:]", text)
    if not m:
        return text
    head_start, head_end = m.start(), m.end()
    tail = text[head_end:]

    # 这一段到哪结束：遇到"综合得分"为止；没有"综合得分"时退回空行。
    #
    # 不能只用空行做边界：模型有时把每一项的解释写得很长（"基本面 18/22：
    # 营收同比+145%、净利润+405%…"），六项之间还夹着换行，用空行截断会
    # 只切到第一项，后面五项提取不到，函数就认为"认出不足四项"而放弃。
    stop = len(tail)
    mm = re.search(r"综合得分", tail)
    if mm:
        stop = mm.start()
    else:
        mm = re.search(r"\n\s*\n", tail)
        if mm:
            stop = mm.start()
    seg = tail[:stop]
    rest = tail[stop:]

    found = {}
    for name in _DIM_NAMES:
        # 容忍列表符号、加粗标记、名字与数字间的空格、全角斜杠
        mm = re.search(rf"{name}\s*\**\s*[:：]?\s*(\d+)\s*[/／]\s*(\d+)", seg)
        if mm:
            found[name] = (mm.group(1), mm.group(2))

    # 至少认出四项才改写。认出太少说明这段根本不是维度打分，或者模型输出
    # 严重跑偏——那时保持原样比强行拼一个残缺的行要好。
    if len(found) < 4:
        return text

    line = " · ".join(f"{n}{found[n][0]}/{found[n][1]}" for n in _DIM_NAMES if n in found)
    # 补回换行。第一版直接拼 rest，"数据确定性8/10"会跟"综合得分"粘成一行。
    if rest and not rest.startswith("\n"):
        rest = "\n" + rest.lstrip()
    # 段名本身也归一化成"维度打分："。模型偶尔会写成"维度打分\n：..."，
    # 冒号跑到了下一行开头，页面上就会裂成两行——而这正是用户看到的
    # "第四第五名长得不一样"里的一部分。
    return text[:head_start] + "维度打分：" + line + rest


def _score_breakdown_bars_html(verdict_text: str) -> str:
    """把"维度打分"那一行画成六条迷你进度条。

    2026-09-12新增（前端审计第14条："六个维度打分挤在一行12px的灰字里"）。
    六个数字连成一串，要一个个读过去才知道哪一维强哪一维弱；画成条形之后，
    长度就是比例，扫一眼就能看出这支票是靠基本面撑着还是靠技术面撑着。

    分母取每条记录自己解析出来的满分，不写死——2026-09-05权重从四维
    40/30/15/15 改成六维 22/20/20/20/8/10，写死分母的地方当时全线失效过
    一次（见 tracker.extract_score_breakdown 里那段注释）。解析不出来的
    维度直接不画，不用0充数：没解析到和真的0分是两回事。
    """
    try:
        breakdown = extract_score_breakdown(verdict_text or "")
    except Exception:
        return ""
    _labels = (
        ("fundamental", "基本面"), ("price_position", "价格位置"), ("technical", "技术面"),
        ("chips", "筹码面"), ("analyst", "分析师"), ("data_certainty", "数据确定性"),
    )
    bars = []
    for key, label in _labels:
        val, mx = breakdown.get(key), breakdown.get(f"{key}_max")
        if val is None or not mx:
            continue
        pct = max(0.0, min(1.0, val / mx)) * 100
        # 刻意不用 flex + 绝对定位画这条：第一版那么写，在排行榜这个
        # "<a> 包一堆 <div>" 的嵌套结构里 flex 没生效，标签和数值挤在一起、
        # 中间的条整个塌成0宽（实测截图确认）。改成固定宽度的 inline-block
        # 轨道 + linear-gradient 填充，不依赖父容器是不是 flex 容器，也没有
        # 需要定位上下文的绝对定位，在任何外层结构里都能画出来。
        bars.append(
            "<div style='margin-top:3px;line-height:1.5'>"
            f"<span style='display:inline-block;width:60px;font-size:0.7rem;"
            f"color:var(--fa-faint)'>{_esc(label)}</span>"
            f"<span style='display:inline-block;width:120px;height:4px;border-radius:2px;"
            f"vertical-align:middle;background:linear-gradient(to right,"
            f"var(--fa-text-2) 0 {pct:.0f}%,var(--fa-border) {pct:.0f}% 100%)'></span>"
            f"<span style='font-size:0.7rem;color:var(--fa-faint);margin-left:8px;"
            f"font-variant-numeric:tabular-nums'>{val}/{mx}</span></div>"
        )
    if not bars:
        return ""
    return "<div style='margin-top:8px'>" + "".join(bars) + "</div>"


def _parse_advice_text(text: str) -> dict:
    """advisor.py 里 judge_stock() 的输出是固定格式的多段文本（结论/置信度/
    基本面/技术面/价格位置/理由几段），这里按段名切开，首页卡片只挑"理由"
    直接展示（信息量最高、篇幅可控），"基本面/技术面/价格位置"放进展开区，
    不是把整段AI原文糊在首页上——那样篇幅太长，跟首页其它卡片（单行新闻）
    的信息密度不一致。"""
    parts: dict[str, str] = {}
    text = text or ""
    positions = []
    # 用正则而不是 find，为了容忍 markdown 加粗和段名与冒号之间的空格。
    #
    # 2026-09-05真实故障：换到 DeepSeek-V3 之后，它习惯把小节名写成
    # **基本面**：，而原来是拿 text.find("基本面：") 精确找的，星号一挡就找
    # 不到——整段解析失败、退回把原文当 markdown 直接渲染，排行榜第4第5名
    # 于是整篇摊开显示，跟前三名的折叠样式完全不一样。
    #
    # 这已经是同一个根因的第三处了（前两处是 advisor 里的综合得分和结论
    # 正则）。凡是解析模型自由文本的地方，都要假设它会用 markdown 修饰，
    # 换供应商时尤其要重新验一遍——格式要求写在提示词里只是"通常会遵守"。
    for name in _ADVICE_SECTIONS:
        m = re.search(r"\*{0,2}" + re.escape(name) + r"\*{0,2}\s*[：:]", text)
        if m:
            positions.append((m.start(), name, m.end()))
    positions.sort()
    for i, (idx, name, body_start) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        parts[name] = text[body_start:end].strip()
    return parts


def _load_daily_plan(market: str) -> dict:
    """读当天的盘前计划快照，不现场取数、不调AI。返回 {} 表示今天没有这份计划。"""
    path = Path(__file__).resolve().parent / "data" / f"daily_plan_{market.lower()}.json"
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(plan, dict) or plan.get("日期") != cn_now().date().isoformat():
        return {}
    return plan


def _daily_plan_item_for(symbol: str) -> dict | None:
    """在HK/US两份今日盘前计划里找这支票的结构化条目——2026-09-11前端审计
    发现的P0：同一支票，"今日可执行清单"（daily_plan.py，ATR/目标价公式
    算出来的结构化数字）和排行榜/自选（advisor.py的judge_stock，AI在理由
    段落里自己另外写一个目标价）经常对不上（比如老铺黄金结构化650、AI
    正文665）——这是两条完全独立的计算路径，一个是公式，一个是模型自己
    现算，本来就没有理由凑巧相等。

    正确的修法是"程序只算一次、AI引用不许自己编"，但judge_stock的完整
    prompt改造（把daily_plan的计算结果作为已知事实喂给它，禁止它自己
    重新推导目标价）涉及改核心打分prompt、跟下单闸门的计算顺序也要理顺，
    风险和工作量都不小，不在这次前端审计的修复范围内一起做。这里先做
    风险最低、当场见效的一半：排行榜显示目标价时，只要daily_plan当天
    对这支票算过结构化数字，就优先展示这个数字（清楚标成"系统计算"），
    不再展示AI自己在正文里写的那个可能对不上的数字——不是删掉AI的推理，
    只是不让它的"目标价"这三个字跟公式算出来的同名数字同台打架。
    """
    for market in ("HK", "US", "A"):
        plan = _load_daily_plan(market)
        for item in (plan.get("关注候选") or []):
            if item.get("代码") == symbol:
                return item
    return None


def _is_order_ready(item: dict) -> bool:
    """一条候选是不是真的可以照着下单——交易参数齐全且过了闸门。

    2026-09-11修：这里原来查的是 买入区间/止损/目标 三个键，而 daily_plan.py
    的 _build_item 写出来的是 买入下沿/买入上限/止损参考/目标价——键名对不上，
    条件恒为假，所以首页"今日可执行清单"从上线起就没渲染出过任何一条，不是
    "今天确实没有标的达标"。（intraday_watch.py 早就在做 止损参考->止损 的
    重命名，这份 schema 的真实键名在那边有据可查。）
    """
    return bool(
        item.get("方向") == "买入"
        and item.get("新开仓状态") == "可执行"
        and item.get("建议股数")
        and item.get("买入下沿") is not None
        and item.get("买入上限") is not None
        and item.get("止损参考") is not None
        and item.get("目标价") is not None
        and (item.get("盈亏比") or 0) > 0
    )


def _load_order_ready_items(market: str) -> list[dict]:
    plan = _load_daily_plan(market)
    if plan.get("AI状态") != "正常":
        return []
    return [item for item in (plan.get("关注候选") or []) if _is_order_ready(item)]


def _load_watch_only_items(market: str) -> list[dict]:
    """没通过下单闸门、但当天确实被打过分的候选。

    2026-09-11用户要求："就算没达到门槛也要写啊我得参考啊"。原来这块只渲染
    可执行清单，一旦当天没有标的过闸门，整块就是一句"今天暂无"，等于把当天
    几十支的评分工作全藏起来了。差一点点的候选和差很远的候选对用户是完全
    不同的信息——把它们连同"差在哪"一起列出来，判断权交回给人。
    """
    plan = _load_daily_plan(market)
    return [item for item in (plan.get("关注候选") or []) if not _is_order_ready(item)]


def _render_advice_section():
    """首页"AI投研候选"——跟其它模块（世界地图/今日资讯）唯一的本质区别：
    这里明确给买入/卖出/持有/观望结论，其它模块刻意"只摆事实不下结论"。
    这个差异必须对访客说清楚，不能让人以为整个网站的调性突然变了。

    只读advisor.py（私人cron脚本，工作日17:30跑一次）写进advice表的最近一次
    结果，首页访问不现场重新跑——重新跑一次要几分钟、几十次AI调用，公开页面
    每次访问都触发一遍完全不现实，也没必要（这类基本面判断一天一次足够新）。
    """
    st.markdown("**今日可执行清单**")
    st.caption("通过买入区间、股数、止损、目标和盈亏比全部校验的标的排在最前；没过闸门的也列出来，并写明差在哪。")

    def _plan_row(item: dict, *, ready: bool) -> str:
        # 买入区间经常只有单边：daily_plan 在均线已经高于赔率分界时会只给上限
        # 不给下沿（"这个位置本来就不便宜"），两边都当必填就会把一个有效的
        # "不高于X就能买"渲染成一个没信息量的破折号。
        lo, hi = item.get("买入下沿"), item.get("买入上限")
        _has_lo, _has_hi = isinstance(lo, (int, float)), isinstance(hi, (int, float))
        if _has_lo and _has_hi:
            range_text = f"买入区间 {lo:.2f}–{hi:.2f}"
        elif _has_hi:
            range_text = f"买入上限 {hi:.2f}"
        elif _has_lo:
            range_text = f"买入下沿 {lo:.2f}"
        else:
            range_text = "尚无有效买入区间"
        bits = [range_text]
        if item.get("建议股数"):
            bits.append(f"买入 {int(item['建议股数'])} 股")
        if isinstance(item.get("止损参考"), (int, float)):
            bits.append(f"止损 {item['止损参考']:.2f}")
        if isinstance(item.get("目标价"), (int, float)):
            bits.append(f"目标 {item['目标价']:.2f}")
        if isinstance(item.get("盈亏比"), (int, float)):
            bits.append(f"盈亏比 {item['盈亏比']:.2f}:1")
        if isinstance(item.get("现价"), (int, float)):
            bits.insert(0, f"现价 {item['现价']:.2f}")
        tag = (
            f"<span style='color:{OK_COLOR};font-size:.72rem;font-weight:600'>可执行</span>"
            if ready else
            f"<span style='color:var(--fa-faint);font-size:.72rem'>仅观察</span>"
        )
        # 没过闸门的必须把原因摆出来。"不可执行原因"是 daily_plan 逐条算出来的
        # 具体判据（尚未触发/不可追高/趋势仍向下/盈亏比不足…），不是一句笼统的
        # "不达标"——用户要的正是这个，好自己判断是"差一点"还是"差很远"。
        reason = item.get("不可执行原因") if not ready else None
        score = item.get("评分")
        return (
            f"<div style='padding:9px 0;border-bottom:1px solid var(--fa-border)'>"
            f"<strong>{_esc(_clean_name(item.get('名称', '')))}</strong>"
            f"<span style='color:var(--fa-faint);font-size:.78rem'> · "
            f"{_esc(str(item.get('市场', '')))} · {_esc(str(item.get('代码', '')))}"
            + (f" · {score}分" if score is not None else "")
            + f"</span>&nbsp;&nbsp;{tag}<br>"
            f"<span style='font-size:.8rem;color:var(--fa-muted)'>{' · '.join(bits)}</span>"
            + (f"<br><span style='font-size:.78rem;color:var(--fa-faint)'>{_esc(reason)}</span>"
               if reason else "")
            + "</div>"
        )

    _order_ready = [item for _market in ("HK", "US") for item in _load_order_ready_items(_market)]
    _watch_only = [item for _market in ("HK", "US") for item in _load_watch_only_items(_market)]
    if not _order_ready and not _watch_only:
        st.caption("今天还没有生成盘前计划（港股09:00前、美股21:00前各跑一次）。")
    else:
        if _order_ready:
            for item in _order_ready:
                st.markdown(_plan_row(item, ready=True), unsafe_allow_html=True)
        else:
            st.caption("今天没有标的通过全部下单校验——下面是当天打过分、但被闸门拦下的候选，供参考，不是下单指令。")
        # 没过闸门的按评分降序，最多列8条：这块是"参考"，不是又一张长列表。
        for item in sorted(_watch_only, key=lambda x: -(x.get("评分") or 0))[:8]:
            st.markdown(_plan_row(item, ready=False), unsafe_allow_html=True)

    st.markdown("**投研观察排行榜**")
    # 2026-09-12（前端审计"排行榜说买入、自选里同一只标观望"）：补一句说明
    # 这里的结论回答的是哪个问题。这条链路(source='screen')问的是"现在值不
    # 值得新建仓"，自选/持仓那边(source='position')问的是"已有仓位要不要继续
    # 拿"，同一支票两个答案可以同时成立，不是数据打架。详见自选行展开区里
    # 对应的那段说明。
    st.caption(
        "研究排序，不是下单指令——实际操作以上方「今日可执行清单」为准。"
        "这里回答「值不值得新建仓」，持仓页的标签回答「已持有的要不要继续拿」，两者不一致正常。"
    )
    # 卡片可点击跳转详情页——复用持仓列表卡片验证过的方案（见
    # _render_position_rows 的踩坑记录：JS/CSS猜DOM结构点不动，最后用最朴素
    # 的<a href="?open_symbol=...">整页导航才可靠）。那段CSS只在持仓tab渲染
    # 时才注入，首页不一定会经过那个函数，这里独立注入一份，不依赖执行顺序。
    st.markdown(
        "<style>"
        "a.pos-card-link, a.pos-card-link:link, a.pos-card-link:visited {"
        "  text-decoration: none !important; color: inherit !important;"
        "  display: block; cursor: pointer;"
        "}"
        "a.pos-card-link:hover { opacity: 0.85; }"
        "</style>",
        unsafe_allow_html=True,
    )
    # 2026-09-08：港股/美股分开出榜，不再混排——跟微信那边09:00/21:00分市场
    # Top3推荐是同一次改造，网页首页也要跟着改，不能一边港美股分开一边
    # 网页还是大杂烩。source改成watchlist_hk/watchlist_us（advisor.py的
    # judge_market_watchlist写的独立批次），不再需要market_quota凑配额——
    # 每个市场本来就是独立池子，不存在"被另一个市场挤占名额"这回事。
    # 老的source="watchlist"（三市场混排）不删，advisor.py主流程仍然在写，
    # 只是首页不再读它。
    _market_label = {"US": "美股", "HK": "港股", "A": "沪深"}

    def _render_board_rows(board):
        for rank, row in enumerate(board, 1):
            market_key = row.get("market", "A")
            _vtext = _clean_ai_markdown(row.get("fundamental_verdict", ""))
            parts = _parse_advice_text(_vtext)
            action = row.get("action", "观望")
            price = row.get("price_at_advice")
            price_text = f"{price:.2f}" if price else "—"
            # 2026-09-11修：现价标注取价时间——这是这次判断生成那一刻的价格
            # 快照，不是此刻的实时价，"今日可执行清单"用的是当天另一次单独
            # 取数（daily_plan.py），两边时间点不同、数字天然可能不一样。
            # 之前两边都不标时间，看起来像同一支票现价对不上是bug，其实只是
            # 没写清楚"现价"分别指哪个时刻。
            _created = row.get("created_at") or ""
            price_time = ""
            if _created:
                try:
                    price_time = f"（{_to_cn_time_str(_created)[5:16]}取价）"
                except Exception:
                    price_time = ""
            # 目标价优先用daily_plan.py当天算出来的结构化数字（公式计算，
            # 跟"今日可执行清单"同一个数），AI在理由段落里自己另外写的目标价
            # 不再单独展示成一个可能对不上的数字——见_daily_plan_item_for。
            _plan_item = _daily_plan_item_for(row.get("symbol", ""))
            if _plan_item and isinstance(_plan_item.get("目标价"), (int, float)):
                target_text = f"{_plan_item['目标价']:.2f}（系统计算）"
            else:
                target_text = parts.get("目标价")
            score = row.get("score")
            href = (
                f"?open_symbol={urllib.parse.quote(row.get('symbol',''))}"
                f"&open_market={urllib.parse.quote(market_key)}"
                f"&open_name={urllib.parse.quote(row.get('name',''))}"
                f"{_auth_qs()}"
            )
            # 跟持仓/自选列表同一个处理：不再用 border=True 的卡片。原来每一条是
            # 一张带边框的卡片，卡片里又套一个带边框的"基本面/技术面/价格位置"
            # 折叠框，五条就是五组方框套方框。改成发丝线分隔的平铺条目。
            with st.container(key=f"lb_row_{market_key}_{row.get('symbol','')}"):
                st.markdown(
                    f"<a class='pos-card-link' href='{href}' target='_self'>"
                    # 用 float 而不是 flex 做"左名称/右分数"这一行：flex 在这个
                    # <a> 包 <div> 的嵌套结构里不生效（迷你条形图那次已经实测
                    # 过一遍，右侧那组会掉到第二行、左对齐，右边空一大片，
                    # 整行重心全歪）。float 不依赖父容器是不是 flex 容器。
                    # 右侧那组写在源码里更靠前，是 float 的标准写法。
                    f"<div style='overflow:hidden'>"
                    f"<span style='float:right;white-space:nowrap'>"
                    # 2026-09-12（前端审计第14条"综合评分81是一个很小的数字"）：
                    # 评分是这一行里信息量最大的一个数，原来只是一串跟其它元信息
                    # 一样大的灰字，扫一眼榜单根本注意不到。
                    # 第一版做成了深色圆形徽章（审计文档建议的做法），用户看了
                    # 实机反馈"这个黑色圈圈太丑了"——一个实心深色圆盘在这套
                    # 近乎无色、全靠字重和留白分层的界面里，是整页对比度最高的
                    # 元素，抢戏程度远超它该有的分量，跟右下角那颗AI浮标当初从
                    # 实心黑圆改成白底细边是同一个问题。
                    # 改成不加任何底色，只把数字本身做得站得住：字号提上去、
                    # 字重加粗、用正文墨色（低分压灰）。可见性来自字本身，
                    # 不靠色块。
                    + (
                        f"<span style='font-size:1.02rem;font-weight:650;letter-spacing:-.02em;"
                        f"font-variant-numeric:tabular-nums;"
                        f"color:{'var(--fa-text)' if score >= 60 else 'var(--fa-muted)'}'>{score}</span>"
                        if score is not None else ""
                    )
                    + f"<span style='color:var(--fa-muted);margin-left:10px;"
                    f"font-size:0.74rem;font-weight:600;letter-spacing:.02em'>研究观点：{_esc(action)}</span></span>"
                    f"<span style='font-weight:600;letter-spacing:-.01em'>"
                    f"<span style='color:var(--fa-faint);font-weight:500'>{rank}</span>&nbsp;&nbsp;{_esc(row.get('name',''))}"
                    f"<span style='font-weight:400;color:var(--fa-faint);font-size:0.78rem'> · {_market_label.get(market_key, market_key)}</span></span></div>"
                    f"<div style='font-size:0.74rem;color:var(--fa-faint);margin-top:3px'>{_esc(row.get('symbol',''))} · 现价{price_text}{_esc(price_time)}"
                    f" · 置信度{_esc(parts.get('置信度','—'))}"
                    # 目标价和投资期限是研报格式里最该被一眼看到的两项——"买入"
                    # 如果不带目标价和时间尺度，就是一句没有可检验内容的话。
                    + (f" · 目标价{_esc(target_text)}" if target_text else "")
                    + (f" · {_esc(parts['投资期限'])}" if parts.get("投资期限") else "")
                    + "</div>"
                    # 每张卡末尾那句"仅供参考，不构成投资建议"是advisor的prompt里
                    # 硬性要求AI附上的，落在数据里。榜单一屏十几张卡，同一句重复
                    # 十几遍，占地方，而且重复到一定次数人眼就自动跳过了，反而不如
                    # 只说一次有效。渲染时剥掉，改成榜单末尾统一出现一次。
                    # 六维拆解不画在卡片正面：2026-09-12实机看下来，六条各占
                    # 一行、一张卡就被撑高一半，把真正该读的"理由"挤到了屏幕
                    # 外面——一屏五张卡要滚很久，跟"简约清爽"背道而驰。卡片
                    # 正面只留结论链路（名称/分数/观点/一行元信息/理由），
                    # 拆解收进下面已有的那个展开区，想看的人点一下就有。
                    + f"<div style='margin-top:8px'>{_esc(_strip_disclaimer(parts.get('理由', '')))}</div>"
                    f"</a>",
                    unsafe_allow_html=True,
                )
                # 展开区按研报的读法排序：先多空两边的论点，再是三个基础面，
                # 最后是假设/催化剂/证伪这三条"这个判断怎么才算错"的内容。
                _detail_secs = [
                    "多头逻辑", "空头逻辑", "基本面", "技术面", "价格位置",
                    "关键假设与催化剂", "证伪条件",
                ]
                _bars_html = _score_breakdown_bars_html(_vtext)
                if _bars_html or any(parts.get(sec) for sec in _detail_secs):
                    with st.expander("维度打分 / 多空逻辑 / 催化剂与证伪"):
                        if _bars_html:
                            st.markdown(
                                "<div style='font-size:0.74rem;color:var(--fa-faint);"
                                "margin-bottom:2px'>维度打分</div>" + _bars_html,
                                unsafe_allow_html=True,
                            )
                        for sec in _detail_secs:
                            if parts.get(sec):
                                st.markdown(_labeled_line(sec, parts[sec]), unsafe_allow_html=True)

    # 港股/美股各自独立取一份，不再用market_quota从混合池里配额分配。
    _any_board = False
    for _mk, _label in (("HK", "港股"), ("US", "美股")):
        try:
            _data = get_latest_leaderboard(limit=5, source=f"watchlist_{_mk.lower()}")
        except Exception:
            continue
        _board = _data.get("leaderboard") or []
        if not _data.get("run_date") or not _board:
            continue
        _any_board = True
        st.markdown(f"**{_label}**")
        st.markdown(
            f"<div style='font-size:0.74rem;color:var(--fa-faint);margin:-4px 0 14px'>"
            f"更新于 {_data['run_date']}</div>",
            unsafe_allow_html=True,
        )
        _render_board_rows(_board)

    if not _any_board:
        st.caption("还没有生成过投研观察排行榜")
        return

    # 免责声明统一放在榜单末尾说一次——上面每张卡里的那句已经剥掉了。
    st.markdown(
        f"<div style='margin-top:18px;font-size:0.74rem;color:var(--fa-faint)'>"
        f"{_DISCLAIMER_SENTENCE}</div>",
        unsafe_allow_html=True,
    )


def _render_app_guide():
    """应用指南。2026-09-04侧边栏整个撤掉，这块挪到"我的"分区，正文原样保留。

    外面包一层带key的容器：这两个折叠面板原来还是Streamlit默认的白底圆角带框
    样式，跟同一页上面那些发丝线分隔的行（最近搜索那几行）不是一套东西，
    在"我的"这一页底部突兀地冒出两个白盒子。靠这个key在CSS里把它们压成
    跟上面完全一致的行。
    """
    with st.container(key="my_about_guide"), st.expander("应用指南"):
        st.markdown(
            "**定位**\n\n"
            "Invest Agent 是一个多市场（沪深/港股/美股）行情查询和数据交叉验证工具，"
            "把行情、财务、新闻这几类原始数据放在一起给你看。个股/指数详情页的 AI 分析"
            "只做交叉核对和综合评分，不做黑箱荐股、不直接给买卖判断；"
            "「持仓」页的组合分析是例外——它只针对你自己填的真实持仓和设定的资金上限，"
            "按集中度、资金余量给出继续持有/加仓/减仓/定投/止盈/割肉这类具体操作建议"
            "（附股数和金额），这是基于你自己数据算出来的仓位管理建议，不是选股推荐，"
            "同样不构成投资建议，请自行判断风险。\n\n"
            "**行情**\n\n"
            "在「行情」分区按市场查看核心指数（沪深按涨跌幅列示，港股按东财人气榜排热度，"
            "美股展示固定核心股名单），沪深另有涨停/跌停池和南向资金；"
            "局部报价会自动刷新，模块旁会标注市场状态与数据时间。\n\n"
            "**个股/指数详情页**\n\n"
            "点开任意标的先看K线或分时图，再看一手资讯（沪深优先展示官方公告，"
            "港股/美股优先富途资讯，都查不到才退回财新摘要），最后是 AI 深度分析——"
            "包含资讯解读、财务摘要、对比大盘、技术面与消息面交叉验证，"
            "以及一段综合评分（0-100，越高越偏多头证据、越低越偏空头证据，"
            "评分依据是各条独立证据链是否互相印证，不是 AI 自己主观看好程度）。\n\n"
            "**持仓**\n\n"
            "右上角搜索可按代码或名称查行情，+ 按钮用于添加持仓；填写股数或金额后记为真实持仓，成交均价可选填"
            "（不填只是关注），卡片显示迷你走势图、实时涨跌和持仓浮盈，"
            "点卡片进详情页，点 × 卖出或取消关注。\n\n"
            "**AI模拟炒股**\n\n"
            "内置 Gemini AI 用虚拟资金自主管理一个模拟盘——只交易港股/美股（沪深不参与），"
            "在开盘时段按行情触发决策，不需要手动操作；"
            "这里能看到它的持仓、收益曲线和完整交易记录，仅供观察AI决策能力，"
            "不构成投资建议。\n\n"
            "**我的**\n\n"
            "账户信息、自选与持仓的数量和市场分布、累计做过多少次AI分析、最近搜索、"
            "以及行情与 Gemini AI 的数据源状态都在这里。其中「AI 判断准确率」是这样来的：每次"
            "生成「综合数据分析」时会记录当时价格和 AI 判断的方向倾向，满 7 天后"
            "自动补录当时的价格做对照，统计一个方向一致率——这是历史记录的客观统计，"
            "不代表未来表现，不是胜率承诺。\n\n"
            "**重要说明**\n\n"
            "本应用所有分析、评分、资讯摘要仅基于公开数据的整理和交叉核对，"
            "不构成任何投资建议，不保证数据的完整性和及时性，据此操作的风险自负。"
        )


def _render_data_source_health():
    """数据源连接与熔断状态。同样是2026-09-04从侧边栏挪过来的，正文未改。"""
    with st.container(key="my_about_health"), st.expander("数据源状态"):
        _health = get_data_source_health()
        _futu = _health["futu"]
        if not _futu["已安装SDK"]:
            st.markdown("**Futu OpenD**：未安装 SDK，港股/美股实时数据全部走兜底源（腾讯行情）。")
        elif _futu["已连接"]:
            st.markdown(f"**Futu OpenD**：<span style='color:{OK_COLOR}'>已连接</span>", unsafe_allow_html=True)
        else:
            _last_try = _futu["上次尝试连接"]
            _last_try_txt = (
                datetime.fromtimestamp(_last_try).strftime("%H:%M:%S") if _last_try else "尚未尝试"
            )
            _next_interval = _futu["下次重连间隔秒"]
            _next_interval_txt = f"，下次重连间隔约{_next_interval:.0f}秒（连续失败会指数退避，最长5分钟）" if _next_interval else ""
            st.markdown(
                f"**Futu OpenD**：<span style='color:{BAD_COLOR}'>未连接</span>"
                f"（上次尝试 {_last_try_txt}{_next_interval_txt}；"
                "港股/美股行情会自动退回腾讯行情兜底，不影响使用）",
                unsafe_allow_html=True,
            )

        # 行情连接正常不代表 AI 决策链路正常。模拟盘曾发生账户欠费后持续失败、
        # 而这里仍显示“暂无失败”的误导状态，所以单独展示最近一次 AI 决策。
        try:
            # limit 放大到 200：这里要回答两个问题——"最近一次是不是失败"只需要
            # 第一条，但"最后一次成功是什么时候"要往回翻。2026-09-11用户截图里
            # 显示"最后一次成功 无成功记录"，其实只是连续失败次数超过了当时写死
            # 的 limit=8，历史上成功过很多次——把"翻不到"说成"没有过"是在报假话。
            _sim_runs = get_sim_agent_runs(sim_agent.advisor._EMAIL, limit=200)
            _sim_latest = _sim_runs[0] if _sim_runs else None
            _sim_ok = next((r for r in _sim_runs if r.get("status") != "失败"), None)
            if _sim_latest and _sim_latest.get("status") == "失败":
                _when = _to_cn_dt(_sim_latest.get("run_at"))
                _when_text = _when.strftime("%m-%d %H:%M") if _when else "未知时间"
                _ok_when = _to_cn_dt(_sim_ok.get("run_at")) if _sim_ok else None
                _ok_text = _ok_when.strftime("%m-%d %H:%M") if _ok_when else "最近200次内没有成功记录"
                st.markdown(
                    f"**Gemini AI 决策**：<span style='color:{BAD_COLOR}'>不可用</span>"
                    f"（最近一次失败 {_when_text}；最后一次成功 {_ok_text}）",
                    unsafe_allow_html=True,
                )
            elif _sim_latest:
                _when = _to_cn_dt(_sim_latest.get("run_at"))
                _when_text = _when.strftime("%m-%d %H:%M") if _when else "未知时间"
                st.markdown(
                    f"**Gemini AI 决策**：<span style='color:{OK_COLOR}'>正常</span>"
                    f"（最近一次 {_when_text}：{_esc(str(_sim_latest.get('status') or '完成'))}）",
                    unsafe_allow_html=True,
                )
            else:
                st.markdown("**Gemini AI 决策**：暂无运行记录")
        except Exception:
            st.markdown("**Gemini AI 决策**：状态暂时读取不到")

        _breakers = _health["熔断记录"]
        if _breakers:
            st.caption("兜底数据源熔断记录（只记录触发过失败的，没列出不代表已验证成功，只是还没失败过）")
            for _b in _breakers:
                _ts = datetime.fromtimestamp(_b["最近一次失败"]).strftime("%m-%d %H:%M:%S")
                _state, _color = ("冷却中", DOWN_COLOR) if _b["冷却中"] else ("已恢复", NEUTRAL_COLOR)
                st.markdown(
                    f"<span style='font-size:0.8rem'>{_b['名称']}："
                    f"<span style='color:{_color}'>{_state}</span>（最近一次失败 {_ts}）</span>",
                    unsafe_allow_html=True,
                )
        else:
            st.caption("暂无兜底数据源失败记录。")


@st.dialog("休市公告")
def _show_closure_notice(items: list[dict]):
    """开市安排有变时的一次性公告。

    2026-09-04用户要求："如果港股沪深美股有特殊节假日休假，在前一天我们点进
    网页的时候触发弹窗公告，几月几号什么股因为什么节假日休市"。

    只在"临近"时弹：默认看未来3天。提前太久没有行动价值（十月的假期九月初
    天天弹只会让人学会闭眼点掉），太晚又失去提醒意义。周末不算休市，公告里
    提周末是废话——休市日是拿工作日减去交易日反推的，天然不含周末。
    """
    for it in items:
        _reason = f"（{it['name']}）" if it.get("name") else ""
        _what = "提前收市" if it.get("half_day") else "休市"
        st.markdown(
            f"<div style='padding:10px 0;border-bottom:1px solid var(--fa-border)'>"
            f"<div style='font-size:0.95rem;color:var(--fa-text);font-weight:600'>"
            f"{_esc(it['date'])}　{_esc(it['market_label'])}{_what}{_esc(_reason)}</div></div>",
            unsafe_allow_html=True,
        )
    st.caption("休市期间该市场不接受委托，AI 模拟盘也不会在这些日子里做决策。")
    # 不放"知道了"按钮——用户明确要求"按叉叉就直接退出了"。
    # Streamlit 弹窗自带右上角关闭，再加一个确认按钮是多余的一次点击；而且
    # 那个按钮之前还引入过一个真问题：点它触发 rerun，配额判断重新执行一遍，
    # 弹窗立刻又出来，看上去像按钮失灵。少一个控件就少一处这种耦合。
    # 关掉之后本次会话内不会再弹（见 _maybe_show_closure_notice 的会话闸门）。


def _maybe_show_closure_notice():
    """判断要不要弹休市公告。每天最多三次。

    用户要求"每天跳三次就行，不需要每次点进去就跳"。三次的取舍：一次容易在
    随手关掉之后就再也想不起来，每次都弹则是骚扰——三次能覆盖"早中晚各开一次
    网站"这个真实节奏，又不会烦人。

    详情页一律不弹，而且要在计数之前判断：卡片跳转全是整页导航，如果把详情页
    也算进次数，点三只股票配额就用光了，真正打开首页时反而不提醒了。
    """
    import datetime as _dt

    _MAX_PER_DAY = 3

    # 详情页不弹。判据用 session_state 里有没有详情页标记，不用 query_params
    # ——那几个 open_* 参数在上游处理块里已经被 clear() 掉并 rerun 过了，
    # 执行到这里参数早就没了。
    if (st.session_state.get("_detail_symbol")
            or st.session_state.get("_index_detail_code")
            or st.session_state.get("_sector_detail_name")):
        return

    today = str(_dt.date.today())

    # 同一次会话内只弹一次。用户反馈"从首页点去自选股又跳出来了"——切分区
    # 是一次页内重跑，判断会重新执行一遍，于是每切一次就弹一次。
    #
    # 这个公告的本意是"打开网站时提醒你一句"，不是"每次操作都提醒"。所以
    # 分成两层：会话内只弹一次（切分区、点按钮都不会再弹），跨会话按天计数
    # 最多三次（真正重新打开网站才有机会再提醒）。
    if st.session_state.get("_closure_seen_session"):
        return

    # 站内跳转不弹。2026-09-05审计实测：游客态下从详情页返回自选，公告又弹了
    # 一次——因为项目里的详情页跳转是整页导航（<a href="?open_symbol=">），
    # 每次导航都新建一个 Streamlit session、清空 session_state，上面那道
    # 会话闸门等于不存在。已登录用户的次数落了库不受影响，游客只能靠
    # session_state，正好踩在这个坑上。
    #
    # 判据就用 URL 上有没有参数：干净的 URL 才是"重新打开网站"，带着
    # open_symbol/section 这类参数的一定是站内跳转。这比再造一套游客标识
    # 简单得多，也正好对上用户的原话——"不需要每次点进去就跳"。
    try:
        if dict(st.query_params):
            return
    except Exception:
        pass

    _email = st.session_state.get("user_email")

    # 已登录用户的次数落库，整页刷新之后仍然有效（项目里的跳转全是整页导航，
    # 只放 session_state 的话每跳一次就重新计数，等于没限制）；游客没有
    # email 可挂，退回只用 session_state。
    if _email:
        try:
            shown = get_closure_notice_count(_email, today)
        except Exception:
            shown = int(st.session_state.get(f"_closure_n_{today}", 0))
    else:
        shown = int(st.session_state.get(f"_closure_n_{today}", 0))
    if shown >= _MAX_PER_DAY:
        return

    try:
        items = [
            c for c in get_market_closures(days=10)
            if (_dt.date.fromisoformat(c["date"]) - _dt.date.today()).days <= 3
        ]
    except Exception:
        items = []
    if not items:
        return

    # 只有真的要弹才计数——没有休市安排时不该消耗配额。
    st.session_state["_closure_seen_session"] = True
    st.session_state[f"_closure_n_{today}"] = shown + 1
    if _email:
        try:
            bump_closure_notice_count(_email, today)
        except Exception:
            pass
    _show_closure_notice(items)


def _render_my_page():
    """"我的"——账户、个人记录、指南、数据源状态。

    2026-09-04新增，同时把左侧边栏整个撤掉。原来侧边栏里只有三个折叠面板加一个
    退出按钮，却在每一屏都占掉两百多像素的宽度；而它装的东西（账户、历史回看
    摘要、应用指南、数据源状态）本来就都是"偶尔看一眼"的内容，没有一样需要
    常驻。改成主导航里的一个正式分区，宽度还给内容，信息也能铺开讲。

    这一页刻意不只是把侧边栏那三块搬过来——既然是"我的"，就把散在各处、
    但确实属于这个用户自己的记录集中到一处：自选/持仓的数量和市场分布、
    累计做过多少次AI分析、搜过什么、AI判断的历史准确率、以及几个开关的当前
    状态。这些数字原来要么没人统计过，要么埋在别的页面里。
    """
    logged_in = bool(st.session_state.get("logged_in"))
    email = st.session_state.get("user_email", "") if logged_in else ""

    # ── 账户 ────────────────────────────────────────────────────────────
    st.markdown("**账户**")
    if logged_in:
        ov = {}
        try:
            ov = get_user_overview(email)
        except Exception:
            ov = {}
        first_seen = ov.get("first_seen")
        since_txt = ""
        if first_seen:
            _t = _to_cn_dt(first_seen)
            if _t:
                since_txt = f"自 {_t.strftime('%Y-%m-%d')} 起使用"
        acc_col, btn_col = st.columns([6, 1], vertical_alignment="center")
        acc_col.markdown(
            f"<div style='font-size:0.98rem;font-weight:600;color:var(--fa-text);letter-spacing:-.01em'>"
            f"{_esc(email)}</div>"
            + (f"<div style='font-size:0.76rem;color:var(--fa-faint);margin-top:3px'>{since_txt}</div>"
               if since_txt else ""),
            unsafe_allow_html=True,
        )
        with btn_col:
            if st.button("退出登录", key="_my_logout"):
                _tok = st.session_state.pop("_token", None)
                if _tok:
                    _invalidate_token(_tok)
                try:
                    del st.query_params["_auth"]
                except Exception:
                    pass
                _cv1.html(
                    '<script>try{window.parent.localStorage.removeItem("fa_auth_tok");'
                    'window.parent.document.cookie="fa_auth_tok=; max-age=0; path=/";}catch(e){}</script>',
                    height=1,
                )
                st.session_state["logged_in"] = False
                st.session_state.pop("user_email", None)
                st.rerun()
    else:
        ov = {}
        acc_col, btn_col = st.columns([6, 1], vertical_alignment="center")
        acc_col.markdown(
            "<div style='font-size:0.98rem;font-weight:600;color:var(--fa-text)'>游客模式</div>"
            "<div style='font-size:0.76rem;color:var(--fa-faint);margin-top:3px'>"
            "行情、详情页、AI分析都能看；自选、持仓和判断记录需要登录</div>",
            unsafe_allow_html=True,
        )
        with btn_col:
            if st.button("登录 / 注册", key="_my_login", type="primary"):
                st.session_state["guest_mode"] = False
                st.rerun()

    if logged_in:
        # ── 我的记录 ────────────────────────────────────────────────────
        st.markdown("**我的记录**")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("自选", f"{ov.get('watch_count', 0)}")
        m2.metric("持仓", f"{ov.get('hold_count', 0)}")
        m3.metric("AI 分析", f"{ov.get('analysis_count', 0)}")
        m4.metric("搜索", f"{ov.get('search_count', 0)}")

        # ── 关注的市场 ──────────────────────────────────────────────────
        by_market = ov.get("by_market") or {}
        total_mkt = sum(by_market.values())
        if total_mkt:
            st.markdown("**关注的市场**")
            _label = {"A": "沪深", "HK": "港股", "US": "美股"}
            # 一根横向占比条 + 一行图例。比三个数字更直观地回答"我主要在看哪个
            # 市场"，配色走图表那套去饱和色板，不引入新颜色。
            _seg_colors = ["#2F3A45", "#7C8B9A", "#B9A17B"]
            segs, legend = [], []
            for i, (mkt, n) in enumerate(sorted(by_market.items(), key=lambda kv: -kv[1])):
                pct = n / total_mkt * 100
                c = _seg_colors[i % len(_seg_colors)]
                segs.append(f"<div style='width:{pct:.2f}%;background:{c}'></div>")
                legend.append(
                    f"<span style='display:inline-flex;align-items:center;gap:6px;margin-right:20px'>"
                    f"<span style='width:8px;height:8px;border-radius:2px;background:{c};display:inline-block'></span>"
                    f"<span style='color:var(--fa-text-2);font-size:0.82rem'>{_label.get(mkt, mkt)}</span>"
                    f"<span style='color:var(--fa-faint);font-size:0.82rem'>{n} · {pct:.0f}%</span></span>"
                )
            st.markdown(
                "<div style='display:flex;height:8px;border-radius:4px;overflow:hidden;gap:2px'>"
                + "".join(segs) + "</div>"
                + "<div style='margin-top:10px'>" + "".join(legend) + "</div>",
                unsafe_allow_html=True,
            )

        # ── AI 判断准确率 ───────────────────────────────────────────────
        st.markdown("**AI 判断准确率**")
        _backfill_due_reviews(email)
        try:
            stats = get_accuracy_stats(email)
        except Exception:
            stats = {"总数": 0}
        if stats.get("总数"):
            a1, a2, a3 = st.columns(3)
            a1.metric("方向一致率", f"{stats['一致率']:.0f}%")
            a2.metric("已回看", f"{stats['总数']}")
            a3.metric("说对", f"{stats['一致数']}")
            # 按市场/按方向拆开——笼统一个数看不出"在哪个市场准""偏多还是偏空准"。
            _rows = []
            for group_name, group in (("按市场", stats.get("按市场") or {}), ("按方向", stats.get("按方向") or {})):
                for k, v in group.items():
                    if not v.get("总数"):
                        continue
                    _label_map = {"A": "沪深", "HK": "港股", "US": "美股"}
                    _rows.append((group_name, _label_map.get(k, k), v["一致率"], v["总数"]))
            if _rows:
                with st.expander("按市场 / 按方向拆开看"):
                    for g, k, rate, n in _rows:
                        st.markdown(
                            f"<div style='display:flex;justify-content:space-between;padding:6px 0;"
                            f"border-bottom:1px solid var(--fa-border)'>"
                            f"<span style='color:var(--fa-text-2);font-size:0.86rem'>{g} · {k}</span>"
                            f"<span style='font-size:0.86rem'><span style='font-weight:600'>{rate:.0f}%</span>"
                            f"<span style='color:var(--fa-faint)'> · {n}次</span></span></div>",
                            unsafe_allow_html=True,
                        )
            st.markdown(
                "<div style='font-size:0.74rem;color:var(--fa-faint);margin-top:10px'>"
                "每次生成综合数据分析时记录当时价格和判断方向，满7天后自动补录实际价格做对照。"
                "这是历史记录的客观统计，不代表未来表现。</div>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                "<div style='color:var(--fa-muted);font-size:0.88rem;padding:2px 0'>"
                "还没有满7天可回看的记录</div>"
                "<div style='font-size:0.74rem;color:var(--fa-faint);margin-top:4px'>"
                "在个股详情页生成过综合数据分析之后，判断会被记下来，满7天自动补录当时的实际价格算方向是否一致。</div>",
                unsafe_allow_html=True,
            )

        # ── AI 排行榜/持仓判断 事后一致率 ──────────────────────────────────
        # 2026-09-11修（P0，前端审计"AI在AI咨询里自己拆排行榜的台"）：审计
        # 在AI咨询面板问"推荐股排行榜准不准"，AI如实引用了advice表
        # （advisor.py每天17:30自动判断）的事后一致率和打分回测结论——这些
        # 数据一直都在，只是从没在任何页面上单独展示过。而上面这一块
        # "AI 判断准确率"统计的是完全不同的东西（用户自己在个股详情页手动
        # 触发的"综合数据分析"，靠analyses表，独立的7天回看窗口）。两个
        # 标签长得像、口径完全不同，用户在这个页面只看到"还没有满7天"，
        # 却在AI咨询里听到一个具体的百分比，会以为AI在编数字或者前后矛盾
        # ——其实是这个页面从没展示过AI真正引用的那份数据。这里补上，
        # 标签明确写清楚"排行榜/持仓判断"，跟上面那块分开，不共用一个标题。
        try:
            _adv_acc = get_advice_accuracy(email)
            _by_source = _adv_acc.get("按来源", {})
        except Exception:
            _by_source = {}
        _adv_lines = []
        for _src, _label in (("watchlist", "推荐股排行榜"), ("position", "持仓判断")):
            _s = _by_source.get(_src)
            if _s and _s.get("总数"):
                _adv_lines.append((_label, _s["一致率"], _s["总数"]))
        st.markdown("**AI 排行榜 / 持仓判断事后一致率**")
        if _adv_lines:
            _cols = st.columns(len(_adv_lines))
            for _col, (_label, _rate, _n) in zip(_cols, _adv_lines):
                _col.metric(_label, f"{_rate:.0f}%", help=f"共{_n}次判断，方向（买入应涨/卖出应跌）事后核对")
            st.markdown(
                "<div style='font-size:0.74rem;color:var(--fa-faint);margin-top:4px'>"
                "这是advisor.py每天17:30自动生成的买卖判断（跟上面\"个股详情页AI分析\"是两套独立记录）。"
                "AI咨询里回答\"排行榜准不准\"引用的就是这份数据——分数越高不代表事后表现越好，"
                "详见打分体系的事后实证说明。</div>",
                unsafe_allow_html=True,
            )
        else:
            st.caption("还没有满足回看窗口的自动判断记录（advisor.py每天17:30生成，watchlist口径需满6.9天才回填）。")

        # ── AI 战绩墙 ───────────────────────────────────────────────────
        # 2026-09-12新增（升级路线图第1条，两份文档都把它列为最重要的一项）。
        # 上面那两块给的是汇总数字，汇总很容易被当成宣传；这里逐条摆出
        # "当时判断/当时价格/事后价格/涨跌"，亏的那几条一样列出来。数据全
        # 来自已经回填过事后价格的真实记录，不预测、不补值。
        st.divider()
        st.markdown("**AI 战绩墙**")
        # 顶部三个数字走全样本，跟下面列表的筛选无关——所以放在列表判空之外。
        # 之前嵌在 else 分支里，列表一为空连汇总也跟着消失；加了筛选之后
        # "筛完没有结果"会变成常见情况，这个结构必须先拆开。
        try:
            _summary = get_advice_outcome_summary()
        except Exception:
            _summary = {}
        if _summary.get("directional_count"):
            _c1, _c2, _c3 = st.columns(3)
            # 分母必须摆在明面上，不能只藏在 help 里。
            # 2026-09-13 审计原话："'方向判断胜率37%'和'已回填判断1430'并排
            # 显示…用户会自然认为分母是1430"——实际分母只有131（1430条里
            # 观望752、持有547，带方向的买入86+卖出45）。这一块是全站最
            # 强调"诚实"的地方，反而在分母上含糊，是最不该出的问题。
            _c1.metric(
                f"方向判断胜率（{_summary['directional_count']}条中说对{_summary['hits']}条）",
                f"{_summary['win_rate']:.0f}%",
                help="只统计买入/卖出这类声称了方向的判断；持有/观望没声称方向，不计入胜率。",
            )
            _c2.metric("平均事后涨跌", f"{_summary['avg_return_pct']:+.2f}%")
            _c3.metric(
                "已回填判断（含持有/观望）", f"{_summary['total_reviewed']}",
                help="所有已补录事后价格的记录总数。它不是左边胜率的分母。",
            )
            # 分母已经写进左边那个 metric 的标题里，不再重复一遍。
        # 默认只看带方向的判断。已回填的绝大多数是"持有/观望"，不筛的话一屏
        # 二十条里十九条是"无方向"——这个列表存在的意义是逐条核对"说买入的
        # 后来涨了没"，全是没有对错可言的记录时它就失去了作用（审计第13条）。
        _dir_only = st.toggle(
            "只看买入/卖出判断", value=True, key="_wall_dir_only",
            help="关掉会把持有/观望也列出来。那些判断没有声称方向，无所谓对错，也不计入上面的胜率。",
        )
        try:
            _outcomes = get_recent_advice_outcomes(limit=20, directional_only=_dir_only)
        except Exception:
            _outcomes = []
        if not _outcomes:
            st.caption(
                "最近还没有已回填事后价格的买入/卖出判断，关掉上面的开关可以看持有/观望。"
                if _dir_only else "还没有已回填事后价格的判断记录。"
            )
        _rows_html = []
        for _o in _outcomes:
            _ret = _o["return_pct"]
            _ret_color = UP_COLOR if _ret > 0 else (DOWN_COLOR if _ret < 0 else "var(--fa-muted)")
            if _o["hit"] is True:
                _mark, _mark_color = "说对", OK_COLOR
            elif _o["hit"] is False:
                _mark, _mark_color = "说错", BAD_COLOR
            else:
                _mark, _mark_color = "无方向", "var(--fa-faint)"
            _rows_html.append(
                "<div style='display:flex;align-items:center;gap:10px;padding:7px 2px;"
                "border-bottom:1px solid var(--fa-border);font-size:0.8rem'>"
                f"<span style='color:var(--fa-faint);min-width:42px'>{_esc((_o.get('created_at') or '')[5:10])}</span>"
                f"<span style='flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;"
                f"white-space:nowrap'>{_esc(_clean_name(_o.get('name') or _o.get('symbol')))}</span>"
                f"<span style='min-width:34px;color:var(--fa-text-2)'>{_esc(_o.get('action') or '')}</span>"
                f"<span style='min-width:30px;color:var(--fa-faint)'>{_o.get('score') if _o.get('score') is not None else '—'}</span>"
                f"<span style='min-width:62px;text-align:right;color:{_ret_color};font-weight:600'>{_ret:+.2f}%</span>"
                f"<span style='min-width:44px;text-align:right;color:{_mark_color};font-size:0.74rem'>{_mark}</span>"
                "</div>"
            )
        st.markdown("".join(_rows_html), unsafe_allow_html=True)
        st.caption(
            "「说对/说错」只对买入、卖出这类带方向的结论成立，持有/观望不计入胜率。"
            "事后价格是系统按固定回看窗口自动补录的，不是挑出来的时点。"
        )

        # ── 风险偏好 ────────────────────────────────────────────────────
        # 放在"我的"而不是"持仓"：清单里那句"低于风险档案下限2:1"是全站性的
        # 闸门参数，用户找设置会先来这一页（前端审计原话是"整个网站都找不到
        # 可以改这个下限的地方"）。持仓页那个"最大资金投入量"是另一件事
        # （给组合分析算剩余额度用），两者不合并。
        st.divider()
        _render_risk_profile_input(email)

        # ── 到价提醒 ────────────────────────────────────────────────────
        # 详情页那个popover只看得到当前这支票的提醒，设完就散在各个页面里。
        # 这里给一个总览：全部提醒一页看完、能删能重新启用。
        st.divider()
        _render_price_alerts_manager(email)

        # ── 最近搜索 ────────────────────────────────────────────────────
        try:
            _searches = get_search_history(email, limit=8)
        except Exception:
            _searches = []
        if _searches:
            st.markdown("**最近搜索**")
            _mk = {"A": "沪深", "HK": "港股", "US": "美股"}
            if st.session_state.pop("_search_open_error", None):
                st.caption("这条搜索记录查不到对应行情，可能当时就没搜到，或者标的已经退市。")
            # 整行做成链接（2026-09-04用户要求"最新搜索的股票最好能点进去显示
            # 详情界面"）。用 <a href="?open_search=..."> 的整页导航而不是
            # st.button：这一页的行全是HTML div加发丝线，插一个Streamlit按钮
            # 进去会自带一套完全不同的盒模型和间距，跟上下行对不齐。链接则是
            # 把现有的div原样包起来，外观一点不变。
            # flex 直接加在 <a> 上，里面只放两个 span，不再嵌一层 <div>。
            # 第一版是 <a> 包一个 display:flex 的 <div>，结果整行挤成了
            # "500001沪深 · 08-22"——右对齐没了、间距也没了。Streamlit 的
            # markdown 会把这段塞进 <p>，而浏览器遇到 <p> 里的块级 <div> 会
            # 提前把 <p> 闭掉，<a> 和它包着的 <div> 的父子关系当场被打散，
            # 内层的 flex 布局跟着失效。项目里别处的 *-card-link 是同一个坑
            # （文件顶部那段CSS注释记过），这里直接不嵌块级元素来规避。
            st.markdown(
                "<style>a.my-search-link, a.my-search-link:link, a.my-search-link:visited {"
                "text-decoration:none !important; color:inherit !important; cursor:pointer;"
                "display:flex !important; justify-content:space-between; align-items:baseline;"
                "padding:9px 0; border-bottom:1px solid var(--fa-border);"
                "} a.my-search-link:hover { background: rgba(23,24,28,0.02); }</style>",
                unsafe_allow_html=True,
            )
            for h in _searches:
                _t = _to_cn_dt(h.get("searched_at", ""))
                _when = _t.strftime("%m-%d") if _t else ""
                _href = (
                    f"?open_search={urllib.parse.quote(str(h.get('query','')))}"
                    f"&open_search_market={urllib.parse.quote(str(h.get('market') or 'A'))}"
                    f"{_auth_qs()}"
                )
                st.markdown(
                    f"<a class='my-search-link' href='{_href}' target='_self'>"
                    f"<span style='color:var(--fa-text);font-size:0.9rem'>{_esc(h.get('query',''))}</span>"
                    f"<span style='color:var(--fa-faint);font-size:0.78rem'>"
                    f"{_mk.get(h.get('market'), h.get('market') or '')} · {_when}</span></a>",
                    unsafe_allow_html=True,
                )

    # ── 指南与数据源（登录与否都能看）─────────────────────────────────
    st.markdown("**关于**")
    _render_app_guide()
    _render_data_source_health()


# AI咨询浮窗空对话时给的示例问题。挑的是"只有这个网站答得上来"的四个方向
# （自己的持仓/某个分数怎么来的/模拟盘在干嘛/推荐准不准），不是通用金融问答，
# 后两个正好也是审计里用户真的会问的那两个。
_ASSISTANT_EXAMPLE_QUESTIONS = (
    "我的持仓现在要不要动",
    "这支股票为什么打这个分",
    "AI模拟盘最近在买什么、为什么",
    "推荐股排行榜到底准不准",
)


@st.fragment
def _render_ai_assistant():
    """右下角"AI 咨询"悬浮按钮——不管在哪个分区都常驻显示，点开是个能聊天的
    浮窗，能看到用户自己的持仓/回看历史（登录后）+ 首页推荐股排行榜这类
    全站公开数据（游客也能看到），回答"这个网站怎么用/这个数字什么意思"
    这类问题不用用户自己去翻。

    实现思路：用st.popover而不是自己拿HTML/JS搭一个浮层——popover是
    Streamlit原生组件，不用操心sandboxed iframe/跨frame通信这些坑（这个
    项目在登录态那块已经踩过components.v1.html iframe的坑，见_BRIDGE_JS
    相关历史记录）。触发按钮本身用CSS钉在右下角固定位置——Streamlit给
    带key的组件容器自动加`st-key-<key>`这个class（持仓卡片搜索/添加按钮
    已经在用同一个技巧，见_render_position_rows附近的CSS），不用去猜
    Streamlit内部生成的DOM结构。

    2026-08-30补@st.fragment：之前这个函数没有独立fragment，发一条消息
    触发的st.chat_input提交会导致整个app.py从头重跑一遍——不管用户当时
    停在首页/行情/持仓哪个分区，那个分区自己的渲染（哪怕已经有缓存）也
    要重新跑一次DOM生成，再加上千问那次真实请求，用户反馈"消息发出去
    要好久才显示在网页上"，这是真正的原因，不是网络慢。包一个fragment，
    提交消息只重跑这个小组件自己，不牵连整个页面。跟_render_index_
    snapshot那次踩过的坑不是同一类——那次问题是run_every自动定时器
    切页后残留，这里只是交互触发重跑，不加run_every，不会有同类风险。
    """
    st.markdown(
        "<style>"
        # 这颗按钮原来是68px的品牌红大圆+800字重+很重的投影，是整页视觉上
        # 最吵的一个元素，而且正好压在首页地图的右下角。缩到48px、换成墨色、
        # 投影收到几乎看不见，往里再收一点避开地图边缘的指数标签。
        ".st-key-ai_assistant_popover{position:fixed;bottom:26px;right:26px;z-index:9999;}"
        # 浮标从"墨色实心圆"改成白底发丝描边。一个纯黑圆点浮在这套近乎无色的
        # 界面上，是页面里唯一一块高对比实色，视觉上比它承担的功能重得多
        # （用户原话是"看着怪怪的"）。改成跟次要按钮同一套语言：平时只是一个
        # 带细边的白圆，悬停才填墨色——存在感够找得到，但不抢戏。
        ".st-key-ai_assistant_popover button{"
        "border-radius:50%!important;width:50px;height:50px;padding:0!important;"
        "background:#FFFFFF!important;border:1px solid #DBDCE3!important;"
        "box-shadow:0 2px 14px rgba(23,24,28,.10)!important;"
        "transition:background .16s ease,border-color .16s ease!important;"
        "}"
        ".st-key-ai_assistant_popover button:hover{background:#17181C!important;border-color:#17181C!important;}"
        # 文字居中：Streamlit 按钮里的<p>自带上下 margin，加上 letter-spacing
        # 会在右侧多出一个字距的空白，两者叠加就是"看着没居中"。这里把
        # margin 清零、行高压到1，并用 padding-left 抵掉尾部那个字距。
        ".st-key-ai_assistant_popover button p{color:#17181C!important;font-size:.76rem!important;"
        "font-weight:600!important;letter-spacing:.08em;line-height:1!important;"
        "margin:0!important;padding-left:.08em;transition:color .16s ease;}"
        ".st-key-ai_assistant_popover button>div,"
        ".st-key-ai_assistant_popover button [data-testid='stMarkdownContainer']{"
        "display:flex!important;align-items:center!important;justify-content:center!important;"
        "width:100%!important;height:100%!important;}"
        ".st-key-ai_assistant_popover button:hover p{color:#fff!important;}"
        # 浮层本体：收掉Streamlit默认的厚投影和圆角，跟站内卡片同一套。
        "[data-testid='stPopoverBody']{border-radius:12px!important;"
        "border:1px solid #EAEAEF!important;box-shadow:0 8px 32px rgba(23,24,28,.10)!important;"
        # 2026-09-11修：浮层原来是"整块固定高度+整块滚动"，窗口矮一点
        # （实测609px高）输入框就被挤到滚动区外面去了，要在面板里往下滚
        # 才能找到输入框——等于这个功能在小窗口下是坏的。改成纵向flex：
        # 高度跟着视口走（封顶560px），消息区自己滚，输入框固定在底部
        # 永远可见。
        "display:flex!important;flex-direction:column!important;"
        # 2026-09-12再修（前端审计第10条）：
        # 一、宽度写死。原来没设宽度，popover 跟着内容自适应——空对话时窄、
        #    AI 回一段长文之后突然变宽，审计原话"发送消息后面板宽度会跳变"。
        #    聊天面板的宽度不该由某一条回答的长短决定。
        # 二、高度从 min(70vh,560px) 放宽到 min(78vh,620px)。上一版这个上限
        #    叠上内部消息容器自己的固定高度，在矮窗口下把可见消息区挤到只剩
        #    一百多像素（审计实测约130px），一条回答要在里面滚很久。
        "width:420px!important;max-width:92vw!important;"
        "max-height:min(78vh,620px)!important;overflow:hidden!important;}"
        "@media (max-width:640px){[data-testid='stPopoverBody']{width:92vw!important;}}"
        # 消息区：吃掉剩余高度并单独滚动。选的是浮层里第一层竖向容器，
        # 命中不了也只是退回原来的行为，不会把布局搞坏。
        "[data-testid='stPopoverBody']>[data-testid='stVerticalBlock']{"
        "flex:1 1 auto!important;min-height:0!important;overflow-y:auto!important;}"
        # 输入框：不参与伸缩，钉在底部，上面压一条发丝线跟消息区分开。
        "[data-testid='stPopoverBody'] [data-testid='stChatInput']{"
        "flex:0 0 auto!important;margin-top:8px!important;"
        "border-top:1px solid #EAEAEF!important;padding-top:8px!important;}"
        # popover 触发键默认会在文字右边带一个下拉小箭头。这颗按钮是个圆形
        # 浮标，里面只放两个字母，多一个箭头会挤成"AI⌄"，既不居中也不好看。
        ".st-key-ai_assistant_popover button svg,"
        ".st-key-ai_assistant_popover button [data-testid='stIconMaterial']{display:none!important;}"
        "</style>",
        unsafe_allow_html=True,
    )
    with st.popover("AI", key="ai_assistant_popover"):
        st.markdown("**AI 咨询**")

        email = st.session_state.get("user_email") if st.session_state.get("logged_in") else None
        if "_assistant_messages" not in st.session_state:
            st.session_state["_assistant_messages"] = []
        # 上下文只在浮窗打开后第一次算，同一个会话里复用——持仓/历史记录
        # 这类数据变化不快，没必要每发一条消息都重新查一遍数据库；
        # 用户明确要求"重新开一次对话"才需要最新数据可以接受这个折衷。
        if "_assistant_context" not in st.session_state:
            # build_assistant_context内部已经给最慢的那几个数据源分别加了
            # 超时保护，但这里再兜一层调用方总超时——不指望"内部每处都想
            # 到了"，只要有任何一个环节（现在的或者以后新加的）意外没被
            # 内部保护到，这层total_timeout保证浮窗最多等15秒，不会无限
            # 卡住整个弹窗打不开。超时/失败都走同一个降级文案。
            try:
                _ctx_ex = ThreadPoolExecutor(max_workers=1)
                _ctx_fut = _ctx_ex.submit(build_assistant_context, email)
                st.session_state["_assistant_context"] = _ctx_fut.result(timeout=15)
                _ctx_ex.shutdown(wait=False)
            except Exception:
                st.session_state["_assistant_context"] = "（数据加载失败，先聊网站怎么用的问题）"

        # 不用st.chat_message——它自带的默认头像是个卡通小图标，用户明确要求
        # 整个网站不许出现任何emoji/装饰性图标，且要求跟GPT一致的"用户蓝气泡/
        # AI白气泡"经典聊天条样式，改成自己拼HTML气泡，不带任何头像。
        # 示例问题按钮点击结果用局部变量接（见下面按钮那段的注释）
        _clicked_example_q = None
        bubble_box = st.container(height=320)
        with bubble_box:
            if not st.session_state["_assistant_messages"]:
                # 空对话只是个输入框，用户不知道能问啥——加一句纯展示的
                # 引导语，不写进_assistant_messages（不进模型上下文，也不会
                # 被当成一轮真实对话历史发出去）。
                # 引导语原来套了一个assistant气泡，看着像AI已经先说了一句话，
                # 但它其实是静态文案、不进上下文。改成一段安静的说明文字，
                # 不伪装成对话；顺便把能问什么按类别列清楚，比一句话更实用。
                st.markdown(
                    "<div style='padding:10px 2px 4px;color:var(--fa-faint);font-size:0.82rem;"
                    "line-height:1.9'>"
                    "我能看到你的持仓、自选、历史判断记录，以及AI模拟盘的实时状态。<br>"
                    "可以直接点下面这几个，也可以自己打字："
                    "</div>",
                    unsafe_allow_html=True,
                )
                # 2026-09-12（前端审计第10条"四个示例问题只是文字，不能点"）：
                # 做成真按钮。第一版走的是"存进 session_state 再
                # st.rerun(scope='fragment')"，实测在 popover 里点了没反应
                # （重跑之后浮层这一支的状态没接上），改成不绕 rerun：
                # st.button 在被点击的那一次运行就返回 True，把它记在局部变量
                # 里，本次运行继续往下走到下面的发送逻辑即可——发送链路跟手输
                # 完全共用一条，不给示例问题另开一条以后必然走偏的路径。
                for _i, _q in enumerate(_ASSISTANT_EXAMPLE_QUESTIONS):
                    if st.button(_q, key=f"_ai_example_q_{_i}", use_container_width=True):
                        _clicked_example_q = _q
            for m in st.session_state["_assistant_messages"]:
                st.markdown(_chat_bubble(m["role"], m["content"]), unsafe_allow_html=True)

        # 示例问题按钮把问题放进 session_state，这里跟手输的问题走完全同一条
        # 发送链路——不给它们单开一条，不然两条路径以后一定会走偏。
        prompt = st.chat_input("问点什么...") or _clicked_example_q
        if prompt:
            st.session_state["_assistant_messages"].append({"role": "user", "content": prompt})
            with bubble_box:
                st.markdown(_chat_bubble("user", prompt), unsafe_allow_html=True)
                placeholder = st.empty()
                # 2026-08-30新增：先占位显示"正在想"的三点动画，让用户在等
                # 首个字符流出来之前就看到"AI收到了、正在处理"，不是真的
                # 加速请求本身。（这里原来的注释说stream_reply会先做一次
                # 不流式的工具调用探测、单独要4-10秒——那是2026-08-30重写
                # 之前的旧实现，写重写说明时忘了同步删这段注释，2026-09-02
                # 排查聊天卡顿时读到assistant.py现在的代码才发现对不上，
                # 一并订正。现在的stream_reply从第一次请求就是流式+带
                # tools，不需要工具时首字延迟就是模型本身的生成延迟，见
                # assistant.py stream_reply的文档字符串。真正拖慢多轮
                # 聊天的是_client()每次都重新握手连接，已在assistant.py
                # 同一天的改动里修掉。）
                placeholder.markdown(_typing_indicator_html(), unsafe_allow_html=True)
                reply = ""
                try:
                    for chunk in stream_assistant_reply(
                        st.session_state["_assistant_messages"], st.session_state["_assistant_context"],
                    ):
                        reply += chunk
                        placeholder.markdown(_chat_bubble("assistant", reply + " ▌"), unsafe_allow_html=True)
                    placeholder.markdown(_chat_bubble("assistant", reply), unsafe_allow_html=True)
                except Exception as e:
                    reply = f"回答失败：{e}"
                    placeholder.markdown(_chat_bubble("assistant", reply), unsafe_allow_html=True)
            st.session_state["_assistant_messages"].append({"role": "assistant", "content": reply})


_MACRO_SECTIONS = ("一句话结论", "现状", "影响", "盯什么")


# 新股简报的段名。两个市场不一样，因为两个市场决定"要不要打"的因素本来就
# 不一样：港股散户从公开发售认购，回拨机制和绿鞋直接影响散户拿到多少货；
# 美股由承销商配售，没有回拨这回事，真正决定开盘走势的是领投行档次、流通盘
# 和锁定期。段名必须跟 ipo_brief.py 里对应的系统提示词逐字一致，_parse_ipo_text
# 是按这些名字切段的，改一边不改另一边会让整份简报解析成空白。
_IPO_SECTIONS = ("一句话结论", "公司概况", "定价与门槛", "市场热度", "绿鞋与回拨", "风险")
_IPO_SECTIONS_US = ("一句话结论", "公司概况", "定价与门槛", "承销与基石", "锁定期与流通盘", "风险")


@st.fragment
def _render_ipo_open_vs_close(items: list[dict], key_suffix: str = "hk"):
    """新股首日"开盘就卖 vs 持到收盘"（升级路线图第8条）。

    路线图原本要的是"超购倍数 vs 首日表现"散点图。超购倍数这个数据确认拿不到：
    富途的接口没有这个字段，akshare 的 stock_ipo_hk_ths 实测返回的是沪深数据
    （代码是001246/301716这种深市北交所的）而且列里塞的是抓取的页面文本，
    港交所披露易那边得逐份PDF解析。与其硬凑一个不准的数，不如换一个用现有
    数据就能回答、对打新同样实际的问题。
    """
    fig = build_ipo_open_vs_close(items)
    if fig is None:
        return
    try:
        summ = ipo_calc.summarize_history(items)
    except Exception:
        summ = {}

    st.markdown("**开盘就卖，还是持到收盘**")
    # 原来是三条 caption 堆在一起（结论 / 两个中位数 / 怎么读图），占了三行。
    # 压成一句：先给结论，再用括号补上读图规则。中位数那句砍掉——图上直接
    # 看得出来，而且结论里已经给了"高多少个百分点"这个更有用的数。
    _bits = []
    if "hold_better_rate" in summ:
        _bits.append(
            f"{summ.get('n', 0)}只里持到收盘更划算的占 {summ['hold_better_rate']:.0%}"
            f"（中位数高 {summ.get('hold_gain_median', 0):+.1f} 个百分点）"
        )
    _bits.append("虚线上方＝持到收盘更好，下方＝高开回落，左下＝开盘收盘都破发")
    st.caption("；".join(_bits) + "。")
    st.plotly_chart(fig, use_container_width=True, config=_PLOTLY_CONFIG,
                    key=f"_ipo_open_close_{key_suffix}")


def _render_ipo_calculator(items: list[dict]):
    """打新收益测算器（升级路线图第8条）。

    中签率必须由用户自己填：港交所的分配结果逐只公布，而且同一只票不同认购
    档位的中签率差很多，硬猜一个默认值比留空更误导。预期涨幅默认用历史中位数
    而不是均值——新股首日收益是长尾分布，一只翻倍能把均值拉高十几个点。
    """
    try:
        summ = ipo_calc.summarize_history(items)
    except Exception:
        return
    default_move = summ.get("close_median")
    if default_move is None:
        return

    with st.expander("打新收益测算器"):
        st.caption("算的是长期重复打新的平均结果；单次要么中0手要么中1手，实际会大幅跳变。")
        c1, c2, c3 = st.columns(3)
        with c1:
            lot_price = st.number_input("每手入场费（港币）", min_value=0.0,
                                        value=5000.0, step=500.0, key="_ipo_lot_price")
            lots = st.number_input("认购手数", min_value=1, value=10, step=1,
                                   key="_ipo_lots")
        with c2:
            hit = st.number_input("中签率（%）", min_value=0.0, max_value=100.0,
                                  value=20.0, step=1.0, key="_ipo_hit",
                                  help="券商认购页面或事后公告里有，我们没有这个数据源，需要你自己填")
            move = st.number_input("预期首日涨幅（%）", value=float(round(default_move, 1)),
                                   step=1.0, key="_ipo_move",
                                   help=f"默认填的是最近{summ.get('n', 0)}只的收盘涨幅中位数")
        with c3:
            margin_pct = st.slider("孖展（融资）比例 %", 0, 95, 0, step=5,
                                   key="_ipo_margin")
            rate = st.number_input("融资年利率（%）", min_value=0.0, value=5.0,
                                   step=0.5, key="_ipo_rate")
        days = st.slider("冻结天数", 1, 14, 7, key="_ipo_days")

        res = ipo_calc.estimate(
            lot_price, int(lots), hit, move,
            margin_ratio=margin_pct / 100.0, margin_rate_pct=rate,
            margin_days=int(days), fee_per_subscription=100.0,
        )
        if not res:
            return
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("认购总额", f"HK${res['subscribe_amount']:,.0f}")
        m2.metric("自有资金", f"HK${res['own_capital']:,.0f}")
        m3.metric("利息成本", f"HK${res['interest']:,.0f}")
        m4.metric("预期净收益", f"HK${res['net_profit']:,.0f}")

        # 下面几条 caption 里的 HK\$ 必须转义，不要"顺手清理"掉那个反斜杠。
        # Streamlit 的 markdown 把成对的 $...$ 当 LaTeX 公式渲染：同一条 caption
        # 里出现两个 HK$（比如"借来的 HK$22,500"和"净亏 HK$122"），中间整段会被
        # 当成数学公式排成斜体，两个美元符号本身还会被吃掉——线上实际出现过，
        # 截图里是"借来的 HK22,500计的…净亏 𝐻𝐾122"。
        # st.metric 不走 markdown，所以上面那四个 f"HK${...}" 不用转义。
        st.caption(
            f"预期中签 HK\\${res['allotted_amount']:,.0f}，"
            f"首日需涨 **{res.get('breakeven_move_pct', 0):.2f}%** 才够覆盖利息和手续费。"
        )
        if margin_pct > 0:
            st.caption(
                f"利息按借来的 HK\\${res['borrowed']:,.0f} 计，跟中不中签无关——"
                f"一手没中就净亏 HK\\${res['interest'] + res['fee']:,.0f}。"
            )
        if "return_on_own_capital" in res:
            st.caption(f"自有资金回报率 {res['return_on_own_capital']:+.2%}——融资放大回报，也同样放大亏损。")


def _render_us_ipo_calculator(items: list[dict]):
    """美股打新测算器（2026-09-13，用户要求美股跟港股一致）。

    不是把港股那个换成美元。美股 IPO 的机制不同，照搬会让每一个输入框都在问
    一个不存在的数：

      每手入场费  美股按股认购，没有"一手"这个单位；
      中签率      港股是交易所公开分配、事后逐只公告的比率；美股散户拿到的是
                  承销团分给零售渠道的配额，比例由券商自己定、不公开，所以这里
                  的输入项叫"获配比例"而不是"中签率"——它们不是同一件事，
                  用同一个词会让人以为有个官方数字可查；
      孖展        美股 IPO 认购是现金冻结，券商不提供融资认购，整块砍掉。

    真正值得算的反而是港股那个算不了的一件事：**开盘就卖 vs 持到收盘**。
    美股新股首日振幅比港股大得多，这两个选择的差距常常比"打不打"本身更大，
    而这两个数我们的 items 里都有（open_pct / first_day_pct），是真实统计
    不是假设，所以这里两个都算出来并排摆。
    """
    try:
        summ = ipo_calc.summarize_history(items)
    except Exception:
        return
    _close_med = summ.get("close_median")
    _open_med = summ.get("open_median")
    if _close_med is None:
        return

    with st.expander("打新收益测算器"):
        st.caption("算的是长期重复参与的平均结果；单次配额要么是0要么很小，实际会大幅跳变。")
        c1, c2, c3 = st.columns(3)
        with c1:
            amount = st.number_input("认购金额（美元）", min_value=0.0, value=10000.0,
                                     step=1000.0, key="_us_ipo_amount")
        with c2:
            alloc = st.number_input("预期获配比例（%）", min_value=0.0, max_value=100.0,
                                    value=10.0, step=1.0, key="_us_ipo_alloc",
                                    help="承销团分给零售渠道的配额比例，由券商自己定、不公开，需要你按自己的经验填")
        with c3:
            fee = st.number_input("认购手续费（美元）", min_value=0.0, value=0.0,
                                  step=5.0, key="_us_ipo_fee")

        # 两种卖法各算一遍。margin 全部传 0：美股 IPO 认购是现金冻结，
        # 券商不提供融资认购，留着这条路只会算出一个不可能发生的场景。
        _rows = []
        for _label, _move in (("开盘就卖", _open_med), ("持到收盘", _close_med)):
            if _move is None:
                continue
            r = ipo_calc.estimate(amount, 1, alloc, _move,
                                  margin_ratio=0.0, margin_rate_pct=0.0,
                                  margin_days=0, fee_per_subscription=fee)
            if r:
                _rows.append((_label, _move, r))
        if not _rows:
            return

        # 负号写在货币符号前面（-$2），不是后面（$-2）。后者是 f-string 直接拼
        # 出来的样子，看着像个笔误。
        def _usd(v: float) -> str:
            return f"-${abs(v):,.0f}" if v < 0 else f"${v:,.0f}"

        _cols = st.columns(len(_rows))
        for _col, (_label, _move, r) in zip(_cols, _rows):
            with _col:
                st.metric(f"{_label}（首日中位数 {_move:+.1f}%）",
                          _usd(r["net_profit"]))
        _r0 = _rows[0][2]
        st.caption(f"预期获配 \\${_r0['allotted_amount']:,.0f}——"
                   f"这个数才是真正在赚钱的部分，认购金额里剩下的会在上市当天退回。")
        if len(_rows) == 2:
            _d_open, _d_close = _rows[0][2]["net_profit"], _rows[1][2]["net_profit"]
            _better = "持到收盘" if _d_close > _d_open else "开盘就卖"
            st.caption(f"按近{summ.get('n', 0)}只的中位数，{_better}多赚 "
                       f"\\${abs(_d_close - _d_open):,.0f}——但这是中位数，"
                       f"单只的开盘和收盘经常差出几十个百分点。")


def _render_a_ipo_briefs():
    """沪深新股认购专区（2026-09-13 用户要求，参照港股那块）。

    没有照搬港股的 AI 简报模式：那块的招股要素（保荐人/基石/超额认购/绿鞋）
    是 ipo_brief.py 这条独立 cron 用 AI 从公开网页挖出来再落库的，沪深没有对应
    的管线，硬做等于再起一条要维护的 AI 链路。

    改成围绕一个 沪深独有、而且富途接口直接给的硬指标：**发行市盈率 vs 行业
    市盈率**。这是沪深打新判断"贵不贵"的通行口径——发行PE显著高于行业PE的，
    上市后向行业均值回归的压力就大。港股和美股的接口都没有这两个字段，所以
    这块的形态跟港股那块本来就该不一样，不是偷懒。

    只读接口不调AI，跟宏观/港股新股同一个原则：首页渲染路径上不跑模型。
    """
    try:
        ipos = get_ipo_calendar("A", limit=8)
    except Exception:
        return
    if not ipos:
        return

    # 未定价的单独归一类：沪深在申购前几天才公布发行价，这期间接口返回的是0。
    # 把0当成"发行价0元"显示出来是错的，当成"没有这只票"藏掉也是错的。
    priced = [ip for ip in ipos if (ip.get("ipo_price") or 0) > 0]
    unpriced = [ip for ip in ipos if (ip.get("ipo_price") or 0) <= 0]

    if priced:
        st.caption("发行市盈率高于行业市盈率越多，上市后向行业均值回归的压力越大——"
                   "这是沪深打新最直接的一个贵贱参照，但它只说估值，不代表公司好坏。")
    for ip in priced:
        _ipo_pe = ip.get("issue_pe") or 0
        _ind_pe = ip.get("industry_pe") or 0
        _ratio_txt, _ratio_color = "", "var(--fa-faint)"
        if _ipo_pe > 0 and _ind_pe > 0:
            _r = _ipo_pe / _ind_pe
            _ratio_txt = f"{_r:.2f}× 行业"
            # 红=偏贵绿=偏宜，跟全站涨跌色一致：高于行业用涨色（要警惕），
            # 低于行业用跌色。这里的语义是"估值位置"不是"赚赔"，所以配文
            # 必须写清楚，不能让人按"红=好"去读。
            _ratio_color = UP_COLOR if _r > 1.15 else (DOWN_COLOR if _r < 0.9 else "var(--fa-text-2)")
        st.markdown(
            f"<div style='display:flex;align-items:baseline;gap:10px;padding:8px 2px;"
            f"border-bottom:1px solid var(--fa-border)'>"
            f"<span style='flex:1;color:var(--fa-text);font-size:0.88rem;font-weight:600'>"
            f"{_esc(ip['name'])}"
            f"<span style='color:var(--fa-faint);font-size:0.76rem;font-weight:400'> "
            f"{_esc(ip['symbol'])}</span></span>"
            f"<span style='color:var(--fa-text-2);font-size:0.8rem;min-width:92px'>"
            f"发行价 {ip['ipo_price']:,.2f}</span>"
            f"<span style='color:var(--fa-faint);font-size:0.78rem;min-width:150px'>"
            f"发行PE {_ipo_pe:,.1f} / 行业 {_ind_pe:,.1f}</span>"
            f"<span style='color:{_ratio_color};font-size:0.82rem;font-weight:600;"
            f"min-width:80px;text-align:right'>{_esc(_ratio_txt)}</span></div>",
            unsafe_allow_html=True,
        )
    if unpriced:
        st.caption(
            "另有 " + "、".join(f"{_esc(ip['name'])}（{_esc(ip['symbol'])}）" for ip in unpriced)
            + " 尚未公布发行价——沪深通常在申购前几天才定价，不是数据缺失。"
        )


def _render_us_ipo_briefs():
    """美股新股（2026-09-13 用户要求做成跟港股一致，标签切换）。

    首日表现统计/开盘vs收盘散点/按月拆解跟港股完全同构，共用
    _render_ipo_perf_block——底层那条"上市首日 last_close 即发行价"的约定
    在美股一样成立（实测 US.AAC.U：last_close=10.0、close=10.05、+0.5%）。

    2026-09-13 补齐到跟港股一致（用户："港股新股下面也有美股那边也要移植"）：
    打新收益测算器和 AI 招股简报这两块原来美股没有，现在都有了，但都不是把
    港股那份换个币种——机制不同，见 _render_us_ipo_calculator 和
    ipo_brief.py 里 _US_SYSTEM 上方的注释。

    富途一次返回上百条（实测102条），其中很多是没有确定上市日的。只列出已经
    定了日期、且还没上市的，按日期升序——没定日期的堆在页面上没有行动价值。
    """
    # 首日表现统计跟港股共用同一块（数据由 ipo_brief.py 按 market="US" 另存一份）。
    try:
        _perf_us = get_latest_ipo_performance(market="US")
    except Exception:
        _perf_us = {}
    _render_ipo_perf_block(_perf_us, show_calculator=True, key_suffix="us")

    # AI 招股简报。港股那块叫"即将上市与认购中"，美股这里只能叫"即将上市"——
    # 美股没有公开认购窗口，接口的 apply_end_time 全是 N/A，写"认购中"是编的。
    try:
        _us_briefs = get_latest_ipo_briefs(limit=4, market="US")
    except Exception:
        _us_briefs = []
    if _us_briefs:
        st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)
        st.markdown(
            "<div style='font-size:0.76rem;color:var(--fa-faint);margin:2px 0 4px'>"
            "即将上市</div>", unsafe_allow_html=True)
        st.markdown(
            "<div style='font-size:0.76rem;color:var(--fa-faint);margin:2px 0 10px'>"
            "发行数据来自交易接口，承销商／基石／锁定期来自公开资讯，"
            "资讯里没提到的一律标注未查到</div>",
            unsafe_allow_html=True,
        )
        _render_ipo_brief_cards(_us_briefs, market="US")

    try:
        ipos = get_ipo_calendar("US", limit=60)
    except Exception:
        ipos = []
    _today = cn_now().strftime("%Y-%m-%d")
    # 已经出了 AI 简报的不在下面重复列一遍——那几只上面已经有完整的一张卡了，
    # 再出现在日程表里只会让人以为是两只不同的票。
    _done = {str(b.get("symbol") or "") for b in _us_briefs}
    upcoming = [ip for ip in ipos
                if (ip.get("list_date") or "") >= _today and ip["symbol"] not in _done][:8]
    if not upcoming:
        if not (_perf_us or {}).get("stats") and not _us_briefs:
            st.caption("暂时没有美股新股数据。")
        return

    st.markdown(
        "<div style='font-size:0.76rem;color:var(--fa-faint);margin:14px 0 6px'>"
        "其余日程</div>", unsafe_allow_html=True)
    st.caption("美股 IPO 由承销商配售，散户通过券商拿到的是零售渠道配额，没有公开认购窗口。")
    for ip in upcoming:
        _lo, _hi = ip.get("price_min"), ip.get("price_max")
        if _lo and _hi:
            _pr = f"{_lo:,.2f}" if _lo == _hi else f"{_lo:,.2f} - {_hi:,.2f}"
        elif ip.get("ipo_price"):
            _pr = f"{ip['ipo_price']:,.2f}"
        else:
            _pr = "定价待定"
        st.markdown(
            f"<div style='display:flex;align-items:baseline;gap:10px;padding:8px 2px;"
            f"border-bottom:1px solid var(--fa-border)'>"
            f"<span style='color:var(--fa-faint);font-size:0.78rem;min-width:78px'>"
            f"{_esc(ip.get('list_date') or '待定')}</span>"
            f"<span style='flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;"
            f"white-space:nowrap;color:var(--fa-text);font-size:0.86rem'>{_esc(ip['name'])}"
            f"<span style='color:var(--fa-faint);font-size:0.76rem'> {_esc(ip['symbol'])}</span></span>"
            f"<span style='color:var(--fa-text-2);font-size:0.8rem;min-width:118px;"
            f"text-align:right'>发行价 {_esc(_pr)}</span></div>",
            unsafe_allow_html=True,
        )


def _render_ipo_perf_block(perf: dict, show_calculator: bool = True, key_suffix: str = "hk"):
    """新股"首日表现统计 + 开盘vs收盘散点 + 按月拆解"这一整块。

    2026-09-13 从 _render_ipo_briefs 里抽出来，给港股和美股共用——用户要求
    美股那块做成跟港股一致并且能标签切换。两个市场的这部分完全同构：底层都是
    "上市首日那根日K的 last_close 即发行价"这条约定（实测美股同样成立）。

    show_calculator 为真时按 key_suffix 分派到各自的测算器：港股那个算中签率
    和孖展，美股那个算获配比例和"开盘卖 vs 收盘卖"。两者不能互换，见
    _render_us_ipo_calculator 的 docstring。

    key_suffix 必须按市场给不同的值：st.tabs 会把**所有**标签页的内容都渲染
    出来（不是点到才渲染），所以港股和美股这两块是同时存在于页面上的，图表
    的 key 写死就会撞 StreamlitDuplicateElementKey，整块报错。
    """
    _st = (perf or {}).get("stats") or {}
    if _st.get("count"):
        # 取到数据的只数和窗口内实际上市只数不一定相等（美股差很多，见
        # data_sources 里 listed_in_window 的注释）。不相等时如实写成"N只中的M只"，
        # 否则会把抽样说成全样本。
        _listed = _st.get("listed_in_window") or _st["count"]
        _scope = (f"近{_st['days']}天已上市 {_st['count']} 只的首日表现"
                  if _listed <= _st["count"] else
                  f"近{_st['days']}天已上市 {_listed} 只，其中 {_st['count']} 只取到首日数据")
        st.markdown(
            f"<div style='font-size:0.76rem;color:var(--fa-faint);margin:2px 0 8px'>"
            f"{_esc(_scope)}</div>",
            unsafe_allow_html=True,
        )
        # 上涨占比这一项是用户看到"首日平均+55.7%"时问出来的：他不确定这个数
        # 是涨跌幅的均值，还是上涨股票占全部新股的比例。两种读法差别极大——
        # 均值55.7%是被几只翻三倍的票拉起来的，而实际上涨的只有64%。既然一个
        # 标签解释不清，就把两个数并排列出来，歧义自然消失。
        # 用 items 现算而不是读 stats：库里已有的那条记录是加这个字段之前写的，
        # 从 stats 取会是空的，而 items 一直都在。
        _ups = [i for i in ((perf or {}).get("items") or []) if (i.get("first_day_pct") or 0) > 0]
        _up_rate = len(_ups) / _st["count"] * 100 if _st.get("count") else 0.0

        # 2026-09-12改（前端审计第13条手机端 + 第15条颜色语义）：
        # 一、原来用 st.columns(5)，Streamlit 在窄屏会把列竖着堆起来，五个
        #     数字占掉整整一屏（审计原话"竖着排成一列，太占地方"）。改成一个
        #     自己控制的 grid：桌面五列，手机两列，不依赖 st.columns 的断点。
        # 二、"上涨占比"原来用涨色（红）、"破发率"用跌色（绿）。这两个是
        #     比率，不是方向——按红涨绿跌的习惯读，绿色的破发率会被读成
        #     "好事"，可破发率高恰恰是坏事。比率型指标一律走中性色，红绿
        #     只留给真正的涨跌（均值/中位数那两个是涨跌幅本身，保留配色）。
        _avg_c = UP_COLOR if _st["avg"] > 0 else DOWN_COLOR
        _med_c = UP_COLOR if _st["median"] > 0 else DOWN_COLOR
        _cells = [
            ("首日涨跌幅均值", f"{_st['avg']:+.1f}%", _avg_c, "1.3rem"),
            ("中位数", f"{_st['median']:+.1f}%", _med_c, "1.3rem"),
            ("上涨占比", f"{_up_rate:.0f}%", "var(--fa-text)", "1.3rem"),
            ("破发率", f"{_st['break_rate']:.0f}%", "var(--fa-text)", "1.3rem"),
            ("区间", f"{_st['min']:+.0f}% ~ {_st['max']:+.0f}%", "var(--fa-text)", "1.05rem"),
        ]
        st.markdown(
            "<style>.fa-ipo-stats{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}"
            "@media (max-width:640px){.fa-ipo-stats{grid-template-columns:repeat(2,1fr)}}</style>"
            "<div class='fa-ipo-stats'>"
            + "".join(
                f"<div><div style='font-size:0.76rem;color:var(--fa-faint)'>{_esc(_label)}</div>"
                f"<div style='font-size:{_size};font-weight:600;color:{_color};"
                f"font-variant-numeric:tabular-nums'>{_esc(_val)}</div></div>"
                for _label, _val, _color, _size in _cells
            )
            + "</div>",
            unsafe_allow_html=True,
        )
        st.caption(f"{_st['count']}只里{len(_ups)}只首日收涨。首日收益是长尾分布，"
                   f"均值被少数翻倍股拉高——看中位数更接近实际。")

        _monthly = (perf or {}).get("monthly") or []
        _items = (perf or {}).get("items") or []

        _render_ipo_open_vs_close(_items, key_suffix=key_suffix)
        if show_calculator:
            if key_suffix == "us":
                _render_us_ipo_calculator(_items)
            else:
                _render_ipo_calculator(_items)

        with st.expander(f"按月拆解与逐只明细（{len(_items)} 只）"):
            if _monthly:
                st.markdown(
                    "<div style='font-size:0.76rem;color:var(--fa-faint);margin-bottom:6px'>按上市月份</div>",
                    unsafe_allow_html=True)
                for mm in _monthly:
                    _c = UP_COLOR if mm["avg"] > 0 else DOWN_COLOR
                    st.markdown(
                        f"<div style='display:flex;align-items:baseline;gap:10px;padding:7px 2px;"
                        f"border-bottom:1px solid var(--fa-border)'>"
                        f"<span style='flex:1;color:var(--fa-text);font-size:0.86rem'>{_esc(mm['month'])}</span>"
                        f"<span style='color:var(--fa-faint);font-size:0.78rem'>{mm['count']} 只</span>"
                        f"<span style='flex:1;text-align:right;color:{_c};font-size:0.86rem'>"
                        f"平均 {mm['avg']:+.1f}%</span>"
                        f"<span style='color:var(--fa-faint);font-size:0.78rem;min-width:84px;text-align:right'>"
                        f"破发 {mm['break_rate']:.0f}%</span></div>",
                        unsafe_allow_html=True)
            if _items:
                st.markdown(
                    "<div style='font-size:0.76rem;color:var(--fa-faint);margin:14px 0 6px'>"
                    "逐只（按上市日倒序）</div>", unsafe_allow_html=True)
                for it in _items[:40]:
                    _c = UP_COLOR if it["first_day_pct"] > 0 else DOWN_COLOR
                    _op = (f"开盘 {it['open_pct']:+.0f}%" if it.get("open_pct") is not None else "")
                    st.markdown(
                        f"<div style='display:flex;align-items:baseline;gap:10px;padding:7px 2px;"
                        f"border-bottom:1px solid var(--fa-border)'>"
                        f"<span style='color:var(--fa-faint);font-size:0.76rem;min-width:78px'>"
                        f"{_esc(it['list_date'])}</span>"
                        f"<span style='flex:2;color:var(--fa-text);font-size:0.86rem'>{_esc(it['name'])}"
                        f"<span style='color:var(--fa-faint);font-size:0.76rem'> {_esc(it['symbol'])}</span></span>"
                        f"<span style='color:var(--fa-faint);font-size:0.76rem;min-width:86px;text-align:right'>"
                        f"招股 {it['offer_price']:,.2f}</span>"
                        f"<span style='color:var(--fa-faint);font-size:0.76rem;min-width:80px;text-align:right'>"
                        f"{_esc(_op)}</span>"
                        f"<span style='color:{_c};font-size:0.88rem;min-width:80px;text-align:right'>"
                        f"{it['first_day_pct']:+.1f}%</span></div>",
                        unsafe_allow_html=True)


def _render_ipo_briefs():
    """港股新股认购专区。

    2026-09-05用户要求。跟宏观专区同一个模式：数据和AI判断预先算好落库，
    首页只读一行，不在渲染路径里跑AI。

    列表只显示还没上市的——打新窗口过了之后这块内容对用户就没有行动价值了，
    留在页面上只会占位置。认购截止日单独标出来并且做了紧迫度提示：新股最容易
    错过的不是"不知道有这只"，是"知道但忘了截止日"。
    """
    try:
        briefs = get_latest_ipo_briefs(limit=6)
    except Exception:
        briefs = []

    import datetime as _d

    st.markdown("<div style='height:26px'></div>", unsafe_allow_html=True)

    # 先摆近期整体表现，再列具体新股。顺序是有意的：打新最该先看的不是某一只
    # 的招股书，而是"最近这个窗口打新整体赚不赚钱"——破发率六成的时候，单只
    # 的基本面再好也要掂量一下。
    try:
        perf = get_latest_ipo_performance()
    except Exception:
        perf = {}

    # 简报不是在页面渲染时生成的。没有最近一次任务的时间，就不能把空列表
    # 表述成“当前没有”；尤其是任务失效时，那会把“未知”伪装成“没有”。
    updated_at = (perf or {}).get("created_at")
    updated_text = ""
    is_stale = True
    if updated_at:
        try:
            updated = _d.datetime.fromisoformat(updated_at)
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            updated_text = updated.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
            is_stale = (datetime.now(timezone.utc) - updated.astimezone(timezone.utc)) > timedelta(hours=24)
        except (TypeError, ValueError):
            pass
    # 常规的"数据更新：X（北京时间）"和周末那条解释全部去掉——用户
    # 2026-09-13："什么刷新时间刷新日期什么的时效性的话都可以删掉"。
    # 这类时间戳每页都挂一条，累积起来是纯噪音，而且数据正常时它不回答任何
    # 问题。ipo_brief 的定时任务是 `25 7 * * 1-5`，周末本来就不跑，所以周末
    # 也不再提示。
    #
    # 只保留一种情况：**交易日**超过24小时没更新——那是真出问题了，不说
    # 用户会把过期的认购清单当成当前的。这不是时效性噪音，是安全提示。
    _today_cn = datetime.now(ZoneInfo("Asia/Shanghai"))
    _no_refresh_today = _today_cn.weekday() >= 5  # 5=周六 6=周日
    if is_stale and not _no_refresh_today:
        st.warning("新股数据超过24小时未更新，以下不应视为当前认购清单，请以券商页面为准。")
    if not briefs:
        st.caption("当前没有处于认购期、且尚未上市的港股新股。")
        return

    # 首日表现统计/散点/按月拆解跟美股共用同一个函数（见 _render_ipo_perf_block）。
    _render_ipo_perf_block(perf, show_calculator=True, key_suffix="hk")
    st.markdown("<div style='height:16px'></div>", unsafe_allow_html=True)

    st.markdown(
        "<div style='font-size:0.76rem;color:var(--fa-faint);margin:2px 0 4px'>"
        "即将上市与认购中</div>", unsafe_allow_html=True)
    st.markdown(
        "<div style='font-size:0.76rem;color:var(--fa-faint);margin:2px 0 10px'>"
        "招股数据来自交易接口，保荐人／基石／超额认购／绿鞋回拨来自公开资讯，"
        "资讯里没提到的一律标注未查到</div>",
        unsafe_allow_html=True,
    )

    _render_ipo_brief_cards(briefs, market="HK")


def _render_ipo_brief_cards(briefs: list[dict], market: str = "HK"):
    """一只新股一张卡：名称 + 代码 + AI结论 + 关键数字，展开看全文。

    2026-09-13从 _render_ipo_briefs 里抽出来给美股复用。两个市场共用同一套
    版式是对的——用户在标签间切换时，同一个位置应该是同一个意思的东西。
    真正按市场分叉的只有三处，都是数据决定的，不是样式偏好：

      认购截止日   港股有公开认购窗口，倒计时是这块最有行动价值的信息；
                  美股没有这个概念（接口的 apply_end_time 全是 N/A），
                  摆一个空位比不摆更糟。
      入场费/每手   同上，美股按股买，没有"一手"。
      段名         见 _IPO_SECTIONS / _IPO_SECTIONS_US 上方那段注释。
    """
    import datetime as _dd
    today = _dd.date.today()
    _sections = _IPO_SECTIONS_US if market == "US" else _IPO_SECTIONS

    for b in briefs:
        parts = _parse_ipo_text(_clean_ai_markdown(b.get("brief_text", "")), market)
        head = parts.get("一句话结论", "")
        # 结论词决定颜色：值得申购用涨色，建议回避用跌色，其余中性。
        _tone = "var(--fa-muted)"
        if "值得申购" in head:
            _tone = UP_COLOR
        elif "建议回避" in head:
            _tone = DOWN_COLOR

        try:
            facts = json.loads(b.get("facts_json") or "{}")
        except Exception:
            facts = {}

        # 认购截止的紧迫度（只有港股有公开认购窗口）
        _due_txt = ""
        _due = (b.get("apply_end") or "") if market != "US" else ""
        if _due:
            try:
                left = (_dd.date.fromisoformat(_due) - today).days
                if left < 0:
                    _due_txt = f"认购已截止（{_due}）"
                elif left == 0:
                    _due_txt = "今日认购截止"
                else:
                    _due_txt = f"认购截止 {_due}（还有{left}天）"
            except Exception:
                _due_txt = f"认购截止 {_due}"

        _meta = []
        if b.get("list_date"):
            _meta.append(f"{b['list_date']} 上市")
        _pmin, _pmax = facts.get("price_min"), facts.get("price_max")
        if market == "US":
            if _pmin and _pmax:
                _meta.append(f"发行价 {_pmin:,.2f}"
                             + (f"-{_pmax:,.2f}" if _pmax != _pmin else ""))
            if facts.get("issue_size"):
                _meta.append(f"发行 {int(facts['issue_size']):,} 股")
                if _pmin and _pmax:
                    _raise = int(facts["issue_size"]) * (float(_pmin) + float(_pmax)) / 2.0
                    _meta.append(f"募资约 {_raise / 1e6:,.0f}M")
        else:
            if facts.get("entrance_price"):
                _meta.append(f"入场费 HK${facts['entrance_price']:,.0f}")
            if facts.get("lot_size"):
                _meta.append(f"每手 {int(facts['lot_size'])}股")
            if _pmin and _pmax:
                _meta.append(f"招股价 {_pmin:,.2f}"
                             + (f"-{_pmax:,.2f}" if _pmax != _pmin else ""))

        st.markdown(
            f"<div style='padding:14px 0 4px;border-top:1px solid var(--fa-border)'>"
            f"<div style='display:flex;align-items:baseline;gap:10px;flex-wrap:wrap'>"
            f"<span style='font-weight:600;font-size:0.98rem;color:var(--fa-text)'>{_esc(b['name'])}</span>"
            f"<span style='color:var(--fa-faint);font-size:0.78rem'>{_esc(b['symbol'])}</span>"
            f"<span style='color:{_tone};font-size:0.86rem;flex:1'>{_esc(head)}</span></div>"
            f"<div style='color:var(--fa-faint);font-size:0.78rem;margin-top:5px'>"
            f"{_esc(' · '.join(_meta))}{('　' + _esc(_due_txt)) if _due_txt else ''}</div></div>",
            unsafe_allow_html=True,
        )
        # key 必须带市场后缀：st.tabs 会把所有标签页的内容都渲染出来（不是点到
        # 才渲染），港股和美股同时在场，不加后缀会撞 key。
        with st.expander("展开详情"):
            for name in _sections[1:]:
                body = parts.get(name)
                if body:
                    st.markdown(_labeled_line(name, body), unsafe_allow_html=True)
            try:
                srcs = json.loads(b.get("sources_json") or "[]")
            except Exception:
                srcs = []
            if srcs:
                st.caption(f"参考资讯 {len(srcs)} 条")


def _parse_ipo_text(text: str, market: str = "HK") -> dict:
    """按段名切开新股简报。跟 _parse_advice_text 同一套约定和同样的加粗容忍。

    段名按市场取（见 _IPO_SECTIONS / _IPO_SECTIONS_US）。这里不做"两套段名
    都试一遍"的兜底：那样会把一份港股简报里恰好出现的"风险"段落塞进美股的
    解析结果，掩盖掉"提示词和解析器对不上"这种真正需要暴露的配置错误。
    """
    parts: dict = {}
    text = text or ""
    positions = []
    for name in (_IPO_SECTIONS_US if market == "US" else _IPO_SECTIONS):
        m = re.search(r"(?:^|\n)\s*\*{0,2}" + re.escape(name) + r"\*{0,2}\s*[：:]", text)
        if m:
            positions.append((m.start(), name, m.end()))
    positions.sort()
    for i, (idx, name, body_start) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        body = text[body_start:end].strip()
        if body:
            parts[name] = body
    return parts


@st.fragment
def _render_event_calendar():
    """财经日历——接下来几天要公布什么数据、有哪些评级变动。

    2026-09-04新增。这个项目原本只回答"发生了什么"：行情是已成交的价格，
    宏观专区是已公布的数据，AI判断是基于已有信息的结论。唯独缺"接下来要
    发生什么"，而对做决策的人来说这恰恰最可操作——知道周四晚上有CPI，
    今天就不会在周三满仓押一个方向。

    做成 fragment：这两份数据的刷新节奏（3小时/6小时）跟首页其余内容完全
    不同，混在一起会让整个首页跟着它一起重跑。
    """
    try:
        events = get_economic_events(days=14)
    except Exception:
        events = []
    try:
        ratings = get_rating_changes("US")
    except Exception:
        ratings = []

    # 财报日历刻意只查"我关心的股票"，不列全市场。富途一个7天窗口就返回90条
    # 全美股财报，堆在首页是纯噪音——用户真正在意的只有自己持仓、自选、以及
    # 排行榜上被推荐的那几支。按需过滤之后这块才跟项目其余部分真正联动：
    # "你持仓的甲骨文下周四出财报"是有行动价值的，"今天有37家公司出财报"不是。
    _watch: set = set()
    try:
        _em = st.session_state.get("user_email")
        if _em:
            for pp in get_positions(_em):
                if pp.get("market") == "US":
                    _watch.add(str(pp.get("symbol", "")).upper())
        for _mk, _rows in (get_latest_advice(limit_per_market=5) or {}).items():
            if _mk == "US":
                for _r in _rows or []:
                    _watch.add(str(_r.get("symbol", "")).upper())
    except Exception:
        pass
    try:
        earnings = get_earnings_dates(tuple(sorted(_watch)), days=21) if _watch else {}
    except Exception:
        earnings = {}

    if not events and not ratings and not earnings:
        return

    st.markdown("<div style='height:26px'></div>", unsafe_allow_html=True)
    st.markdown("**财经日历**")

    if events:
        st.markdown(
            "<div style='font-size:0.76rem;color:var(--fa-faint);margin:2px 0 8px'>"
            "未来两周的重要经济数据　待公布的排在前面，已公布的带实际值</div>",
            unsafe_allow_html=True,
        )
        _star_text = {"HIGH": "高", "MEDIUM": "中"}
        for e in events[:12]:
            _actual = e["actual"].strip()
            # 已公布 vs 待公布用不同的视觉重量：待公布的是"要盯的事"，
            # 已公布的只是背景，把已公布的压暗，让待公布的自己浮出来。
            _done = not e.get("upcoming", not _actual)
            _main_color = "var(--fa-faint)" if _done else "var(--fa-text)"
            _right = (
                f"实际 {_esc(_actual)}" if _done
                else (f"预期 {_esc(e['consensus'])}" if e["consensus"].strip() else "待公布")
            )
            st.markdown(
                f"<div style='display:flex;align-items:baseline;gap:10px;padding:8px 2px;"
                f"border-bottom:1px solid var(--fa-border)'>"
                f"<span style='color:var(--fa-faint);font-size:0.78rem;min-width:76px'>"
                f"{_esc(e['date'])} {_esc(e['time'])}</span>"
                f"<span style='flex:1;color:{_main_color};font-size:0.86rem'>{_esc(e['title'])}</span>"
                f"<span style='color:var(--fa-faint);font-size:0.74rem'>"
                f"{_star_text.get(e['star'], '')}</span>"
                f"<span style='color:var(--fa-text-2);font-size:0.8rem;min-width:96px;text-align:right'>"
                f"{_right}</span></div>",
                unsafe_allow_html=True,
            )

    if earnings:
        st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)
        st.markdown(
            "<div style='font-size:0.76rem;color:var(--fa-faint);margin:2px 0 8px'>"
            "你的持仓与关注股接下来的财报日</div>",
            unsafe_allow_html=True,
        )
        for _sym, _e in sorted(earnings.items(), key=lambda kv: kv[1].get("date") or "9999"):
            # 预期EPS原样打印会出现"31.1615"这种四位小数（2026-09-11前端审计
            # 抓到）——EPS是每股收益，行业惯例两位小数，多出来的位数不是精度
            # 是噪声。拿不到数字时(None/字符串)退回原样显示，不硬转崩掉这一行。
            try:
                _eps = f"预期EPS {float(_e['eps_predict']):.2f}" if _e.get("eps_predict") is not None else ""
            except (TypeError, ValueError):
                _eps = f"预期EPS {_e['eps_predict']}" if _e.get("eps_predict") else ""
            st.markdown(
                f"<div style='display:flex;align-items:baseline;gap:10px;padding:8px 2px;"
                f"border-bottom:1px solid var(--fa-border)'>"
                f"<span style='color:var(--fa-faint);font-size:0.78rem;min-width:76px'>"
                f"{_esc(_e.get('date', ''))}</span>"
                f"<span style='flex:1;color:var(--fa-text);font-size:0.86rem'>"
                f"{_esc(_e.get('name') or _sym)}"
                f"<span style='color:var(--fa-faint);font-size:0.78rem'> {_esc(_sym)}</span></span>"
                f"<span style='color:var(--fa-faint);font-size:0.78rem'>{_esc(_e.get('period', ''))}</span>"
                f"<span style='color:var(--fa-text-2);font-size:0.8rem;min-width:118px;text-align:right'>"
                f"{_esc(_eps)}</span></div>",
                unsafe_allow_html=True,
            )

    if ratings:
        st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)
        st.markdown(
            "<div style='font-size:0.76rem;color:var(--fa-faint);margin:2px 0 8px'>"
            "分析师评级与目标价变动（美股）</div>",
            unsafe_allow_html=True,
        )
        _ct = {"UPGRADE": "上调", "DOWNGRADE": "下调", "MAINTAIN": "维持", "INIT": "首次覆盖"}
        for r in ratings:
            _c = UP_COLOR if r["change_type"] == "UPGRADE" else (
                DOWN_COLOR if r["change_type"] == "DOWNGRADE" else "var(--fa-muted)")
            _tp = ""
            if r["target_price"]:
                _tp = f"目标价 {r['target_price']:,.0f}"
                if r["target_pct"] is not None and abs(r["target_pct"]) >= 0.5:
                    _tp += f"（较前目标价 {r['target_pct']:+.0f}%）"
            st.markdown(
                f"<div style='display:flex;align-items:baseline;gap:10px;padding:8px 2px;"
                f"border-bottom:1px solid var(--fa-border)'>"
                f"<span style='flex:2;color:var(--fa-text);font-size:0.86rem'>"
                f"{_esc(r['name'])}<span style='color:var(--fa-faint);font-size:0.78rem'> "
                f"{_esc(r['symbol'])}</span></span>"
                f"<span style='flex:1.4;color:var(--fa-faint);font-size:0.78rem'>"
                f"{_esc(r['institution'])}</span>"
                f"<span style='color:{_c};font-size:0.82rem;min-width:44px'>"
                f"{_ct.get(r['change_type'], r['change_type'])}</span>"
                f"<span style='color:var(--fa-text-2);font-size:0.8rem;min-width:132px;text-align:right'>"
                f"{_tp}</span></div>",
                unsafe_allow_html=True,
            )


def _render_macro_briefs():
    """首页宏观议题专区——美联储/通胀/就业/中国政策。

    只读 macro_briefs 表里每个议题最新的一条，不做任何AI调用。内容是
    macro_brief.py 由cron离线生成的（见那个文件开头的说明：这类分析一次要
    把十几条资讯汇总起来让AI通读，几十秒一次，而一天之内对所有访客是同一份，
    放在页面现算等于每个人每次打开首页都重跑一遍）。

    版式跟站内其它列表统一：一行一个议题，标题旁边直接把"一句话结论"摆出来
    （这是整段解读里信息密度最高的一句，不点开也该看得到），点开才是现状/
    影响/盯什么三段和配图。美联储那条额外画一张CME FedWatch的利率概率图——
    图文结合在这个议题上是真的有用：几次会议、几个利率区间、各自多大概率，
    用文字讲一遍很啰嗦，一张横向条形图一眼就看完。
    """
    try:
        briefs = get_latest_macro_briefs()
    except Exception:
        briefs = []
    if not briefs:
        return

    st.markdown("**宏观议题**")
    _newest = max((b.get("created_at") or "") for b in briefs)
    _t = _to_cn_dt(_newest)
    if _t:
        st.markdown(
            f"<div style='font-size:0.74rem;color:var(--fa-faint);margin:-4px 0 12px'>"
            f"更新于 {_t.strftime('%m-%d %H:%M')}</div>",
            unsafe_allow_html=True,
        )

    for b in briefs:
        parts: dict[str, str] = {}
        text = b.get("brief_text") or ""
        positions = []
        for nm in _MACRO_SECTIONS:
            idx = text.find(f"{nm}：")
            if idx == -1:
                idx = text.find(f"{nm}:")
            if idx != -1:
                positions.append((idx, nm))
        positions.sort()
        for i, (idx, nm) in enumerate(positions):
            start = idx + len(nm) + 1
            end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
            parts[nm] = text[start:end].strip()

        headline = parts.get("一句话结论") or text[:40]
        with st.container(key=f"macro_{b.get('topic','')}"):
            st.markdown(
                f"<div style='padding:2px 0 6px'>"
                f"<span style='font-weight:600;color:var(--fa-text);font-size:0.95rem'>{_esc(b.get('title',''))}</span>"
                f"<span style='color:var(--fa-text-2);font-size:0.88rem'>　{_esc(headline)}</span></div>",
                unsafe_allow_html=True,
            )
            with st.expander("展开解读"):
                # 图在前、文字在后：这几个议题的解读本来就是围着数据写的，
                # 先看图再读结论，比反过来自然。chart_json 兼容两种结构——
                # 2026-09-04最早那版只存了FedWatch的list，后来改成
                # {"fed_watch":[...], "series":[...]} 的dict，老记录不重跑，
                # 所以两种都要认。
                try:
                    _cj = json.loads(b.get("chart_json") or "null")
                except Exception:
                    _cj = None
                _fed_rows, _series, _rate_path = [], [], {}
                if isinstance(_cj, list):
                    _fed_rows = _cj
                elif isinstance(_cj, dict):
                    _fed_rows = _cj.get("fed_watch") or []
                    _series = _cj.get("series") or []
                    _rate_path = _cj.get("rate_path") or {}

                # 政策利率路径放在FedWatch概率之前：先看"已经走到哪了"，再看
                # "市场认为下一步走去哪"，这个顺序读起来才顺。
                if _rate_path.get("points"):
                    st.markdown(
                        "<div style='font-size:0.76rem;color:var(--fa-faint);margin-bottom:4px'>"
                        "美联储历次利率决议　"
                        "<span style='color:var(--fa-faint)'>阶梯线，每个拐点是一次会议的调整</span></div>",
                        unsafe_allow_html=True,
                    )
                    st.plotly_chart(
                        build_fed_rate_path_chart(_rate_path),
                        use_container_width=True, config=_PLOTLY_CONFIG,
                        key=f"_fed_path_{b.get('id')}",
                    )

                if _fed_rows:
                    st.markdown(
                        "<div style='font-size:0.76rem;color:var(--fa-faint);margin-bottom:4px'>"
                        "市场隐含的联邦基金目标利率概率（CME FedWatch）</div>",
                        unsafe_allow_html=True,
                    )
                    st.plotly_chart(
                        build_fed_watch_chart(_fed_rows),
                        use_container_width=True, config=_PLOTLY_CONFIG,
                        key=f"_fed_watch_{b.get('id')}",
                    )
                for _i, _sr in enumerate(_series):
                    if not _sr.get("points"):
                        continue
                    # 说明文字要跟着图的形态走。带市场预期的指标画成柱+横线，
                    # 说明"柱子高过横线就是超预期"；没有预期数据的水平值序列
                    # 画的是折线，那句关于柱子和横线的说明就完全对不上了——
                    # 用户反馈"图上咋没标注我看不懂"，一部分正是这句驴唇不对
                    # 马嘴的说明造成的。顺带把原来那句删繁就简：颜色语义已经
                    # 去掉了，不需要再解释"跟行情涨跌无关"。
                    # 说明要跟实际画出来的图形一致。图形选柱还是折线由
                    # build_macro_series_chart 按序列形态决定（水平值走折线、
                    # 增量值走柱），这里用同一套判据推一遍，避免说明和图对不上
                    # ——之前就出现过折线图配着"柱=实际值"说明的情况。
                    _pts = _sr["points"]
                    _has_pred = any(pt.get("predict") is not None for pt in _pts)
                    _vv = [p["value"] for p in _pts if p.get("value") is not None]
                    _is_flat = bool(_vv) and min(_vv) >= 0 and max(_vv) > 0 and (max(_vv) - min(_vv)) / max(_vv) < 0.25
                    if _has_pred and not _is_flat:
                        _legend = "深色柱=实际值，浅色柱=当时的市场预期"
                    elif _has_pred:
                        _legend = "实线=实际值，虚线=当时的市场预期"
                    else:
                        _legend = "近期走势"
                    st.markdown(
                        f"<div style='font-size:0.76rem;color:var(--fa-faint);margin:10px 0 2px'>"
                        f"{_esc(_sr.get('name',''))}　"
                        f"<span style='color:var(--fa-faint)'>{_legend}</span></div>",
                        unsafe_allow_html=True,
                    )
                    st.plotly_chart(
                        build_macro_series_chart(_sr),
                        use_container_width=True, config=_PLOTLY_CONFIG,
                        key=f"_macro_series_{b.get('id')}_{_i}",
                    )
                for nm in ("现状", "影响", "盯什么"):
                    if parts.get(nm):
                        st.markdown(_labeled_line(nm, parts[nm]), unsafe_allow_html=True)
                try:
                    srcs = json.loads(b.get("sources_json") or "[]")
                except Exception:
                    srcs = []
                if srcs:
                    # 把AI当时读的原始材料一并列出来。不留原材料的分析没法复核，
                    # 用户也无从判断这段解读是基于什么写的。
                    st.markdown(
                        "<div style='font-size:0.74rem;color:var(--fa-faint);margin-top:12px'>"
                        f"解读依据的 {len(srcs)} 条资讯</div>", unsafe_allow_html=True,
                    )
                    for it in srcs[:10]:
                        st.markdown(
                            f"<div style='font-size:0.76rem;padding:4px 0;border-bottom:1px solid var(--fa-border)'>"
                            f"<a href='{_safe_href(it.get('url',''))}' target='_blank' "
                            f"style='color:var(--fa-text-2);text-decoration:none'>{_esc(it.get('title',''))}</a>"
                            f"<span style='color:var(--fa-faint)'>　{_esc(it.get('date',''))}</span></div>",
                            unsafe_allow_html=True,
                        )


def _render_home_page():
    """首页——世界地图（几个常见指数的实时点位）+ 今日重磅资讯。

    2026-08-30改用get_hot_market_news（富途新闻搜索，用当天真实异动股票
    当关键词种子）替代财新大盘资讯——用户反馈财新那条线"更新的好慢"，
    查过不是查询慢（接口本身1秒内返回），是财新这个源发布节奏本身偏慢
    （周刊/深度报道风格），不是分钟级快讯。富途这条线搜出来为空（比如
    今天没有股票明显异动、或者富途连不上）才退回财新兜底，不会完全没有
    内容可看。
    """
    # 2026-09-12重排。中间经过两版：
    #
    # 前端审计第8条指出"第一屏整屏都是世界地图、可执行清单落在80%的位置"，
    # 于是把地图降到清单之后。用户看过之后要求把地图挪回最上面，并且跟宏观
    # 那条横栏放在一起——"世界地图还是老样子移动到项目最上面吧，这个放在
    # 美元黄金那栏下面"。
    #
    # 这一版是两者的合并而不是回退：审计真正反对的是"第一屏只有一张信息密度
    # 很低的图"，不是地图本身的位置。现在第一屏是 指数横条 + 宏观横栏 + 地图
    # 三层叠在一起——前两层是高密度的数字，地图在它们下面作为同一组"今天全球
    # 什么情况"的收尾，可执行清单紧跟其后，不再被压到页面80%的位置。
    #
    # 指数横条（上证/恒生/标普/纳指/日经/DAX）在这一版删掉了：地图上本来就
    # 标着这六个指数的点位和涨跌，横条是同一份缓存(home_map_cache.json)的
    # 另一种画法。它当初存在的理由是"地图在页面很下面，顶上需要一行数字"，
    # 地图挪回最上面之后这个理由就不成立了，留着只是把同一组数字讲两遍。
    # 宏观横栏留下：VIX/美债/美元/金油铜地图上没有，不重复。
    #
    # 顺序：宏观横栏(VIX/美债/美元/金油铜) → 世界地图 →
    #       今日可执行清单+排行榜 → 宏观议题/日历/IPO → 资讯。
    _render_macro_strip()

    st.markdown("**全球指数一览**")
    _render_home_map()

    st.divider()
    _render_advice_section()

    st.divider()
    _render_macro_briefs()
    _render_event_calendar()
    # 三个市场的新股收进一组标签，而不是竖着排三段——竖排的话首页要多滚三屏，
    # 而用户一次只关心一个市场（用户2026-09-13："上面设置三个标签港股，沪深，
    # 美股三个小标签任意切换"）。
    #
    # 港股和美股的内容是同构的（首日表现统计/开盘vs收盘散点/按月拆解，共用
    # _render_ipo_perf_block）；沪深那块形态不同是数据决定的——这个账号没有沪深
    # 行情权限，取不到历史新股的首日K线，所以算不出首日表现统计，改成用富途
    # 直接给的发行PE/行业PE，那也是沪深打新判断贵贱的通行口径。
    st.markdown("**新股**")
    _ipo_tab_hk, _ipo_tab_a, _ipo_tab_us = st.tabs(["港股", "沪深", "美股"])
    with _ipo_tab_hk:
        _render_ipo_briefs()
    with _ipo_tab_a:
        _render_a_ipo_briefs()
    with _ipo_tab_us:
        _render_us_ipo_briefs()

    st.divider()
    st.markdown("**今日重磅消息**")
    try:
        news = get_hot_market_news()
    except Exception:
        news = None
    if news is None or news.empty:
        try:
            news = get_market_news()
        except Exception:
            news = None
    if news is None or news.empty:
        st.caption("暂时获取不到资讯。")
        return

    news = news.copy()
    # get_hot_market_news已经带了"日期"列（来自富途，跟财新的URL日期格式
    # 不一样），只有财新兜底那条路径（没有这一列）才需要从url正则提取。
    if "日期" not in news.columns:
        news["日期"] = news["url"].str.extract(r"/(\d{4}-\d{2}-\d{2})/")
    news = news.sort_values("日期", ascending=False, na_position="last")

    show_n = 30 if st.session_state.get("_home_news_expand") else 10
    _hot_names = _get_hot_stock_names()
    # 资讯不再一条一个带边框的卡片。十条新闻十个方框，方框本身就是这一段最大
    # 的视觉噪声，而且跟页面上其它列表（自选/排行榜/决策记录）已经统一成的
    # 发丝线平铺行不是一套。这里直接用一个 div 画行，连 st.container 都省了。
    for _, row in news.head(show_n).iterrows():
        _title = _strip_news_boilerplate(row["summary"])
        _title_style = _news_title_style(_title, _hot_names)
        st.markdown(
            f"<div style='padding:11px 2px;border-bottom:1px solid var(--fa-border)'>"
            f"<a href='{_safe_href(row['url'])}' target='_blank' style='{_title_style};text-decoration:none'>{_esc(_title)}</a>"
            f"<div style='font-size:0.74rem;color:var(--fa-faint);margin-top:4px'>"
            # 2026-09-12：标出这条是按哪只异动股搜到的（升级路线图第9条
            # "资讯关联化"）。这批新闻本来就是拿当天真实异动的股票名当关键词
            # 搜的，related 就是那个关键词本身，是事实不是AI推断的关联；
            # 没有这个字段（老缓存/兜底数据源）就照旧只显示分类和日期。
            + (f"影响：{_esc(row.get('related'))} · " if row.get("related") else "")
            + f"{_esc(row.get('tag',''))} · {_esc(row.get('日期') or '-')}</div></div>",
            unsafe_allow_html=True,
        )
    if len(news) > 10:
        if not st.session_state.get("_home_news_expand"):
            if st.button("更多资讯", key="_home_news_more"):
                st.session_state["_home_news_expand"] = True
                st.rerun()
        else:
            if st.button("收起", key="_home_news_collapse"):
                st.session_state["_home_news_expand"] = False
                st.rerun()


@st.fragment
def _render_institution_view(symbol: str, market: str):
    """机构观点：各大机构的评级与目标价，以及晨星研报。

    2026-09-05用户提出"很多分析里没有专家机构的背书"。此前页面上只有我们
    自己AI的判断，唯一的外部意见是折在估值里的一句"分析师一致预期均值"。

    这块补的是两种此前没有的东西：一是逐家机构的评级和目标价——是谁说的、
    什么时候说的、带原文链接，可以点进去核对，而不是一个抹平分歧的平均数；
    二是晨星研报，那是真正带论证过程的第三方估值分析，不只是一个评级数字。

    分歧刻意突出显示：实测苹果8家机构里7家买入目标价340-400，富瑞独家给
    强烈卖出263.66——这种分歧是均值给不了的信息，也正是用户该自己判断的地方。
    """
    try:
        insts = get_institution_ratings(symbol, market, limit=8)
    except Exception:
        insts = []
    try:
        ms = get_morningstar_view(symbol, market)
    except Exception:
        ms = {}
    if not insts and not (ms and (ms.get("fair_value") or ms.get("star_rating"))):
        return

    st.divider()
    st.subheader("机构观点")

    if ms and (ms.get("fair_value") or ms.get("star_rating")):
        _cur = {"HK": "HK$", "US": "$", "A": "¥"}.get(market, "")
        c1, c2 = st.columns([1, 2])
        _star = ms.get("star_rating")
        c1.markdown(
            f"<div style='font-size:0.76rem;color:var(--fa-faint)'>晨星星级</div>"
            f"<div style='font-size:1.35rem;font-weight:600;color:var(--fa-text)'>"
            f"{_esc(str(_star) + ' 星') if _star else '-'}</div>"
            + (f"<div style='font-size:0.76rem;color:var(--fa-faint);margin-top:2px'>"
               f"公允价值 {_cur}{ms['fair_value']:,.2f}</div>" if ms.get("fair_value") else ""),
            unsafe_allow_html=True,
        )
        c2.caption("晨星星级衡量的是相对它算出的内在价值、现在贵不贵："
                   "5星明显低估、3星接近公允、1星明显高估。它不含对短期股价的判断，"
                   "跟券商的买入卖出评级不是一回事。")
        if ms.get("content"):
            with st.expander("晨星的估值论证"):
                st.markdown(ms["content"][:2400])

    if insts:
        _tps = [i["target_price"] for i in insts if i.get("target_price")]
        _spread = ""
        if len(_tps) >= 3:
            _spread = (f"　最高 {max(_tps):,.0f} · 最低 {min(_tps):,.0f}"
                       f"（相差 {(max(_tps) - min(_tps)) / min(_tps) * 100:.0f}%）")
        st.markdown(
            f"<div style='font-size:0.76rem;color:var(--fa-faint);margin:14px 0 6px'>"
            f"各机构最新评级{_esc(_spread)}</div>",
            unsafe_allow_html=True,
        )
        for it in insts:
            _c = UP_COLOR if "买入" in it["rating"] else (
                DOWN_COLOR if "卖出" in it["rating"] else "var(--fa-muted)")
            _name = _esc(it["institution"])
            if it.get("url"):
                _name = (f"<a href='{_safe_href(it['url'])}' target='_blank' "
                         f"style='color:inherit;text-decoration:none'>{_name}</a>")
            st.markdown(
                f"<div style='display:flex;align-items:baseline;gap:10px;padding:8px 2px;"
                f"border-bottom:1px solid var(--fa-border)'>"
                f"<span style='flex:2;color:var(--fa-text);font-size:0.88rem'>{_name}</span>"
                f"<span style='color:{_c};font-size:0.85rem;min-width:56px'>{_esc(it['rating'])}</span>"
                f"<span style='flex:1;text-align:right;color:var(--fa-text-2);font-size:0.85rem'>"
                f"{('目标价 ' + format(it['target_price'], ',.2f')) if it.get('target_price') else ''}</span>"
                f"<span style='color:var(--fa-faint);font-size:0.76rem;min-width:76px;text-align:right'>"
                f"{_esc(it.get('date', ''))}</span></div>",
                unsafe_allow_html=True,
            )


@st.fragment
def _render_chips_section(symbol: str, market: str):
    """个股的"资金与筹码"——谁在买、谁在卖。

    2026-09-05新增。详情页原本有价格、K线、财务、技术面、新闻、AI分析，
    覆盖了"值多少钱"和"走成什么样"，但一直缺一整个维度：持有这只股票的人
    在做什么。价格告诉你成交在什么位置，成交量告诉你有多热，两者都答不了
    "是谁在买"——大单持续净流入而股价没动，跟小单堆量把价格推上去，是完全
    不同的两件事。

    四块数据按"从快到慢"排：当日资金流向是今天的事，空头和机构持仓是按期
    披露的（滞后但反映中长期立场），内部人交易是不定期的事件。放在一个区块
    里互相对照才有意义——比如主力在流出、同时机构持股比例连续下降，这两条
    互相印证；如果主力流出但内部人在买，那就是分歧，值得多看一眼。

    做成 fragment：这几个接口的缓存周期从30分钟到12小时不等，跟详情页其余
    部分（3秒刷新的价格、缓存住的K线）完全不是一个节奏。
    """
    try:
        cap = get_capital_distribution(symbol, market)
    except Exception:
        cap = {}
    try:
        shorts = get_short_interest(symbol, market)
    except Exception:
        shorts = []
    try:
        inst = get_institutional_holding(symbol, market)
    except Exception:
        inst = []
    try:
        insiders = get_insider_trades(symbol, market)
    except Exception:
        insiders = []

    if not (cap or shorts or inst or insiders):
        return

    st.divider()
    st.subheader("资金与筹码")

    _cur = {"HK": "HK$", "US": "$", "A": "¥"}.get(market, "")

    def _amt(v):
        """金额按数量级换单位。资金流动辄以亿计，原样打印一串数字没人读得出来。

        负号要写在货币符号外面：2026-09-11前端审计抓到主力净流出显示成
        「$-744万」——负号被夹在了 $ 和数字之间。会计和行情软件的通行写法是
        -$744万（货币符号紧贴数字，正负号在最外层），跟_fmt_usd_signed同一个
        约定。这里原来把 v 整个丢进 f-string，负数的符号自然跟在 _cur 后面。
        """
        if v is None:
            return "-"
        a = abs(v)
        sign = "-" if v < 0 else ""
        if a >= 1e8:
            return f"{sign}{_cur}{a / 1e8:,.2f}亿"
        if a >= 1e4:
            return f"{sign}{_cur}{a / 1e4:,.0f}万"
        return f"{sign}{_cur}{a:,.0f}"

    if cap:
        _mn = cap.get("main_net")
        _c = UP_COLOR if (_mn or 0) > 0 else (DOWN_COLOR if (_mn or 0) < 0 else "var(--fa-muted)")
        _pct = cap.get("main_net_pct")
        st.markdown(
            f"<div style='font-size:0.76rem;color:var(--fa-faint);margin:2px 0 8px'>"
            f"当日资金流向　超大单+大单算主力，中单+小单算散户</div>",
            unsafe_allow_html=True,
        )
        c1, c2, c3, c4 = st.columns(4)
        c1.markdown(
            f"<div style='font-size:0.76rem;color:var(--fa-faint)'>主力净流入</div>"
            f"<div style='font-size:1.25rem;font-weight:600;color:{_c}'>{_amt(_mn)}</div>"
            + (f"<div style='font-size:0.74rem;color:var(--fa-faint)'>占当日成交 {_pct:+.1f}%</div>"
               if _pct is not None else ""),
            unsafe_allow_html=True,
        )
        for col, label, key in (
            (c2, "超大单", "super_net"), (c3, "大单", "big_net"), (c4, "散户（中+小单）", "retail_net"),
        ):
            v = cap.get(key)
            vc = UP_COLOR if (v or 0) > 0 else (DOWN_COLOR if (v or 0) < 0 else "var(--fa-muted)")
            col.markdown(
                f"<div style='font-size:0.76rem;color:var(--fa-faint)'>{label}</div>"
                f"<div style='font-size:1.05rem;font-weight:600;color:{vc}'>{_amt(v)}</div>",
                unsafe_allow_html=True,
            )

    _cols = []
    if shorts:
        _cols.append("short")
    if inst:
        _cols.append("inst")
    if _cols:
        st.markdown("<div style='height:14px'></div>", unsafe_allow_html=True)
        _cc = st.columns(len(_cols))
        for _i, _kind in enumerate(_cols):
            with _cc[_i]:
                if _kind == "short":
                    _s0 = shorts[0]
                    st.markdown(
                        f"<div style='font-size:0.76rem;color:var(--fa-faint);margin-bottom:6px'>"
                        f"空头持仓（{_esc(_s0['date'])}）</div>"
                        f"<div style='display:flex;justify-content:space-between;padding:6px 0;"
                        f"border-bottom:1px solid var(--fa-border)'>"
                        f"<span style='font-size:0.84rem;color:var(--fa-text-2)'>占流通股</span>"
                        f"<span style='font-size:0.84rem'>{_s0['short_percent']:.2f}%</span></div>"
                        f"<div style='display:flex;justify-content:space-between;padding:6px 0;"
                        f"border-bottom:1px solid var(--fa-border)'>"
                        f"<span style='font-size:0.84rem;color:var(--fa-text-2)'>回补天数</span>"
                        f"<span style='font-size:0.84rem'>{_s0['days_to_cover']:.2f} 天</span></div>",
                        unsafe_allow_html=True,
                    )
                    st.caption("回补天数=空头持仓/日均成交量，越大说明一旦上涨越容易踩踏")
                else:
                    _i0 = inst[0]
                    _chg = _i0["holder_pct_change"]
                    _ic = UP_COLOR if _chg > 0 else (DOWN_COLOR if _chg < 0 else "var(--fa-muted)")
                    st.markdown(
                        f"<div style='font-size:0.76rem;color:var(--fa-faint);margin-bottom:6px'>"
                        f"机构持股（{_esc(_i0['period'])}）</div>"
                        f"<div style='display:flex;justify-content:space-between;padding:6px 0;"
                        f"border-bottom:1px solid var(--fa-border)'>"
                        f"<span style='font-size:0.84rem;color:var(--fa-text-2)'>持股比例</span>"
                        f"<span style='font-size:0.84rem'>{_i0['holder_pct']:.2f}%"
                        f"<span style='color:{_ic}'> {_chg:+.2f}</span></span></div>"
                        f"<div style='display:flex;justify-content:space-between;padding:6px 0;"
                        f"border-bottom:1px solid var(--fa-border)'>"
                        f"<span style='font-size:0.84rem;color:var(--fa-text-2)'>持有机构数</span>"
                        f"<span style='font-size:0.84rem'>{_i0['institution_quantity']:,}"
                        f"<span style='color:var(--fa-faint)'> "
                        f"{_i0['institution_quantity_change']:+d}</span></span></div>",
                        unsafe_allow_html=True,
                    )
                    st.caption("看的是环比变化——机构在加仓还是在撤，比现在持有多少更说明问题")

    if insiders:
        st.markdown("<div style='height:14px'></div>", unsafe_allow_html=True)
        with st.expander(f"内部人交易（近 {len(insiders)} 笔）"):
            st.caption(
                "高管用自己的钱买入是少有的、说话的人要为结果付钱的信号；"
                "卖出的解读要谨慎，可能只是行权套现或个人资金安排，跟看空不是一回事。"
            )
            for it in insiders:
                st.markdown(
                    f"<div style='display:flex;align-items:baseline;gap:10px;padding:7px 2px;"
                    f"border-bottom:1px solid var(--fa-border)'>"
                    f"<span style='color:var(--fa-faint);font-size:0.76rem;min-width:64px'>"
                    f"{_esc(it['date'])}</span>"
                    f"<span style='flex:1.4;color:var(--fa-text);font-size:0.84rem'>{_esc(it['name'])}</span>"
                    f"<span style='flex:2;color:var(--fa-faint);font-size:0.76rem'>{_esc(it['title'])}</span>"
                    f"<span style='color:var(--fa-text-2);font-size:0.8rem'>{_esc(it['transaction_type'])}</span>"
                    f"<span style='color:var(--fa-text);font-size:0.82rem;min-width:88px;text-align:right'>"
                    f"{it['shares']:,.0f} 股</span></div>",
                    unsafe_allow_html=True,
                )


def _alert_line(a: dict, cur_price: float | None = None) -> str:
    """一条到价提醒的文字描述，列表和详情页共用。"""
    word = "涨到" if a["direction"] == "above" else "跌到"
    txt = f"{word} {a['target']:g}"
    if a.get("triggered_at"):
        _t = _to_cn_dt(a["triggered_at"])
        txt += f" · 已于 {_t.strftime('%m-%d %H:%M') if _t else a['triggered_at'][:10]} 触发"
        if a.get("triggered_price"):
            txt += f"（{a['triggered_price']:g}）"
    elif cur_price:
        gap = (a["target"] - cur_price) / cur_price * 100
        txt += f" · 距现价 {gap:+.1f}%"
    return txt


def _render_price_alert_control(symbol: str, market: str, name: str, spot: dict | None):
    """详情页的"设到价提醒"（升级路线图第2条）。

    价格到了由 intraday_watch.py 在盘中盯盘时推微信——那个脚本本来就每隔几
    分钟跑一次、已经接好了微信通道和重试队列，这里只往它的盯盘清单里加一条
    用户自己画的线，不另起一套推送。

    一次性语义：触发一次就停用，不是每天重推（理由见 tracker 建表处）。
    """
    if not st.session_state.get("logged_in"):
        return
    email = st.session_state["user_email"]
    cur = (spot or {}).get("最新价")

    try:
        mine = [a for a in get_price_alerts(email) if str(a["symbol"]) == str(symbol)]
    except Exception:
        return
    active = [a for a in mine if a["enabled"]]

    label = f"到价提醒（{len(active)}）" if active else "设到价提醒"
    with st.popover(label, use_container_width=False):
        st.markdown("**价格到了微信通知我**")
        # 默认值给现价，用户通常是在现价附近上下改几个点，从0开始输很烦。
        default_val = float(cur) if cur else 0.0
        c1, c2 = st.columns([1, 1.2])
        with c1:
            direction_label = st.radio(
                "方向", ["涨到", "跌到"], horizontal=True,
                key=f"_alert_dir_{symbol}", label_visibility="collapsed",
            )
        with c2:
            target = st.number_input(
                "目标价", min_value=0.0, value=default_val, step=0.01, format="%.2f",
                key=f"_alert_val_{symbol}", label_visibility="collapsed",
            )
        note = st.text_input("备注（可选）", key=f"_alert_note_{symbol}",
                             placeholder="到了想做什么，比如：减半仓")
        if st.button("保存提醒", key=f"_alert_save_{symbol}", use_container_width=True):
            if not target or target <= 0:
                st.warning("填一个大于0的价格。")
            else:
                ok = add_price_alert(
                    email, symbol, name, market,
                    "above" if direction_label == "涨到" else "below",
                    float(target), note or "",
                )
                if ok:
                    st.success(f"已设置：{direction_label} {target:g} 通知你。")
                    st.rerun()
                else:
                    st.warning("保存失败，检查一下价格。")

        if mine:
            st.divider()
            for a in mine:
                cols = st.columns([4, 1])
                with cols[0]:
                    tone = "var(--fa-text-2)" if a["enabled"] else "var(--fa-faint)"
                    st.markdown(
                        f"<div style='font-size:0.8rem;color:{tone};padding:4px 0'>"
                        f"{_esc(_alert_line(a, cur))}"
                        + (f"<br><span style='font-size:0.72rem;color:var(--fa-faint)'>"
                           f"{_esc(a['note'])}</span>" if a.get("note") else "")
                        + "</div>",
                        unsafe_allow_html=True,
                    )
                with cols[1]:
                    if st.button("删除", key=f"_alert_del_{a['id']}", type="tertiary"):
                        delete_price_alert(email, a["id"])
                        st.rerun()


def _render_stock_detail(symbol: str, market: str, name: str):
    # 之前这里还挂着 _inject_auto_refresh(30,...) 强制整页每30秒rerun一次——
    # 是_render_price_header改成@st.fragment(run_every=3)独立刷新之前的老
    # 机制，早就没被清理掉。K线数据(hist)本来就缓存在session_state[core_key]
    # 里、AI分析生成后也缓存，全页面rerun并不会让它们变得更"新"，只是白白把
    # 图表/AI文字这些开销大的部分每30秒重新渲染一次——这正是"网页卡卡的"的
    # 真实来源。价格的"活着的感觉"已经由下面的fragment用更轻量的方式做到了，
    # 删掉这个多余的整页定时rerun。
    if st.button("", icon=":material/arrow_back:", key=f"detail_back_{symbol}_{market}", type="tertiary", help="返回"):
        for k in ("_detail_symbol", "_detail_market", "_detail_name", "_detail_module"):
            st.session_state.pop(k, None)
        for key in ("symbol", "market", "name", "section"):
            try:
                del st.query_params[key]
            except KeyError:
                pass
        # 回到进来时那个分区。以前这里写死"持仓"，从自选点进来的用户按返回会
        # 落在一个自己没在看的分区上。
        st.session_state["_active_section"] = st.session_state.get("_detail_return_section", "持仓")
        st.rerun()

    # 详情页页眉。跟首页一样去掉了通栏红底——标题本身用字号和字重就能站住，
    # 满屏的品牌红反而会把下面真正要看的涨跌红压掉。底部一条细线做分隔。
    st.markdown(
        f"""
        <div style='padding:2px 0 14px;border-bottom:1px solid var(--fa-border);margin-bottom:20px'>
            <div style='font-size:1.34rem;font-weight:650;letter-spacing:-.022em;
                        color:var(--fa-text);line-height:1.3'>{_esc(name)}</div>
            <div style='font-size:.78rem;color:var(--fa-faint);margin-top:4px;
                        letter-spacing:.03em'>{_esc(symbol)} · {_esc(market)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # 快路径：只拉行情+实时价，不碰AI，先把图画出来
    core_key = f"_detail_core_{symbol}_{market}"
    if core_key not in st.session_state:
        with st.spinner("加载行情..."):
            try:
                end = cn_now().strftime("%Y%m%d")
                start = (cn_now() - timedelta(days=90)).strftime("%Y%m%d")
                hist = get_stock_history(symbol, start, end, market=market)
                if hist is None or hist.empty:
                    st.error("没有获取到行情数据，检查一下股票代码是否正确。")
                    return
                try:
                    spot = get_stock_realtime(symbol, market=market)
                except Exception:
                    spot = {}
                st.session_state[core_key] = {"hist": hist, "spot": spot}
            except Exception as e:
                st.error(f"加载失败：{e}")
                return

    core = st.session_state[core_key]
    hist, spot = core["hist"], core["spot"]

    _render_price_header(symbol, market)

    # 到价提醒放在价格区块之外，不能放进去。_render_price_header 是
    # @st.fragment(run_every=3)，每3秒自动重跑一次——弹窗里的输入框会在用户
    # 还没输完的时候被重置，而且这个文件里已经踩过一次同类的坑（见
    # _render_position_rows 里 pos_del_ 按钮那段：fragment 的定时刷新会让
    # 弹窗绑定的 fragment 失效，点确认没反应）。这里是稳定作用域。
    _render_price_alert_control(symbol, market, name, spot)

    st.divider()
    period_labels = ["分时K（今日）", "日K", "周K", "月K"]
    period_label = st.radio("K线周期", period_labels, index=0, horizontal=True, key="_detail_kline_period")

    if market == "A" and period_label == "分时K（今日）":
        intraday = get_stock_intraday_a(symbol)
        if intraday.empty:
            st.caption("今天的分时数据暂时取不到，展示日K替代。")
            if hist is not None and not hist.empty:
                st.plotly_chart(build_candlestick(hist), use_container_width=True, config=_PLOTLY_CONFIG)
        else:
            st.plotly_chart(
                build_intraday_line(intraday, spot.get("昨收") if spot else None, market),
                use_container_width=True, config=_PLOTLY_CONFIG,
            )
    elif market == "A":
        period_options = {"日K": ("d", 90), "周K": ("w", 730), "月K": ("m", 1825)}
        freq, days_back = period_options[period_label]
        c_end = cn_now().strftime("%Y%m%d")
        c_start = (cn_now() - timedelta(days=days_back)).strftime("%Y%m%d")
        try:
            chart_hist = get_stock_history(symbol, c_start, c_end, frequency=freq, market=market)
        except Exception:
            chart_hist = hist
        if chart_hist is not None and not chart_hist.empty:
            st.plotly_chart(build_candlestick(chart_hist), use_container_width=True, config=_PLOTLY_CONFIG)
    elif period_label == "分时K（今日）":
        intraday = get_stock_intraday_futu(symbol, market)
        if intraday.empty:
            st.caption("分时数据需要本地 Futu OpenD 连接、且当前有实时推送，暂时展示日K替代。")
            if hist is not None and not hist.empty:
                st.plotly_chart(build_candlestick(hist), use_container_width=True, config=_PLOTLY_CONFIG)
        else:
            if intraday.attrs.get("is_previous_session"):
                st.caption(f"当前没有今日分时，展示最近交易日（{intraday.attrs.get('session_date', '')}）走势。")
            st.plotly_chart(
                build_intraday_line(intraday, spot.get("昨收") if spot else None, market),
                use_container_width=True, config=_PLOTLY_CONFIG,
            )
    else:
        chart_hist = get_stock_kline_futu(symbol, market, period_label)
        if chart_hist.empty:
            chart_hist = hist
            st.caption("该周期需要本地 Futu OpenD 连接，当前展示日K替代。")
        if chart_hist is not None and not chart_hist.empty:
            st.plotly_chart(build_candlestick(chart_hist), use_container_width=True, config=_PLOTLY_CONFIG)

    # 手机上进详情页第一屏默认只有图表这一块——之前新闻/AI分析这些小组件
    # 全部一起加载，手机上往下滑浏览的时候很容易手滑碰到中间那些按钮
    # （K线周期切换、重新分析这些）造成误触。改成默认收起，只留一个
    # "浏览更多"按钮，点了才展开新闻和AI分析部分，第一屏能滑动的可交互
    # 元素少很多，不容易碰错。
    expand_key = f"_detail_expand_{symbol}_{market}"
    st.divider()
    if not st.session_state.get(expand_key):
        if st.button("浏览更多（新闻 · AI 分析）", key=f"_detail_expand_btn_{symbol}_{market}", use_container_width=True):
            st.session_state[expand_key] = True
            st.rerun()
        return
    if st.button("收起", key=f"_detail_collapse_btn_{symbol}_{market}"):
        st.session_state[expand_key] = False
        st.rerun()

    _stock_name_for_news = get_stock_name(symbol) if market == "A" else spot.get("名称", symbol)
    _render_news_section(_stock_name_for_news, symbol=symbol, market=market)

    # 资金筹码放在新闻之后、AI分析之前：它跟新闻一样是"事实材料"，而AI分析是
    # 基于所有材料的结论，材料该排在结论前面。
    _render_chips_section(symbol, market)
    # 机构观点排在筹码之后、AI分析之前：同样属于"事实材料"，
    # 而且是外部第三方的意见，放在我们自己AI的结论前面更合适。
    _render_institution_view(symbol, market)

    st.divider()
    _head_col, _refresh_col = st.columns([5, 1])
    _head_col.subheader("AI 深度分析")
    module_defs = (
        ("news", "资讯解读"), ("financial", "财务摘要"), ("benchmark", "对比大盘"), ("cross", "综合数据分析（交叉验证）"),
    )
    summary_key = f"_detail_summary_{symbol}_{market}"
    if _refresh_col.button("重新分析", key=f"_reanalyze_{symbol}_{market}", use_container_width=True):
        for mod_key, _ in module_defs:
            st.session_state.pop(f"_detail_mod_{symbol}_{market}_{mod_key}", None)
        st.session_state.pop(summary_key, None)
        st.rerun()

    for mod_key, mod_label in module_defs:
        with st.container(border=True):
            st.markdown(f"**{mod_label}**")
            _render_module(mod_key, symbol, market, hist, spot)
    with st.container(border=True):
        st.markdown("**总结性分析**")
        if summary_key not in st.session_state:
            try:
                section_texts = {
                    mod_label: st.session_state.get(f"_detail_mod_{symbol}_{market}_{mod_key}", {}).get("ai_text", "")
                    for mod_key, mod_label in module_defs
                }
                with st.spinner("正在汇总生成总结性分析…"):
                    st.session_state[summary_key] = _stream_ai_text(
                        summarize_overall(symbol, section_texts), raise_on_error=False,
                    )
                # 2026-08-26新增：这个分数原来现算现扔，从来没被存过，没法
                # 回溯验证准不准——补记到cross模块那次log_analysis刚插入的
                # 那条记录上（游客模式不落库，跟cross模块的log_analysis同一个
                # 判断条件）。
                if st.session_state.get("logged_in"):
                    _overall_score = extract_score(st.session_state[summary_key])
                    if _overall_score is not None:
                        record_overall_score(st.session_state["user_email"], symbol, market, _overall_score)
            except Exception as e:
                st.session_state[summary_key] = f"汇总失败：{e}"
        _render_overall_summary(st.session_state[summary_key])


def _render_index_detail(name: str, code: str, market: str):
    # 同样的原因删掉了_inject_auto_refresh，见_render_stock_detail开头的注释。
    if st.button("", icon=":material/arrow_back:", key=f"idx_back_{code}_{market}", type="tertiary", help="返回行情"):
        for k in ("_index_detail_code", "_index_detail_market", "_index_detail_name"):
            st.session_state.pop(k, None)
        st.session_state["_active_section"] = "行情"
        st.rerun()

    # 详情页页眉。跟首页一样去掉了通栏红底——标题本身用字号和字重就能站住，
    # 满屏的品牌红反而会把下面真正要看的涨跌红压掉。底部一条细线做分隔。
    st.markdown(
        f"""
        <div style='padding:2px 0 14px;border-bottom:1px solid var(--fa-border);margin-bottom:20px'>
            <div style='font-size:1.34rem;font-weight:650;letter-spacing:-.022em;
                        color:var(--fa-text);line-height:1.3'>{_esc(name)}</div>
            <div style='font-size:.78rem;color:var(--fa-faint);margin-top:4px;
                        letter-spacing:.03em'>{_esc(code)} · {_esc(market)}指数</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    try:
        idx_snap = next((i for i in get_multi_index_snapshot(market) if i["名称"] == name), None)
    except Exception:
        idx_snap = None

    _render_index_price_header(name, market)

    st.divider()
    period_label = st.radio(
        "K线周期", ["分时K（今日）", "日K", "周K", "月K"], index=0, horizontal=True, key="_idx_kline_period",
    )

    base_price = idx_snap.get("最新") - idx_snap.get("涨跌") if idx_snap else None

    # 2026-09-11修（P0，前端审计"指数K线空白"）：实测不是取不到数据——沪深
    # 指数分时/日K走的是BaoStock/新浪这两个接口（个股K线走本地Futu，几乎
    # 秒回），从这台VPS访问境内接口本身就有真实网络延迟，缓存没命中时单次
    # 加载能到8-9秒。之前这段时间页面上什么都不画，用户等到8秒以上只看到
    # 一片空白、控制台也没有报错，直接判断成"坏了"——其实只是没有任何加载
    # 状态提示，慢等于坏是同一个体验问题。包一层st.spinner，缓存命中时
    # spinner几乎一闪而过感知不到，缓存没命中的几秒钟至少能看出"在加载"
    # 而不是"卡死了"。
    with st.spinner("加载K线数据..."):
        if period_label == "分时K（今日）":
            intraday = get_index_intraday_a(code) if market == "A" else get_index_intraday_futu(name, market, base_price)
            if intraday.empty:
                st.caption("今天的分时数据暂时取不到，展示日K替代。")
                try:
                    chart_hist = get_index_history(code, market, "日K")
                except Exception:
                    chart_hist = None
                if chart_hist is not None and not chart_hist.empty:
                    st.plotly_chart(build_candlestick(chart_hist), use_container_width=True, config=_PLOTLY_CONFIG)
            else:
                st.plotly_chart(
                    build_intraday_line(intraday, base_price, market),
                    use_container_width=True, config=_PLOTLY_CONFIG,
                )
        else:
            try:
                chart_hist = get_index_history(code, market, period_label)
            except Exception as e:
                chart_hist = None
                st.error(f"K线加载失败：{e}")
    if period_label != "分时K（今日）" and chart_hist is not None and not chart_hist.empty:
        st.plotly_chart(build_candlestick(chart_hist), use_container_width=True, config=_PLOTLY_CONFIG)

    idx_expand_key = f"_idx_expand_{code}_{market}"
    st.divider()
    if not st.session_state.get(idx_expand_key):
        if st.button("浏览更多（成分股 · 资讯 · AI 分析）", key=f"_idx_expand_btn_{code}_{market}", use_container_width=True):
            st.session_state[idx_expand_key] = True
            st.rerun()
        return
    if st.button("收起", key=f"_idx_collapse_btn_{code}_{market}"):
        st.session_state[idx_expand_key] = False
        st.rerun()

    st.subheader("成分股")
    _render_index_top_movers(market, index_name=name)

    st.divider()
    _render_news_section(name, is_index=True)

    st.divider()
    _idx_head_col, _idx_refresh_col = st.columns([5, 1])
    _idx_head_col.subheader("AI 深度分析")

    idx_ai_key = f"_idx_analysis_{code}_{market}"
    if _idx_refresh_col.button("重新分析", key=f"_idx_reanalyze_{code}_{market}", use_container_width=True):
        for _suffix in ("_news", "_cross", "_summary"):
            st.session_state.pop(f"{idx_ai_key}{_suffix}", None)
        st.rerun()
    _idx_news_fresh = f"{idx_ai_key}_news" not in st.session_state
    with st.container(border=True):
        st.markdown("**资讯解读**")
        if _idx_news_fresh:
            try:
                with st.spinner("正在获取资讯并生成AI解读…"):
                    news, _ = get_index_news(name, limit=8)
                    news_summary = _news_to_summary(news)
                    ai_text = _stream_ai_text(summarize_index_news(name, news_summary))
                st.session_state[f"{idx_ai_key}_news"] = {"ai_text": ai_text, "summary": news_summary}
            except Exception as e:
                st.session_state[f"{idx_ai_key}_news"] = {"ai_text": f"获取失败：{e}", "summary": "无相关新闻"}
        else:
            st.markdown(st.session_state[f"{idx_ai_key}_news"]["ai_text"])

    _idx_cross_fresh = f"{idx_ai_key}_cross" not in st.session_state
    with st.container(border=True):
        st.markdown("**综合数据分析**")
        daily_hist = get_index_history(code, market, "日K")
        has_hist = daily_hist is not None and not daily_hist.empty
        technical_summary = compute_technical_signal(daily_hist) if has_hist else "暂无技术面数据"
        stats = compute_stats(daily_hist) if has_hist and len(daily_hist) > 5 else {}

        try:
            _idx_snap_now = next((i for i in get_multi_index_snapshot(market) if i["名称"] == name), None)
            if _idx_snap_now:
                _idx_spot = {
                    "最新价": _idx_snap_now["最新"],
                    "昨收": _idx_snap_now["最新"] - _idx_snap_now["涨跌"],
                }
                _idx_intraday = (
                    get_index_intraday_a(code) if market == "A"
                    else get_index_intraday_futu(name, market, _idx_spot["昨收"])
                )
            else:
                _idx_spot, _idx_intraday = {}, None
        except Exception:
            _idx_spot, _idx_intraday = {}, None
        realtime_signal = compute_realtime_signal(_idx_spot, _idx_intraday)
        technical_summary += " 【盘中实时信号】" + realtime_signal

        if stats:
            scol1, scol2, scol3, scol4 = st.columns(4)
            scol1.metric("区间收益率", stats.get("区间收益率", "—"))
            scol2.metric("年化波动率", stats.get("年化波动率", "—"))
            scol3.metric("最大回撤", stats.get("最大回撤", "—"))
            scol4.metric("夏普比率(简化)", stats.get("夏普比率(简化)", "—"))
        st.markdown(f"**技术面信号**：{technical_summary}")
        if has_hist:
            st.plotly_chart(build_return_histogram(daily_hist), use_container_width=True, config=_PLOTLY_CONFIG)

        st.caption("AI 解读")
        if _idx_cross_fresh:
            news_summary = st.session_state.get(f"{idx_ai_key}_news", {}).get("summary", "无相关新闻")
            # 这里之前失败时会 `return`——是函数级别的return，会直接跳出整个
            # _render_index_detail，导致下面的"总结性分析"区块整个不渲染
            # （跟个股详情页 _render_module 不一样：那边每个模块是独立函数
            # 调用，一个模块内部return只影响它自己，不影响后续模块）。这里
            # 改成跟上面"资讯解读"模块一样的写法——异常时把错误信息当成
            # 这次的展示文本存进缓存，不再 return，让后面的区块正常渲染。
            try:
                with st.spinner("正在生成AI解读…"):
                    ai_text = _stream_ai_text(analyze_index(name, technical_summary, news_summary))
                st.session_state[f"{idx_ai_key}_cross"] = {"ai_text": ai_text}
            except Exception as e:
                ai_text = f"分析失败：{e}"
                st.session_state[f"{idx_ai_key}_cross"] = {"ai_text": ai_text}
                st.error(ai_text)
        else:
            st.markdown(st.session_state[f"{idx_ai_key}_cross"]["ai_text"])

    idx_summary_key = f"{idx_ai_key}_summary"
    with st.container(border=True):
        st.markdown("**总结性分析**")
        if idx_summary_key not in st.session_state:
            try:
                section_texts = {
                    "资讯解读": st.session_state.get(f"{idx_ai_key}_news", {}).get("ai_text", ""),
                    "综合数据分析": st.session_state.get(f"{idx_ai_key}_cross", {}).get("ai_text", ""),
                }
                with st.spinner("正在汇总生成总结性分析…"):
                    st.session_state[idx_summary_key] = _stream_ai_text(
                        summarize_overall(name, section_texts), raise_on_error=False,
                    )
            except Exception as e:
                st.session_state[idx_summary_key] = f"汇总失败：{e}"
        _render_overall_summary(st.session_state[idx_summary_key])


@st.fragment(run_every=10)
def _render_positions_today_pnl(positions: list):
    """今日收益，要求实时同步——跟_render_position_rows一样每10秒刷新
    （2026-09-02从3秒调宽到10秒：批量化之后单次调用变1次了，但两个
    fragment(这个+_render_position_rows)各自每3秒都打一次批量请求，
    加起来每30秒仍要吃掉20次调用额度，占富途"每30秒最多60次"限流的
    1/3，叠加sim_agent/sim_snapshot那两个后台cron或者别的手动查询，
    还是有概率撞限流导致页面卡住。10秒是参考AI模拟炒股那个页面
    （建HK+US两个市场连接更重，特意选了15秒）定的，肉眼感知不到明显
    延迟，调用量降到6次/30秒，给别的并发查询留足余量）。

    2026-09-01真实故障纠偏：这里原来靠"get_stock_realtime本身@st.cache_data
    (ttl=3)，同一只股票3秒内被两个fragment各查一次第二次命中缓存"这个隐性
    假设省请求——前提是_render_position_rows也在调get_stock_realtime，
    帮忙把缓存填上。后来_render_position_rows改成用
    get_stock_realtime_futu_batch(见那边的真实故障修复注释：22支自选股票
    每3秒各自单独查询撞上Futu"每30秒最多60次"的限流)，这个隐性的缓存共享
    就断了——这个fragment会退回成每支持仓各自单独查询，重新变成撞限流的
    源头之一。这里也改成同一套批量查询，两个fragment都不再依赖"运气好
    刚好命中另一个fragment填的缓存"这种脆弱的隐性耦合。
    """
    if not _fragment_alive("positions"):
        return

    holding_items = [w for w in positions if (w.get("shares") or 0) > 0]
    if not holding_items:
        st.caption("今日收益：暂无真实持仓。")
        return

    hk_us_items = [
        (it["symbol"], it.get("market", "A")) for it in holding_items if it.get("market", "A") in ("HK", "US")
    ]
    try:
        hk_us_quotes = get_stock_realtime_futu_batch(hk_us_items) if hk_us_items else {}
    except Exception:
        hk_us_quotes = {}

    def _fetch_today(item):
        symbol, market = item["symbol"], item.get("market", "A")
        if market in ("HK", "US"):
            spot = hk_us_quotes.get((symbol, market)) or {}
        else:
            try:
                spot = get_stock_realtime(symbol, market=market)
            except Exception:
                spot = {}
        price, prev_close = spot.get("最新价"), spot.get("昨收")
        if not price or not prev_close:
            return None
        pnl_native = (price - prev_close) * item["shares"]
        pnl_cny, _note = to_cny(pnl_native, item.get("currency", "CNY"))
        value_cny, _note2 = to_cny(price * item["shares"], item.get("currency", "CNY"))
        if pnl_cny is None or value_cny is None:
            return None
        # 场外基金/上金所现货走的是T-1净值兜底(见get_stock_realtime里的
        # _fetch_otc_fund_quote/_fetch_sge_spot_quote)，"最新价/昨收"字段
        # 形状跟真实实时行情一样，但含义是"上一个披露日 vs 再前一日"，不是
        # "今天 vs 昨天"——直接汇总进这个每3秒刷新的"今日收益"会让隔夜没
        # 更新的净值变化看起来像是实时跳动的盈亏，是真实的误导，不是无害的
        # 显示细节，必须单独标记出来。
        is_stale = str(spot.get("数据源", "")).endswith("(T-1)")
        return pnl_cny, value_cny, is_stale

    results = _run_concurrent_with_deadline(holding_items, _fetch_today, timeout=6)
    total_pnl, total_value, skipped, stale_count = 0.0, 0.0, 0, 0
    for i in range(len(holding_items)):
        r = results.get(i)
        if r is None:
            skipped += 1
            continue
        total_pnl += r[0]
        total_value += r[1]
        if r[2]:
            stale_count += 1

    if total_value <= 0:
        st.caption("今日收益：行情/汇率暂时都获取不到，稍后重试。")
        return

    pnl_pct = total_pnl / (total_value - total_pnl) * 100 if (total_value - total_pnl) else 0
    pnl_color = UP_COLOR if total_pnl >= 0 else DOWN_COLOR
    st.markdown(
        f"<div style='font-size:0.8rem;color:var(--fa-muted)'>今日收益</div>"
        f"<div style='font-size:1.6rem;font-weight:700;color:{pnl_color}'>"
        f"{total_pnl:+,.0f} <span style='font-size:1rem'>（{pnl_pct:+.2f}%）</span></div>",
        unsafe_allow_html=True,
    )
    if skipped:
        st.caption(f"有 {skipped} 支持仓因行情/汇率暂时获取不到，未计入。")
    if stale_count:
        st.caption(f"其中 {stale_count} 支场外基金/贵金属现货用的是上一披露日净值（非实时），已计入合计。")


def _render_max_capital_input(email: str):
    """最大资金投入量（折人民币，手动设定）——用户明确要求"AI要知道我们
    总共有多少钱，不能盲目加仓"，advise_portfolio只看得到已经买了多少，
    看不到用户自己心里的资金上限，这里让用户手动设一下，写进
    user_settings表，advise_portfolio读取后能算"还剩多少额度"。不设是
    合法状态(None)，不强制填。"""
    current = get_max_capital(email)
    new_value = st.number_input(
        "最大资金投入量（¥，AI组合分析会参考，不填则不限制）",
        min_value=0.0, value=float(current) if current else 0.0, step=1000.0,
        key=f"_max_capital_input_{email}",
    )
    if st.button("保存", key=f"_max_capital_save_{email}", use_container_width=True):
        set_max_capital(email, new_value if new_value > 0 else None)
        st.success("已保存。")


def _render_price_alerts_manager(email: str):
    """「我的」页的到价提醒总览（升级路线图第2条）。

    详情页那个 popover 只看得见当前这支票的提醒，设完就散在各个页面里，
    过两天想不起来自己设过什么。这里一页看完全部，能删、能把已触发的重新
    启用。
    """
    st.markdown("**到价提醒**")
    try:
        alerts = get_price_alerts(email)
    except Exception:
        st.caption("提醒列表暂时读不出来。")
        return
    if not alerts:
        st.caption("还没有设置到价提醒。在任意个股详情页点「设到价提醒」可以添加，"
                   "价格到了会在盘中推微信给你。")
        return

    active = [a for a in alerts if a["enabled"]]
    st.caption(
        f"生效中 {len(active)} 条，已触发 {len(alerts) - len(active)} 条——"
        f"开盘时段检查，触发一次后自动停用。"
    )
    _mk = {"A": "沪深", "HK": "港股", "US": "美股"}
    for a in alerts:
        c1, c2, c3 = st.columns([3.2, 1, 1])
        with c1:
            tone = "var(--fa-text)" if a["enabled"] else "var(--fa-faint)"
            st.markdown(
                f"<div style='padding:6px 0'>"
                f"<span style='color:{tone};font-weight:600;font-size:0.88rem'>{_esc(a['name'] or a['symbol'])}</span>"
                f"<span style='color:var(--fa-faint);font-size:0.74rem'>　{_esc(a['symbol'])} · "
                f"{_esc(_mk.get(a['market'], a['market']))}</span>"
                f"<div style='font-size:0.78rem;color:var(--fa-text-2)'>{_esc(_alert_line(a))}</div>"
                + (f"<div style='font-size:0.72rem;color:var(--fa-faint)'>{_esc(a['note'])}</div>"
                   if a.get("note") else "")
                + "</div>",
                unsafe_allow_html=True,
            )
        with c2:
            if a["enabled"]:
                if st.button("停用", key=f"_al_off_{a['id']}", type="tertiary"):
                    set_price_alert_enabled(email, a["id"], False)
                    st.rerun()
            else:
                if st.button("重新启用", key=f"_al_on_{a['id']}", type="tertiary"):
                    set_price_alert_enabled(email, a["id"], True)
                    st.rerun()
        with c3:
            if st.button("删除", key=f"_al_rm_{a['id']}", type="tertiary"):
                delete_price_alert(email, a["id"])
                st.rerun()


def _render_risk_profile_input(email: str):
    """风险偏好设置（2026-09-12新增，前端审计第12条）。

    "今日可执行清单"里一直在写"盈亏比低于风险档案下限 2:1，只观察不给
    下单数量"这类话，但这份档案此前只存在于 data/risk_profile.json，网站上
    没有任何地方能看到它、更没法改——用户被一个看不见也够不着的规则挡着。
    这里把四个真正参与闸门判断的字段摆出来让用户自己设。

    注意不要在这里显示或编辑 starting_capital / allowed_markets：那两个字段
    不参与新开仓闸门，放进来只会让这个设置面板看起来像"全局资金设置"，跟
    上面那个"最大资金投入量"语义打架。risk_policy.save_profile 是读-改-写，
    不会动这里没列出来的字段。
    """
    import risk_policy

    profile = risk_policy.load_profile() or {}
    st.markdown("**风险偏好**")
    st.caption("可执行清单的闸门参数：不满足这几条的标的只会被列为“仅观察”，不会给出下单数量。")

    # 兜底值必须跟出厂的 risk_profile.example.json 一致（1% / 10% / 3% / 2.5）。
    # 2026-09-13 审计抓到：这里原来的兜底是 10% / 100% / 10% / 2.0，是整组里
    # 最宽松的一套——单一标的 100% 等于这道闸门根本不存在，而界面还把它
    # 介绍成"闸门参数"，会让人以为有保护。没有配置文件的用户第一次打开这个
    # 面板，看到的就该是一套真正能兜住的默认值，而不是等于没设。
    _DEF = {"min_reward_risk": 2.5, "max_risk_per_trade_pct": 1.0,
            "max_position_pct": 10.0, "max_daily_loss_pct": 3.0}

    min_rr = st.number_input(
        "最低盈亏比（清单里那句“低于风险档案下限 X:1”就是这个值）",
        min_value=0.5, max_value=10.0, step=0.5,
        value=float(profile.get("min_reward_risk") or _DEF["min_reward_risk"]),
        key=f"_risk_min_rr_{email}",
        help="常见区间 2:1 ~ 3:1。低于 1.5:1 意味着赢的时候赚得比输的时候亏得还少。",
    )
    max_risk = st.number_input(
        "单笔最大亏损占总资金比例（%）",
        min_value=0.1, max_value=100.0, step=0.5,
        value=float(profile.get("max_risk_per_trade_pct") or _DEF["max_risk_per_trade_pct"]),
        key=f"_risk_per_trade_{email}",
        help="业界常规是 1%~2%。设成 10% 意味着连错 7 次本金就腰斩。",
    )
    max_pos = st.number_input(
        "单一标的最大仓位占比（%）",
        min_value=1.0, max_value=100.0, step=5.0,
        value=float(profile.get("max_position_pct") or _DEF["max_position_pct"]),
        key=f"_risk_max_pos_{email}",
        help="常见区间 10%~25%。设成 100% 等于这道闸门不存在，单一标的可以吃掉全部资金。",
    )
    max_daily = st.number_input(
        "单日最大回撤容忍度（%）",
        min_value=0.1, max_value=100.0, step=0.5,
        value=float(profile.get("max_daily_loss_pct") or _DEF["max_daily_loss_pct"]),
        key=f"_risk_daily_{email}",
        help="常见区间 2%~5%。触发后当天不再新开仓。",
    )

    # 只改兜底值救不了已经存成 100% 的档案——那份 json 已经落盘，兜底只在
    # 字段缺失时才生效。所以这里对"等于没有风控"的取值直接点名，但不擅自
    # 改用户的数：这是他的风险偏好，我们能做的是确保他知道自己设的是什么。
    _danger = []
    if max_pos >= 100:
        _danger.append("**单一标的 100%** 等于这道闸门不存在——一只票就能吃掉全部资金")
    elif max_pos > 40:
        _danger.append(f"单一标的 {max_pos:g}% 偏高（常见 10%~25%）")
    if max_risk >= 10:
        _danger.append(f"**单笔亏损 {max_risk:g}%** 远高于常规的 1%~2%，连错 7 次本金腰斩")
    elif max_risk > 3:
        _danger.append(f"单笔亏损 {max_risk:g}% 偏高（常见 1%~2%）")
    if min_rr < 1.5:
        _danger.append(f"盈亏比下限 {min_rr:g}:1 偏低，赢的时候赚得可能比输的时候亏得还少")
    if _danger:
        st.warning(
            "当前这组参数的保护力度很弱：\n\n"
            + "\n".join(f"- {d}" for d in _danger)
            + "\n\n这是你自己的偏好，系统不会替你改；但"
              "上面那句“不满足条件只列为仅观察”在这组值下几乎拦不住任何标的。"
        )

    if st.button("保存风险偏好", key=f"_risk_save_{email}", use_container_width=True):
        try:
            risk_policy.save_profile({
                "min_reward_risk": min_rr,
                "max_risk_per_trade_pct": max_risk,
                "max_position_pct": max_pos,
                "max_daily_loss_pct": max_daily,
            })
            st.success("已保存。下一次生成可执行清单时生效。")
        except (ValueError, OSError) as e:
            st.error(f"保存失败：{e}")


@st.fragment(run_every=15)
def _render_ai_sim_live_snapshot(email: str, equity_points: list):
    """AI模拟盘的现金/持仓市值/浮盈浮亏这部分单独抽出来自动刷新——用户
    反馈"持仓收益好久没更新"，查证后发现_render_ai_sim_dashboard本身没有
    任何自动刷新机制，只有用户手动触发rerun（切tab/点按钮）才会重新查询
    Futu账户，之前看到的数字不是错的（跟当时的实时行情核对过是对的），
    只是页面不会自己动。这里用run_every=15秒刷新——比持仓页真实持仓那个
    10秒刷新（_render_position_rows，2026-09-02从3秒调宽到10秒，见那边
    注释）还要更保守一点，因为这里每次刷新要建HK+US两个市场的Futu交易
    连接，比单纯查行情更重。
    历史决策记录/下单记录/图表不放在这个fragment里——那些只有AI每5分钟
    跑一次才会变，没必要跟着这里一起抖。
    """
    if not _fragment_alive("sim"):
        return

    with st.spinner("读取模拟盘状态..."):
        try:
            snapshot = sim_trader.get_agent_snapshot()
        except Exception as e:
            st.error(f"模拟盘状态读取失败：{e}")
            snapshot = None

    if not snapshot:
        return

    # 虚拟现金和持仓市值分开展示，不只给一个合并数字——用户明确要求
    # 能看清"钱花出去多少变成了股票、还剩多少现金"，不是只有一个净值
    # 黑箱数字。总额(=两者之和)标注清楚起始基准，这个基准数字本身不会变，
    # 变的是净值相对它涨跌了多少——2026-09-02用户明确要求"起始值设成
    # 一万美金重新来过"，标签直接读sim_agent._VIRTUAL_BUDGET_HKD，不再
    # 写死"十万港币"这个已经过期的数字，以后基准数字再变也不用记得回来
    # 改这里的文案。
    #
    # 2026-09-02用户进一步明确要求"所有东西都以美元计价不要用港币了"——
    # 内部记账（tracker/sim_trader/sim_agent那一整套）继续用港币做统一
    # 结算币种不动（港股本来就是港币计价，改成内部按美元记账要连带改
    # 数据库schema和大量已经跑通的历史数据，风险跟收益不成比例）；这里
    # 只改显示层，读到的还是港币金额，展示前统一除以USD_HKD_RATE换算成
    # 美元再打印"$"前缀，用户在页面上看到的从头到尾都是美元，感知不到
    # 背后其实是港币记账。
    # 2026-09-11修：不能直接用snapshot["holdings_value_hkd"]——这个SIMULATE
    # 账户不是AI独占的沙盒，账户里躺着两笔从未经AI下单、来源不明的持仓
    # （前端审计发现的账目bug：虚拟现金因为AI没在这两笔上花过钱，一直停在
    # 起始本金没被扣过，这两笔的市值原样加进净值，就凭空多出一截"收益"——
    # 现金没花出去，市值却被当成赚的算了进去）。改成只用AI自己真正下单
    # 买过的仓位（按simulated_orders成交流水核对），账户里其他仓位单独
    # 展示，不静默丢弃。
    _reconciled = sim_trader.get_ledger_reconciled_holdings(email, snapshot)
    _usd_rate = sim_trader.USD_HKD_RATE
    holdings_value = _reconciled["ai_value_hkd"]
    virtual_cash = get_sim_virtual_cash(email)
    if virtual_cash is None:
        virtual_cash = sim_agent._VIRTUAL_BUDGET_HKD
    net_value = holdings_value + virtual_cash

    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("虚拟现金（剩余可用）", f"${virtual_cash / _usd_rate:,.0f}")
    with col2:
        st.metric("持仓市值（仅AI自己买入的）", f"${holdings_value / _usd_rate:,.0f}")
    with col3:
        st.metric("总额（起始$10,000）", f"${net_value / _usd_rate:,.0f}")

    if _reconciled["foreign_positions"]:
        _foreign_bits = "、".join(
            f"{p.get('name') or p.get('code')} HK${(p.get('market_val_hkd') or 0):,.0f}"
            for p in _reconciled["foreign_positions"]
        )
        st.caption(
            f"另有 {len(_reconciled['foreign_positions'])} 笔非AI持仓（{_foreign_bits}），"
            f"不计入净值和收益率。可在富途App里自行平仓清掉。"
        )

    # 累计收益率的基准是起始本金，不是"图表窗口里第一个快照点"。
    # 2026-09-11修：原来拿equity_points的最早一点当基准，而那个列表只覆盖
    # 图表窗口（不是全部历史），算出来的"累计"比"本月"还小——页面上同时
    # 摆着"总额（起始$10,000）$15,442"和"累计收益率+16.58%"、"本月收益
    # +54.42%"三个互相矛盾的数字。累计就该是相对起始本金，跟上面那张
    # "总额（起始$10,000）"卡片同一个口径。
    _start_capital_usd = sim_agent._VIRTUAL_BUDGET_HKD / _usd_rate
    if _start_capital_usd:
        change_pct = (net_value / _usd_rate - _start_capital_usd) / _start_capital_usd * 100
        st.metric("累计收益率（相对起始本金）", f"{change_pct:+.2f}%")
    if snapshot["skipped_markets"]:
        st.caption(f"以下市场暂时没查到模拟账户：{'、'.join(snapshot['skipped_markets'])}")

    # 本日/昨日/本月收益——用户明确要求这三个分开的时间窗口，各自带一个
    # 收益百分比，负收益就是负数（不用红绿颜色掩盖，数字本身带符号最直接）。
    # 找不到某个窗口的数据（比如AI今天/本月还没运行过）时如实显示"暂无
    # 数据"，不拿0冒充"没有变化"。
    pnl = get_period_pnl(email, net_value)
    pnl_col1, pnl_col2, pnl_col3 = st.columns(3)
    for col, label, key in ((pnl_col1, "本日收益", "today"), (pnl_col2, "昨日收益", "yesterday"), (pnl_col3, "本月收益", "month")):
        block = pnl.get(key)
        with col:
            if block:
                # delta_color="inverse"：Streamlit默认是"涨绿跌红"的西方约定，
                # 而这个项目全站用的是"红涨绿跌"的中式约定（见theme.py）。
                # 不指定的话，同一个页面上亏损的百分比是红的、而下面持仓里
                # 下跌的股票是绿的，两套配色互相打架。
                st.metric(
                    label, _fmt_usd_signed(block["change"] / _usd_rate),
                    f"{block['pct']:+.2f}%", delta_color="inverse",
                )
            else:
                st.metric(label, "暂无数据")

    if snapshot["positions"]:
        st.markdown("**当前持仓**")
        for p in snapshot["positions"]:
            pl_color = UP_COLOR if (p["pl_val"] or 0) >= 0 else DOWN_COLOR
            # pl_val是持仓自己原始币种(HKD港股/USD美股)的浮盈亏，统一换成美元
            # 展示——港股仓位除以汇率折成美元，美股仓位本来就是美元不用转。
            if p["pl_val"] is not None:
                pl_usd = p["pl_val"] if p["currency"] == "USD" else p["pl_val"] / _usd_rate
                pl_text = _fmt_usd_signed(pl_usd, decimals=2)
            else:
                pl_text = "—"
            # 非AI下单的那几笔要在列表里就标出来，不能只靠上面那句说明。
            # 2026-09-12实机看下来：上面净值卡片写着"仅AI自己买入的"，下面
            # 这个列表却把遗留仓位和AI刚买的GLD混在一起平铺，读者对不上账，
            # 会以为净值算漏了。
            _is_foreign = any(
                fp.get("code") == p.get("code") and fp.get("market") == p.get("market")
                for fp in _reconciled["foreign_positions"]
            )
            _tag = ("<span style='font-size:0.7rem;color:var(--fa-faint);margin-left:8px'>"
                    "非AI持仓 · 不计入净值</span>") if _is_foreign else ""
            st.markdown(
                f"<div style='display:flex;justify-content:space-between;padding:4px 0"
                f"{';opacity:.55' if _is_foreign else ''}'>"
                f"<span>{_esc(p['name'])}（{_esc(p['code'])}）· {p['qty']:g}股{_tag}</span>"
                f"<span style='color:{pl_color}'>{pl_text}</span></div>",
                unsafe_allow_html=True,
            )
    else:
        st.caption("当前空仓。")


def _render_sim_manager_stats(email: str, equity_points: list[dict]):
    """AI基金经理：基准对比 + 专业指标（升级路线图第7条）。

    这一块的设计原则是**数据不够就不给数字**，数学在 sim_metrics.py。

    为什么要专门强调：写这段时这个盘刚重置成1万美金，数据库里只有1个交易日的
    净值快照。年化收益、夏普比率这类指标照公式硬算全都算得出来，而且看起来很
    专业——把1天的涨跌按252个交易日外推，+0.5%的一天会变成"年化+250%"。这个
    项目的定位是"亏的也放在里面，没有挑过"，在指标上编数字比不显示指标伤害
    大得多。所以样本不够的指标一律不显示，并且明说还差多少个交易日。
    """
    if not equity_points:
        return
    start_capital = sim_agent._VIRTUAL_BUDGET_HKD / sim_trader.USD_HKD_RATE
    try:
        orders = get_simulated_orders(email, limit=500)
    except Exception:
        orders = []
    try:
        res = sim_metrics.compute(equity_points, start_capital, orders)
    except Exception:
        return
    if not res:
        return

    st.divider()
    st.markdown("**AI基金经理**")

    # ── 基准对比 ────────────────────────────────────────────────────
    # 跑赢没跑赢是这一块最该先回答的问题，放在指标之前。
    bench_df, bench_name = None, "标普500"
    try:
        _start = min(p["run_at"] for p in equity_points).strftime("%Y%m%d")
        _end = cn_now().strftime("%Y%m%d")
        bench_df = get_benchmark_history(_start, _end, market="US")
    except Exception:
        bench_df = None
    fig = build_sim_vs_benchmark(equity_points, bench_df, bench_name)
    if fig is not None:
        st.caption(f"AI净值 vs {bench_name}，两条线都归一到100（同样投100块，"
                   f"现在各自变成多少）。基准按AI起跑那天对齐。")
        st.plotly_chart(fig, use_container_width=True, config=_PLOTLY_CONFIG,
                        key="_sim_vs_bench")

    # ── 指标 ────────────────────────────────────────────────────────
    def _pct(v):
        return f"{v:+.2%}" if v is not None else "—"

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("区间收益", _pct(res.get("total_return")))
    c2.metric("最大回撤", f"{res['max_drawdown']:.2%}" if "max_drawdown" in res else "—")
    c3.metric("年化收益", _pct(res.get("annual_return")) if "annual_return" in res else "—")
    c4.metric("夏普比率", f"{res['sharpe']:.2f}" if "sharpe" in res else "—")

    c5, c6, c7, c8 = st.columns(4)
    c5.metric("成交笔数", str(res.get("n_trades", 0)))
    c6.metric("平仓回合", str(res.get("round_trips", 0)))
    c7.metric("胜率", f"{res['win_rate']:.0%}" if "win_rate" in res else "—")
    c8.metric("平均持仓", f"{res['avg_holding_days']:.1f}天" if "avg_holding_days" in res else "—")

    notes = []
    if not res.get("enough_for_annual"):
        need = res.get("min_days_for_annual", 20) - res.get("n_days", 0)
        notes.append(
            f"年化收益和夏普比率暂时不显示：只有 {res.get('n_days', 0)} 个交易日的净值，"
            f"还差 {max(need, 0)} 天。把一两天的涨跌按252个交易日外推出来的"
            f"「年化」不是收益率，是放大后的噪声。"
        )
    if res.get("round_trips", 0) == 0:
        notes.append("还没有完成的平仓回合（买入后尚未卖出），所以胜率和平均持仓天数暂时算不出来。")
    elif "win_rate" not in res:
        notes.append("有平仓回合但缺成交价，胜率暂时算不出来——成交价由系统在盘中自动回填，稍后会补上。")
    if "turnover" in res:
        notes.append(f"区间换手率 {res['turnover']:.2f} 倍（累计成交金额 ÷ 当前净值），未年化。")
    if "sharpe" in res:
        notes.append("夏普比率按无风险利率为0计算；当前美债10年期约5%，因此这个数偏乐观。")
    notes.append(f"最大回撤用的是全部盘中快照（共{res.get('n_points', 0)}个点），"
                 f"不是日线——回撤问的是最难受的时候有多难受，压成日线会把日内的坑抹平。")
    for n in notes:
        st.caption(n)


def _render_ai_sim_dashboard():
    """AI模拟炒股页——2026-09-01用户明确要求"回看页全部改成AI模拟炒股，我需要
    看到它的持仓、收益和相关的所有交易记录"，完全取代原来的AI判断准确率
    追踪（_render_accuracy_dashboard函数还留着，只是导航不再调用它——
    用户是要"完全替换"这个入口，不是要删掉底层历史数据，函数和数据表都
    保留，只是不在这个入口展示）。

    展示的是sim_agent.py那条每5分钟一次的自主决策链路（只交易港股/美股，
    沪深不参与，起始本金1万美金，2026-09-02从十万港币改的——内部记账仍按
    港币结算，页面展示层统一折成美元，见_render_ai_sim_live_snapshot里
    _usd_rate那处说明），不是持仓页那个"跟着每天17:30组合分析走"的模拟盘
    （那个继续在持仓页自己的开关那块，两条链路各自独立）。

    2026-09-01用户明确要求"其他用户都能看见，就是让他们看内置AI靠不靠谱
    的"——这不是per-user数据，是同一个固定账号(sim_agent.advisor._EMAIL)
    的展示台，不区分是谁在看，所以不接收email参数，也不像持仓/自选那样
    卡登录墙，游客也能直接看。同一个理由，"AI自主模拟交易"开关也去掉了
    （用户反馈"省的误触断掉"）——这本来就该是个一直开着的展示性功能，不
    是需要谁去手动控制的开关，误触关掉会让其他访客看到的是"已关闭"而不是
    真实在跑的AI，得不偿失。sim_agent.py里那层sim_agent_enabled检查还留着
    当兜底（真要停可以直接改数据库），只是页面上不再暴露这个开关。
    """
    email = sim_agent.advisor._EMAIL


    # 200 而不是 30：连续失败的时间一长（2026-09-09 供应商欠费那次连挂了两天、
    # 几百次），30 条窗口里一条成功记录都翻不到，横幅就会说"最后一次成功 无记录"，
    # 把"翻不到"讲成了"从来没成功过"。
    runs = get_sim_agent_runs(email, limit=200)

    # AI停摆要在页面顶部说清楚（2026-09-11前端审计）。真实发生过的情况：
    # 2026-09-09 15:56 起AI供应商账户欠费，之后每次决策都失败，但页面顶部
    # 的收益卡片照常显示、看起来一切正常，用户完全看不出这个盘已经两天
    # 没人管了——收益数字还在动（持仓市值跟着行情波动），只是没有任何新
    # 决策。这种"坏了但看着没坏"比直接报错更危险。
    _last_ok = next((r for r in runs if str(r.get("status") or "") != "失败"), None)
    _latest = runs[0] if runs else None
    if _latest is not None and str(_latest.get("status") or "") == "失败":
        _ok_dt = _to_cn_dt(_last_ok.get("run_at")) if _last_ok else None
        _ok_text = _ok_dt.strftime("%m-%d %H:%M") if _ok_dt else "最近200次内没有成功记录"
        _fail_dt = _to_cn_dt(_latest.get("run_at"))
        _fail_text = _fail_dt.strftime("%m-%d %H:%M") if _fail_dt else ""
        # 原始报错（含供应商的Request id、整段JSON）只写日志，不摆给用户看。
        __import__("logging").getLogger(__name__).warning(
            "[sim] 最近一次AI决策失败：%s", _latest.get("note") or "")
        st.warning(
            f"AI 决策已暂停：最近一次尝试（{_fail_text}）没有成功，"
            f"最后一次成功决策是 {_ok_text}。"
            f"下方的收益和持仓数字仍会跟着行情波动，但期间没有产生任何新的买卖决策。"
        )

    # 走势图数据源用sim_equity_snapshots(每几分钟一次，跟AI决策频率解耦)，
    # 不再用sim_agent_runs的决策快照(15分钟一次)——用户反馈"遇到低波动
    # 持仓连续几次数字精确不变，图表看着像死了"，见sim_snapshot.py开头
    # 的说明。"AI每次决策记录"那个列表(下面)继续用runs，两者互不影响。
    snapshots = get_equity_snapshots(email, limit=500)
    equity_points = []
    for s in snapshots:
        _dt = _to_cn_dt(s.get("snapshot_at"))
        if _dt is None:
            continue
        # assets_hkd这个key名字留着没改（改名字要连带改_render_ai_sim_
        # live_snapshot/build_sim_equity_curve两处读这个key的地方，纯粹
        # 增加改动面）——但存进去的值已经是折算成美元后的数字，2026-09-02
        # "所有东西都以美元计价"这条要求下，这个列表从产生的那一刻起
        # 就是美元口径，下游不管是算百分比（比例跟单位无关，不受影响）
        # 还是画图表（直接就是美元数字，不用再转一次）都不用关心这件事。
        equity_points.append({"run_at": _dt, "assets_hkd": s["net_value_hkd"] / sim_trader.USD_HKD_RATE})

    _render_ai_sim_live_snapshot(email, equity_points)

    if len(equity_points) >= 2:
        # 用户明确要求走势图分天/周/月三种模式看——快照本身就只在开盘时段
        # 才记（sim_snapshot.py没开盘直接跳过不落库），非交易日/非开盘时段
        # 天然没有数据点，不用额外过滤"非交易日"。
        _view_key = f"_ai_sim_chart_view_{email}"
        view = st.radio(
            "走势图范围", ["天", "周", "月"], horizontal=True,
            key=_view_key, label_visibility="collapsed",
        )
        now_cn = datetime.now(timezone(timedelta(hours=8)))
        if view == "天":
            latest_date = max(p["run_at"].date() for p in equity_points)
            windowed = [p for p in equity_points if p["run_at"].date() == latest_date]
            granularity = "day"
        elif view == "周":
            cutoff = now_cn - timedelta(days=7)
            windowed = [p for p in equity_points if p["run_at"] >= cutoff]
            granularity = "week"
        else:
            cutoff = now_cn - timedelta(days=30)
            windowed = [p for p in equity_points if p["run_at"] >= cutoff]
            granularity = "month"
        if len(windowed) >= 2:
            st.plotly_chart(
                build_sim_equity_curve(
                    windowed, baseline=sim_agent._VIRTUAL_BUDGET_HKD / sim_trader.USD_HKD_RATE, granularity=granularity,
                ),
                use_container_width=True, config=_PLOTLY_CONFIG,
            )
        else:
            st.caption(f"「{view}」这个范围内数据点还不够画线——换个更大的范围看看，或者等AI多跑几轮。")
    else:
        st.caption("数据点还不够，多跑几轮后这里会出现走势图")

    _render_sim_manager_stats(email, equity_points)

    # 2026-09-03用户明确要求"折线图下面做两个饼状图，一个港股/美股/剩余资金
    # 各占比例，一个持仓股票比例"——这里单独再查一次实时快照，跟上面
    # _render_ai_sim_live_snapshot那个fragment各自独立（那个fragment
    # 15秒自动刷新，这两个饼图跟着整页正常重跑就够，不需要同样高频，
    # 复用同一份数据反而要把这两个饼图也塞进那个fragment，职责会混在一起）。
    # 复用持仓页现成的build_position_donut，只是新增的currency_symbol参数
    # 传"$"——这个页面2026-09-02已经全部改成美元展示，不能沿用那边默认的¥。
    try:
        _donut_snapshot = sim_trader.get_agent_snapshot()
        # ⚠️ 必须用账本核对过的 ai_positions，不能用 snapshot["positions"]。
        # 2026-09-12 审计实测：同一个页面顶部写着"账户里还有2笔非AI下单的持仓…
        # 不计入上面的净值/收益率"（净值走 get_ledger_reconciled_holdings 过滤过），
        # 而这两个饼图直接用了账户原始持仓，于是"总资产 $11,995"比顶部的
        # "总额 $9,969"多出 $2,026，饼里还明明白白列着新奥能源、滨化股份，
        # "港股持仓 16.9%"整块都来自这两笔非AI仓——同一屏两个自相矛盾的口径。
        _donut_positions = sim_trader.get_ledger_reconciled_holdings(
            email, _donut_snapshot)["ai_positions"]
        _donut_cash = get_sim_virtual_cash(email)
        if _donut_cash is None:
            _donut_cash = sim_agent._VIRTUAL_BUDGET_HKD
        _usd_rate = sim_trader.USD_HKD_RATE
        _hk_value = sum(
            p["market_val_hkd"] for p in _donut_positions
            if p["market"] == "HK" and p.get("market_val_hkd") is not None
        )
        _us_value = sum(
            p["market_val_hkd"] for p in _donut_positions
            if p["market"] == "US" and p.get("market_val_hkd") is not None
        )
        _total_usd = (_hk_value + _us_value + _donut_cash) / _usd_rate

        st.divider()
        st.markdown("**资产分布**")
        donut_col1, donut_col2 = st.columns(2)
        with donut_col1:
            st.caption("港股 / 美股 / 剩余现金")
            allocation_rows = [
                {"label": "港股持仓", "value_cny": _hk_value / _usd_rate},
                {"label": "美股持仓", "value_cny": _us_value / _usd_rate},
                {"label": "剩余现金", "value_cny": _donut_cash / _usd_rate},
            ]
            allocation_rows = [r for r in allocation_rows if r["value_cny"] > 0]
            if allocation_rows and _total_usd > 0:
                st.plotly_chart(
                    build_position_donut(allocation_rows, _total_usd, currency_symbol="$", show_legend=True),
                    use_container_width=True, config=_PLOTLY_CONFIG, key="_ai_sim_allocation_donut",
                )
            else:
                st.caption("暂无数据。")
        with donut_col2:
            st.caption("持仓个股占比")
            position_rows = sorted(
                (
                    {"label": f"{p['name']}（{p['code']}）", "value_cny": p["market_val_hkd"] / _usd_rate}
                    for p in _donut_positions
                    if p.get("market_val_hkd")
                ),
                key=lambda r: r["value_cny"], reverse=True,
            )
            holdings_total_usd = sum(r["value_cny"] for r in position_rows)
            if position_rows:
                st.plotly_chart(
                    build_position_donut(position_rows, holdings_total_usd, currency_symbol="$", show_legend=True,
                                        center_label="持仓市值"),
                    use_container_width=True, config=_PLOTLY_CONFIG, key="_ai_sim_positions_donut",
                )
            else:
                st.caption("当前空仓。")
    except Exception as e:
        st.caption(f"资产分布图暂时加载失败：{e}")

    st.divider()
    st.markdown("**AI每次决策记录**")
    if not runs:
        st.caption("还没有运行记录")
    else:
        _runs_key = f"_ai_sim_runs_show_all_{email}"
        show_all_runs = st.session_state.get(_runs_key, False)
        visible_runs = runs if show_all_runs else runs[:5]
        for r in visible_runs:
            when = _to_cn_time_str(r.get("run_at"))
            title = f"{when} · {r['status']}" + (
                f" · {_sim_note_for_display(r['note'])}" if r.get("note") else "")
            # 决策记录同样压成发丝线分隔的行，不再是一摞带边框的白盒子——
            # 这里一屏能有五到三十条，方框叠方框是这一段最主要的视觉噪声。
            with st.container(key=f"sim_run_{r.get('run_at','')}"), st.expander(title):
                if r.get("reasoning_text"):
                    st.markdown(_esc(r["reasoning_text"]).replace("\n", "<br>"), unsafe_allow_html=True)
                try:
                    sigs = json.loads(r.get("signals_json") or "[]")
                except Exception:
                    sigs = []
                actionable = [s for s in sigs if s.get("action") in ("买入", "卖出")]
                for s in actionable:
                    # 这几行是"AI这一轮想做什么"，不等于"真的做成了什么"——被预算/
                    # 集中度/非开盘市场拦下来的信号一股都没成交。原来这里无条件按
                    # "买入 甲骨文 1股"渲染，跟同一个展开框标题里的"执行成功0条、
                    # 1条超预算被拦截"直接打架，看着像真买了。sim_agent.py现在把
                    # 归宿写进signals_json了，这里如实标出来。老记录没有这两个
                    # 字段，保持原样不加后缀。
                    if "执行成功0条" in (r.get("note") or "") and not s.get("_drop_reason"):
                        # 老记录没有_drop_reason/_executed，但note里的"执行成功0条"
                        # 同样能说明这一轮一笔都没成交，拿它兜底让历史记录也如实。
                        suffix = " · 未成交"
                    elif s.get("_drop_reason"):
                        suffix = f" · 已拦截未执行（{_esc(s['_drop_reason'])}）"
                    elif s.get("_executed") is False:
                        suffix = " · 未成交"
                    else:
                        suffix = ""
                    st.caption(f"{s['action']} {s['name']}（{s['symbol']}·{s['market']}）{s['shares']:g}股{suffix}")
        if not show_all_runs and len(runs) > 5:
            if st.button(f"更多（最近{len(runs)}条）", key=f"_ai_sim_runs_more_{email}"):
                st.session_state[_runs_key] = True
                st.rerun()

    st.divider()
    st.markdown("**完整下单记录**")
    orders = get_simulated_orders(email, limit=50)
    if not orders:
        st.caption("还没有下单记录。")
    else:
        _orders_key = f"_ai_sim_orders_show_all_{email}"
        show_all_orders = st.session_state.get(_orders_key, False)
        visible_orders = orders if show_all_orders else orders[:10]
        for o in visible_orders:
            # 下单状态不能借用涨跌色。全站是"红涨绿跌"的中式配色，直接套上去
            # 就是"成功"红、"失败"绿——2026-09-11前端审计实测读下来第一反应
            # 是反的。状态是对错语义，不是方向语义，走另一套：成功绿、失败红
            # （这两个词的通用约定），跳过灰。
            status_color = {
                "成功": OK_COLOR, "失败": BAD_COLOR, "跳过": NEUTRAL_COLOR,
            }.get(o["status"], NEUTRAL_COLOR)
            try:
                _ordered = float(o.get("shares_ordered") or 0)
            except (TypeError, ValueError):
                _ordered = 0
            try:
                _requested = float(o.get("shares_signal") or 0)
            except (TypeError, ValueError):
                _requested = 0
            # 模拟市价单在落单时没有可靠成交价，不能伪造价格或金额；至少把
            # 实际委托数量（失败/跳过则计划数量）明确展示出来，方便复核执行。
            _qty = f" · 委托 {_ordered:g} 股" if _ordered > 0 else (
                f" · 计划 {_requested:g} 股" if _requested > 0 else ""
            )
            st.markdown(
                f"<div style='padding:4px 0'>{_to_cn_time_str(o['created_at'])} · "
                f"{_esc(o['name'] or o['symbol'])}（{_esc(o['symbol'])}·{_esc(o['market'])}）· {_esc(o['action'])} · "
                f"<span style='color:{status_color}'>{_esc(o['status'])}</span>{_qty}"
                + (f" · {_esc(o['note'])}" if o["note"] else "") + "</div>",
                unsafe_allow_html=True,
            )
        if not show_all_orders and len(orders) > 10:
            if st.button(f"更多（最近{len(orders)}条）", key=f"_ai_sim_orders_more_{email}"):
                st.session_state[_orders_key] = True
                st.rerun()


def _render_positions_donut(positions: list):
    """持仓占比环形图。只统计真正持仓(shares>0)，纯关注(shares=0)不占份额。
    不用@st.fragment(run_every=3)——Plotly图3秒重绘会明显闪烁（见持仓分析
    方案里的踩坑记录），这块跟着页面正常rerun刷新就够了，不需要独立3秒轮询。
    汇率/实时价任何一项失败就跳过那一支（不拿0或旧数字硬凑），并在图下方
    如实提示"部分持仓因数据获取失败未计入"，不悄悄编一个不准的总资产出来。
    """
    holding_items = [w for w in positions if (w.get("shares") or 0) > 0]
    if not holding_items:
        if positions:
            st.caption("当前只有关注项，暂无真实持仓")
        else:
            st.caption("暂无真实持仓")
        return

    def _fetch_value(item):
        symbol, market = item["symbol"], item.get("market", "A")
        try:
            spot = get_stock_realtime(symbol, market=market)
            price = spot.get("最新价") if spot else None
        except Exception:
            price = None
        if not price:
            return None
        value_cny, _note = to_cny(item["shares"] * price, item.get("currency", "CNY"))
        return value_cny

    results = _run_concurrent_with_deadline(holding_items, _fetch_value, timeout=6)

    holdings, skipped = [], 0
    for i, item in enumerate(holding_items):
        value_cny = results.get(i)
        if value_cny is None:
            skipped += 1
            continue
        holdings.append({"label": f"{item['name']}（{item['symbol']}）", "value_cny": value_cny})

    if not holdings:
        st.caption("行情/汇率暂时都获取不到，稍后重试。")
        return

    holdings.sort(key=lambda h: h["value_cny"], reverse=True)
    total_value_cny = sum(h["value_cny"] for h in holdings)
    st.plotly_chart(build_position_donut(holdings, total_value_cny), use_container_width=True, config=_PLOTLY_CONFIG, key="_positions_donut")
    if skipped:
        st.caption(f"有 {skipped} 支持仓因行情/汇率暂时获取不到，未计入本图。")


def _render_portfolio_risk(positions: list):
    """组合风险体检（升级路线图第4条）。

    路线图里的原话：普通散户最大的问题往往不是选错了某一只股票，而是整个
    组合押在同一个方向上——自选里的美光、英伟达、台积电、海力士、闪迪，
    其实全是同一个押注（存储和AI芯片）。这种风险券商App不会主动提醒。

    这一块全部是本地算的（numpy/pandas），不经过AI，跟AI那段文字判断是两条
    独立的证据链——跟 charts.compute_stats 的定位一样。数学在 portfolio_risk.py，
    这里只负责取数和渲染。

    刻意放在AI组合分析之前：先看客观结构（你实际押在什么上面），再看AI的
    主观解读。反过来的话，读者会带着AI的结论去看数字。
    """
    holding_items = [w for w in positions if (w.get("shares") or 0) > 0]
    if len(holding_items) < 2:
        # 一只股票谈不上"组合风险"，相关性/分散度全部无意义。
        return

    end = cn_now().strftime("%Y%m%d")
    # 取180个自然日≈120个交易日。路线图给的就是120天：再短相关系数不稳，
    # 再长会把早就变了的市场状态(比如上一轮加息周期)混进来当成当下的结构。
    start = (cn_now() - timedelta(days=180)).strftime("%Y%m%d")

    def _fetch(item):
        symbol, market = item["symbol"], item.get("market", "A")
        price, hist = None, None
        try:
            spot = get_stock_realtime(symbol, market=market)
            price = spot.get("最新价") if spot else None
        except Exception:
            price = None
        try:
            hist = get_stock_history(symbol, start, end, market=market)
        except Exception:
            hist = None
        return price, hist

    results = _run_concurrent_with_deadline(holding_items, _fetch, timeout=12)

    holdings, hist_by_symbol, name_by_symbol = [], {}, {}
    for i, item in enumerate(holding_items):
        got = results.get(i)
        if not got:
            continue
        price, hist = got
        if not price:
            continue
        value_cny, _note = to_cny(item["shares"] * price, item.get("currency", "CNY"))
        if value_cny is None:
            continue
        sym = item["symbol"]
        holdings.append({
            "symbol": sym, "name": item.get("name", sym),
            "market": item.get("market", "A"),
            "currency": item.get("currency", "CNY"),
            "value": value_cny,
        })
        name_by_symbol[sym] = item.get("name", sym)
        if hist is not None and not hist.empty and "收盘" in hist.columns and "日期" in hist.columns:
            s = pd.Series(
                pd.to_numeric(hist["收盘"], errors="coerce").values,
                index=pd.to_datetime(hist["日期"]),
            ).dropna()
            # 只保留日期(丢掉时分秒)。各数据源给的时间戳粒度不一样——有的带
            # 收盘时刻、有的是零点，不归一的话两只股票的"同一天"对不上，
            # inner join 之后会直接空掉。
            s.index = s.index.normalize()
            if len(s) >= 2:
                hist_by_symbol[sym] = s

    if len(holdings) < 2:
        return

    # 基准选权重最大的那个市场的指数，并且把名字显示出来。混合组合没有
    # "唯一正确"的基准，与其偷偷选一个不如明说这次是拿谁比的。
    mkt_weight: dict[str, float] = {}
    for h in holdings:
        mkt_weight[h["market"]] = mkt_weight.get(h["market"], 0.0) + h["value"]
    main_market = max(mkt_weight.items(), key=lambda kv: kv[1])[0]
    bench_name = _BENCHMARK_NAMES.get(main_market, "")
    bench_close = None
    if bench_name:
        try:
            bdf = get_benchmark_history(start, end, market=main_market)
            if bdf is not None and not bdf.empty:
                # 注意是 .dt.normalize() 不是 .normalize()：pd.to_datetime 作用在
                # Series 上返回的还是 Series，归一化要走 .dt 访问器。写成
                # .normalize() 会抛 AttributeError，而这里外面包着 try/except，
                # 结果是基准被静默丢掉、Beta 和压力测试一起消失还不报错。
                bench_close = pd.Series(
                    pd.to_numeric(bdf["收盘"], errors="coerce").values,
                    index=pd.DatetimeIndex(pd.to_datetime(bdf["日期"])).normalize(),
                ).dropna()
        except Exception:
            bench_close = None

    try:
        res = portfolio_risk.analyze(holdings, hist_by_symbol, bench_close, bench_name)
    except Exception:
        return
    if not res:
        return

    st.markdown("**组合体检**")

    if res.get("insufficient_history"):
        st.caption(
            f"历史数据只够 {res.get('n_days', 0)} 个交易日，算不出可信的相关性和波动率。"
            "新建仓的标的过一段时间再看。"
        )
        return

    # ── 数字区 ────────────────────────────────────────────────────
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("年化波动率", f"{res['port_vol_annual']:.1%}" if "port_vol_annual" in res else "—")
    with c2:
        st.metric(
            f"对{res.get('benchmark_name','基准')} Beta",
            f"{res['port_beta']:.2f}" if "port_beta" in res else "—",
        )
    with c3:
        st.metric("单日风险(VaR95)", f"{abs(res['var95_pct']):.2%}" if "var95_pct" in res else "—")
    with c4:
        st.metric("有效分散度", f"{res['concentration']['effective_n']:.1f} 只")

    st.caption(
        f"基于最近 {res['n_days']} 个交易日、覆盖 {res.get('coverage', 1):.0%} 的仓位；"
        "都是历史统计量，不是对未来的预测。"
    )

    # ── 暴露度 ────────────────────────────────────────────────────
    def _exposure_line(title: str, data: dict[str, float]) -> str:
        if not data:
            return ""
        parts = " · ".join(f"{k} {v:.0%}" for k, v in data.items())
        return (
            f"<div style='padding:6px 0;border-bottom:1px solid var(--fa-border)'>"
            f"<span style='color:var(--fa-faint);font-size:0.78rem'>{_esc(title)}</span>"
            f"<span style='float:right;font-size:0.82rem;font-variant-numeric:tabular-nums'>"
            f"{_esc(parts)}</span></div>"
        )

    rows_html = _exposure_line("市场暴露", res.get("market_exposure", {}))
    rows_html += _exposure_line("币种暴露", res.get("currency_exposure", {}))
    if res.get("sector_exposure"):
        rows_html += _exposure_line("行业暴露", res["sector_exposure"])
    if rows_html:
        st.markdown(rows_html, unsafe_allow_html=True)

    # ── 相关性矩阵 ────────────────────────────────────────────────
    corr = res.get("corr_matrix")
    if corr is not None and len(corr) >= 2:
        st.markdown("**两两相关性**")
        st.caption("颜色越深表示两只越同涨同跌——深色格子意味着它们其实是同一个押注。")
        fig = build_correlation_heatmap(corr, name_by_symbol)
        if fig is not None:
            st.plotly_chart(fig, use_container_width=True, config=_PLOTLY_CONFIG,
                            key="_portfolio_corr")

    # ── 提示 ──────────────────────────────────────────────────────
    tips = portfolio_risk.build_warnings(res, name_by_symbol)
    if tips:
        st.markdown("**体检提示**")
        for t in tips:
            st.markdown(
                f"<div style='padding:5px 0;font-size:0.84rem;color:var(--fa-text-2);"
                f"line-height:1.6'>· {_esc(t)}</div>",
                unsafe_allow_html=True,
            )
        st.caption("以上是对组合结构的客观描述，不是买卖建议——集中本身不等于错，但应该是想清楚之后的选择。")


_PORTFOLIO_REANALYZE_COOLDOWN = 300  # 5分钟节流——组合分析是1次真实AI调用，不是纯本地计算，不能让用户点着玩


def _render_bold_as_red(text: str) -> str:
    """把AI分析文本里的**加粗**改成红色高亮——用户明确要求"重点标红"。AI
    在_PORTFOLIO_SYSTEM里已经被要求用**加粗**标关键结论，复用这个已有的
    标记习惯改渲染方式，不用再发明新的自定义标记语法。非加粗部分照常转义
    （AI生成文本理论上不该有恶意内容，但统一走_esc()是这个项目一贯的
    习惯，不因为"来源可信"就破例）。"""
    parts = re.split(r"(\*\*.+?\*\*)", text)
    html_parts = []
    for part in parts:
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            html_parts.append(f"<span style='color:{UP_COLOR};font-weight:700'>{_esc(part[2:-2])}</span>")
        else:
            html_parts.append(_esc(part).replace("\n", "<br>"))
    return "".join(html_parts)


def _render_trade_signals(signals_json: str):
    """结构化交易信号——用户明确要求"先只调研+搭好架子，不接真实下单"：
    这里只是把advisor.py解析好的信号(标的/方向/股数/金额)展示成一张能
    直接照着操作的表，不调用富途交易接口(OpenSecTradeContext/place_order)，
    下单动作还是用户自己去券商完成。红涨绿跌配色跟买卖方向复用同一套
    UP_COLOR/DOWN_COLOR（买入用涨色、卖出用跌色，跟这个项目一贯的红绿
    约定保持一致，不是另外发明一套买卖配色）。
    """
    try:
        signals = json.loads(signals_json) if signals_json else []
    except (json.JSONDecodeError, TypeError):
        signals = []
    if not signals:
        return
    action_signals = [s for s in signals if s.get("action") != "不动"]
    if not action_signals:
        st.caption("本次信号：全部维持不动，没有需要操作的标的。")
        return
    st.markdown("**交易信号**")
    for s in action_signals:
        color = UP_COLOR if s["action"] == "买入" else DOWN_COLOR
        st.markdown(
            f"<div style='display:flex;justify-content:space-between;align-items:center;"
            f"padding:6px 10px;margin:4px 0;border-radius:6px;background:var(--fa-card-bg,rgba(0,0,0,.03))'>"
            f"<span>{_esc(s['name'])}（{_esc(s['symbol'])}·{_esc(s['market'])}）</span>"
            f"<span style='color:{color};font-weight:700'>"
            f"{_esc(s['action'])} {s['shares']:g}股 · 约¥{s['amount_cny']:,.0f}</span></div>",
            unsafe_allow_html=True,
        )


def _render_portfolio_advice(email: str, positions: list):
    """AI组合分析卡片——跟左边"今日收益"不同，这块不是实时刷新的（AI调用
    有成本，不能每3秒跑一次），只读advisor.py（每工作日17:30跑）写进
    portfolio_advice表的最近一次结果，另外给一个"立即重新分析"按钮供用户
    现场触发（组合分析只有1次AI调用，跟单支判断动辄几十次不一样，现场跑
    得起），加5分钟节流防止连续点击刷爆DeepSeek账户。
    """
    holding_count = sum(1 for p in positions if (p.get("shares") or 0) > 0)
    advice = get_latest_portfolio_advice(email)

    if advice:
        created = advice["created_at"][:19].replace("T", " ")
        st.caption(f"更新于 {created}（UTC）")

        # 持仓变化检测——2026-08-26真实复现过的bug：用户已经清仓腾讯，但
        # 这张卡片还在展示几天前生成的分析，文字里具体写着"减仓腾讯40股"，
        # 用户如果不细看时间戳很容易误以为这是当前建议。这份分析里的股数/
        # 金额是基于生成那一刻的持仓快照(holdings_json)算的，持仓一旦变化
        # （加仓/减仓/清仓）这些具体数字就直接过期了——拿当前真实持仓的
        # symbol集合跟落库时的快照比对，不一致就强提醒，不能沉默展示。
        try:
            snapshot_symbols = {h["symbol"] for h in json.loads(advice.get("holdings_json") or "[]")}
        except Exception:
            snapshot_symbols = set()
        current_symbols = {p["symbol"] for p in positions if (p.get("shares") or 0) > 0}
        if snapshot_symbols and snapshot_symbols != current_symbols:
            st.warning(
                "你的持仓自这份分析生成后已经变化（加仓/减仓/清仓），"
                "下面提到的具体股数/金额操作建议很可能已经过期，不要直接照做——"
                "建议先看一眼下方持仓列表，再点击下方「立即重新分析」刷新。"
            )

        _render_trade_signals(advice.get("signals_json", ""))
        # 分小节渲染而不是把整段AI原文糊成一片。2026-09-04用户反馈组合分析
        # "说的都没啥逻辑"——内容本来就是分段生成的(总体评估/集中度/行业集中/
        # 市场敞口/宏观适配/逐支跟踪/...)，但渲染时全是同一号字的连续段落，
        # 段名混在正文里，看上去就是一大坨，读者根本分不出哪句是结论哪句是
        # 依据。按段名切开、段名用小标题样式，层次一出来"有没有逻辑"就看得见了。
        _parts = _parse_portfolio_text(advice["analysis_text"])
        if _parts:
            for _name in _PORTFOLIO_SECTIONS:
                _body = _parts.get(_name)
                if not _body:
                    continue
                st.markdown(
                    f"<div style='font-size:0.72rem;letter-spacing:.08em;color:var(--fa-faint);"
                    f"text-transform:none;margin:16px 0 6px'>{_name}</div>",
                    unsafe_allow_html=True,
                )
                st.markdown(_render_bold_as_red(_body), unsafe_allow_html=True)
        else:
            st.markdown(_render_bold_as_red(advice["analysis_text"]), unsafe_allow_html=True)
    else:
        st.caption("AI 组合分析还没生成过。")

    if holding_count < 2:
        st.caption("持仓不足2支，暂不生成集中度分析")
        return

    throttle_key = f"_portfolio_advice_last_{email}"
    elapsed = time.time() - st.session_state.get(throttle_key, 0)
    if elapsed < _PORTFOLIO_REANALYZE_COOLDOWN:
        st.button(f"请稍后再试（{int(_PORTFOLIO_REANALYZE_COOLDOWN - elapsed)}秒冷却）", disabled=True, use_container_width=True)
        return

    if st.button("立即重新分析", key=f"_portfolio_reanalyze_{email}", use_container_width=True):
        st.session_state[throttle_key] = time.time()
        with st.spinner("AI 正在分析组合……（要逐支查行情/新闻+推理+生成交易信号，大约1-3分钟）"):
            try:
                import advisor
                advisor._load_secrets_into_env()
                result = advisor.advise_portfolio(email)
            except Exception as e:
                st.error(f"分析失败：{e}")
                return
        if result is None:
            st.warning("持仓不足2支，暂不生成组合分析。")
        else:
            st.rerun()


@st.fragment(run_every=10)
def _render_position_rows(position_items: list, _email: str, sort_mode: str = "默认"):
    """持仓列表本体单独做成 fragment，价格/涨跌幅每10秒自己刷新（2026-09-02
    从3秒调宽到10秒，理由见_render_positions_today_pnl同一处注释——批量化
    之后单次调用变1次了，但3秒刷新叠加另一个同页fragment，还是有撞富途
    限流的空间，10秒能大幅降低这个概率，肉眼感知不到明显延迟），效仿长桥的
    紧凑列表样式：名称代码 + 迷你走势图 + 现价/成交额 + 涨跌幅色块 + 删除键。
    数字真变了背景闪一下（复用详情页那套red/green flash动画）。每行用
    st.container(border=True)包起来，整行都是一个卡片。

    卡片点击跳转：试过两版JS/CSS方案（覆盖层、DOM遍历绑事件）在真实浏览器里
    都点不动，大概率是猜的Streamlit内部结构不对。这版换成最朴素可靠的办法——
    整块卡片内容包在一个真正的<a href="?open_symbol=...">链接里，点击就是
    标准浏览器导航，不依赖任何猜测。URL参数在脚本最开头统一处理（见文件靠前
    的 st.query_params 检查）。删除键单独放在旁边一个真正的 st.button，
    跟这个<a>标签是两个独立的DOM元素，互不干扰。
    """
    if not _fragment_alive("positions"):
        return

    if not position_items:
        st.caption("这个分类下暂时没有持仓。")
        return


    st.markdown(
        _price_flash_css()
        + "<style>"
        # 浏览器默认的 a:link/a:visited 样式（蓝色+下划线）选择器带伪类，
        # 优先级比单纯的class选择器高，必须用!important才能真正覆盖掉。
        + "a.pos-card-link, a.pos-card-link:link, a.pos-card-link:visited {"
        + "  text-decoration: none !important; color: inherit !important;"
        + "  display: block; cursor: pointer;"
        + "}"
        + "a.pos-card-link:hover { opacity: 0.85; }"
        # 删除键用type="tertiary"，图标本身默认偏小，用户反馈要大一点、位置要
        # 跟卡片内容对齐。垂直对齐交给st.columns自己的vertical_alignment="center"
        # 处理（原生机制，比猜CSS高度靠谱）。默认按钮是圆角矩形/胶囊形，用户
        # 反馈这个和"对比/搜索"图标按钮一样改成正圆——固定等宽高+50%圆角。
        + "</style>",
        unsafe_allow_html=True,
    )

    # 表头跟下面每行的列宽必须是同一套 st.columns 比例分出来的，不能自己
    # 另外拿flex div模仿列宽——之前拿固定36px去凑删除键那一列的宽度，
    # 在不同屏幕宽度下跟实际的 st.columns([9,1]) 比例对不上，表头和数据
    # 看着就没对齐。
    _head_static_col, _head_dynamic_col, _head_del_col = st.columns([5.24, 3.76, 1])
    _head_static_col.markdown(
        "<div class='fa-flex-row' style='display:flex;align-items:center;padding:4px 8px;font-size:0.75rem;color:var(--fa-muted)'>"
        "<div style='flex:2.1'>名称/代码</div>"
        "<div style='flex:1.1;text-align:center'>走势</div>"
        "</div>",
        unsafe_allow_html=True,
    )
    _head_dynamic_col.markdown(
        "<div class='fa-flex-row' style='display:flex;align-items:center;padding:4px 8px;font-size:0.75rem;color:var(--fa-muted)'>"
        "<div style='flex:1.3;text-align:right'>最新/成交额</div>"
        "<div style='flex:1;text-align:right'>涨跌幅</div>"
        "</div>",
        unsafe_allow_html=True,
    )

    # 先把所有行的数据一次性取完（不带任何渲染），再统一画出来——之前是
    # 边取数据边画一行，用户反馈"一个一个蹦出来很慢"。取数据本身的耗时省不掉
    # （网络请求），但至少不会让用户看着页面一行一行往外挤，而是等一下之后
    # 整批一起出现，观感上干脆很多。
    def _fetch_quote(item, hk_us_quotes: dict):
        """只取实时价，不碰迷你图的历史数据。

        2026-09-12改（前端审计"自选52只要等25秒、前面只有2只有价格、其余
        全是—"）：这个函数原来叫_fetch_one，把两件性质完全不同的事捆在
        一次调用里——港美股的实时价其实已经在外面用
        get_stock_realtime_futu_batch 一次性批量取回来了（实测21支0.14秒，
        根本不是瓶颈），而迷你图的日线历史是每支单独查、并且在
        data_sources._futu_call 那层串到单个常驻worker线程上排队的（实测
        21支串行12.57秒，52支就是半分钟量级）。两者共用同一个4秒deadline，
        结果就是慢的那件事把快的那件事一起拖死：deadline一到整行被丢掉，
        连同手里明明已经拿到的价格，一起渲染成"—"。

        拆成两段之后，价格这一段几乎不可能超时（数据已在内存里），迷你图
        单独走另一个deadline，取不到就先不画、后续刷新再补，不再影响价格。
        """
        item_market = item.get("market", "A")
        symbol = item["symbol"]
        if item_market in ("HK", "US"):
            wspot = hk_us_quotes.get((symbol, item_market))
            if wspot is None:
                # 批量查询里没这支（比如这次快照请求整体失败），退回单独查
                # 一次兜底——只是个别情况，不会像"每支都单独查"那样再次
                # 撞上限流。
                try:
                    wspot = get_stock_realtime(symbol, market=item_market)
                except Exception:
                    wspot = {}
        else:
            try:
                wspot = get_stock_realtime(symbol, market=item_market)
            except Exception:
                wspot = {}
        return wspot

    def _collect_rows():
        # 之前是for循环一只一只顺序取（实时价+迷你图两个接口都要等网络返回），
        # 用户反馈"持仓加载好慢"——几只股票乘以两次网络请求累加起来确实慢。
        # 沪深走BaoStock/akshare，内部各自有全局锁保证线程安全，并发提交时这
        # 部分本来就会排队，不会因为并发就变快；但港股/美股走Futu，现在走的是
        # 单一常驻worker线程+队列（见data_sources.py的_futu_call），单次查询
        # 本身只要零点几秒，并发提交多只互不阻塞。用线程池把每只股票的取数
        # 并发起来，沪深之间该排队还是排队，但沪深和港股/美股之间、以及港股/
        # 美股彼此之间不用再互相等，混合市场的持仓整体加载时间能明显缩短。
        #
        # 统一截止时间/避免线程堆积的实现细节抽到了共享的
        # _run_concurrent_with_deadline（见它的docstring——那里记录了同一个
        # 教训：per-future timeout会被排队顺序绕过，必须用整批统一的deadline）。
        #
        # 真实故障修复（2026-09-01）：这里之前是每支股票各自单独调
        # get_stock_realtime，港股/美股都走get_market_snapshot单只查询——
        # 20支股票、每3秒刷新一次，等于每30秒约200次get_market_snapshot
        # 调用，直接撞上Futu"每30秒最多60次"的限流，请求卡住不返回，页面
        # 表现为长时间转圈("自选页死机")。改成先把所有港股/美股代码一次性
        # 打包进一个get_stock_realtime_futu_batch调用，每次刷新固定只占用
        # 1次调用额度，跟持仓/自选列表里有多少支股票无关。
        hk_us_items = [
            (it["symbol"], it.get("market", "A")) for it in position_items if it.get("market", "A") in ("HK", "US")
        ]
        try:
            hk_us_quotes = get_stock_realtime_futu_batch(hk_us_items) if hk_us_items else {}
        except Exception:
            hk_us_quotes = {}

        # 第一段：价格。港美股的值已经在上面那次批量调用里了，这一段基本是
        # 内存取值；只有沪深需要真的发请求（走BaoStock/akshare各自的全局锁）。
        quote_results = _run_concurrent_with_deadline(
            position_items, lambda item: _fetch_quote(item, hk_us_quotes), timeout=4,
        )
        # 第二段：迷你图历史。单独一个deadline，超时只是这一轮没有走势图，
        # 不影响价格显示；缓存是按自然日存的（_sparkline_closes_cached），
        # 所以每次刷新都会多暖热几支，几轮之后全部补齐，属于渐进加载而不是
        # "整页卡在那里等"。给6秒而不是4秒：它已经不阻塞价格了，可以多等一会
        # 一次多补几支，减少补齐所需的刷新轮数。
        spark_results = _run_concurrent_with_deadline(
            position_items,
            lambda item: _fetch_sparkline_closes(item["symbol"], item.get("market", "A")),
            timeout=6,
        )
        rows = []
        for i, item in enumerate(position_items):
            item_market = item.get("market", "A")
            symbol = item["symbol"]
            wspot = quote_results.get(i)
            if wspot is None:
                # 并发那一轮没赶上，但批量结果里可能本来就有，直接兜底取用，
                # 不要因为调度没排上就把已经到手的价格丢掉渲染成"—"。
                wspot = hk_us_quotes.get((symbol, item_market)) or {}
            rows.append((item, item_market, symbol, wspot, spark_results.get(i) or []))
        return rows

    # 这个fragment每3秒自动刷新一次——只有真正第一次加载（session里还没有
    # 任何一次成功渲染过）才显示"加载中"，之后的静默自动刷新不再包一层
    # spinner：之前每次刷新都会先弹一下spinner再画出列表，整个列表跟着
    # 抖一下，跟_render_price_header那套"数字变了背景轻轻一闪"的丝滑感
    # 完全相反。改成只有首次展示这一遭才等得起spinner，后续刷新静默取数，
    # 取完直接原地重画，观感上就是"数字自己跳动"而不是"列表重绘"。
    if not st.session_state.get("_pos_seen_once"):
        with st.spinner("加载中..."):
            _rows_data = _collect_rows()
        st.session_state["_pos_seen_once"] = True
    else:
        _rows_data = _collect_rows()

    # 排序放在取数之后：涨跌幅要等行情回来才知道，AI评分要读 advice 表，
    # 两个排序键都不在调用方手上（2026-09-12，前端审计第9条"不能排序"）。
    # 默认顺序不动（用户自己添加自选的先后顺序本身是一种信息，不该被
    # 无条件重排）。
    if sort_mode and sort_mode != "默认":
        def _chg_pct(row):
            _wspot = row[3] or {}
            _last, _prev = _wspot.get("最新价"), _wspot.get("昨收")
            if not _last or not _prev:
                return None
            return (_last - _prev) / _prev * 100

        if sort_mode == "涨幅":
            _rows_data.sort(key=lambda r: (_chg_pct(r) is None, -(_chg_pct(r) or 0)))
        elif sort_mode == "跌幅":
            _rows_data.sort(key=lambda r: (_chg_pct(r) is None, _chg_pct(r) or 0))
        elif sort_mode == "AI评分":
            _adv_for_sort = {}
            try:
                _adv_for_sort = get_position_advice(_email)
            except Exception:
                pass
            def _score(row):
                _a = _adv_for_sort.get(row[2]) or {}
                return _a.get("score")
            _rows_data.sort(key=lambda r: (_score(r) is None, -(_score(r) or 0)))

    # AI持仓判断——只是本地SQLite读一次(不是每行都查、也不触发AI调用)，
    # 这个fragment本身每3秒会重跑，一起刷新代价很小。数据来自advisor.py
    # 每个工作日跑一次的持仓判断(holding=True的judge_stock)，不是现场生成。
    try:
        _advice_map = get_position_advice(_email)
    except Exception:
        _advice_map = {}

    for item, item_market, symbol, wspot, closes in _rows_data:
        spark_color = "#999"
        if wspot and wspot.get("最新价") and wspot.get("昨收"):
            spark_color = UP_COLOR if wspot["最新价"] >= wspot["昨收"] else DOWN_COLOR

        # 名称+走势图这部分每3秒刷新时几乎不变（迷你图数据本身缓存了好几分钟，
        # 涨跌方向短期内也很少翻转），但之前跟价格/涨跌幅拼进同一个markdown
        # 字符串——价格每次都变，导致这一整块（含SVG）每3秒都要重新生成、
        # 重新发给前端重绘，是持仓列表"感觉卡顿"的一部分原因。缓存住SVG
        # 字符串，输入不变就直接复用，减少每次刷新真正要重绘的内容量。
        spark_key = f"_pos_spark_{symbol}_{item_market}"
        spark_cache = st.session_state.get(spark_key)
        closes_tuple = tuple(closes)
        if spark_cache is not None and spark_cache[0] == closes_tuple and spark_cache[1] == spark_color:
            spark_svg = spark_cache[2]
        else:
            spark_svg = _build_sparkline_svg(closes, spark_color)
            st.session_state[spark_key] = (closes_tuple, spark_color, spark_svg)

        shares = item.get("shares") or 0
        cost_total = item.get("cost_total") or 0
        pnl_html = ""

        if wspot and wspot.get("最新价"):
            wchange = wspot["最新价"] - wspot.get("昨收", wspot["最新价"])
            wchange_pct = wchange / wspot["昨收"] * 100 if wspot.get("昨收") else 0
            color = UP_COLOR if wchange >= 0 else DOWN_COLOR

            # 场外基金/上金所现货是T-1净值(见get_stock_realtime里的兜底)，
            # 不是真实盘中报价——不能套用"3秒刷新+背景一闪"这套暗示"刚刚
            # 变化"的动画，那是在说谎；数字本身也要标个"T-1"，不然跟真实
            # 实时行情长得一模一样，用户没法分辨这支的涨跌是不是"今天"的。
            is_stale = str(wspot.get("数据源", "")).endswith("(T-1)")

            flash_key = f"_pos_last_price_{symbol}_{item_market}"
            prev = st.session_state.get(flash_key)
            st.session_state[flash_key] = wspot["最新价"]
            flash_class = ""
            if not is_stale and prev is not None and prev != wspot["最新价"]:
                flash_class = "price-flash-up" if wspot["最新价"] > prev else "price-flash-down"

            stale_tag = " <span style='font-size:0.65rem;color:var(--fa-muted)'>T-1</span>" if is_stale else ""
            price_html = (
                f"<div class='{flash_class}' style='text-align:right;border-radius:4px'>"
                f"<div style='font-weight:600;color:{color}'>{wspot['最新价']:.2f}{stale_tag}</div>"
                f"<div style='font-size:0.72rem;color:var(--fa-muted)'>{_fmt_turnover(wspot.get('成交额'))}</div>"
                f"</div>"
            )
            # 涨跌幅原来是"实色块+白字"。一屏二十来行，就是二十来个饱和色块
            # 竖着排下来，页面上最抢眼的变成了这一列色块本身，而不是数字。
            # 改成同色系的淡底+彩字：颜色照样一眼分得出涨跌，但重量轻得多，
            # 视线回到数字上。用 color-mix 把同一个色号兑淡，不另外挑一个浅色，
            # 保证以后改 theme.py 时深浅两档自动同步。
            badge_html = (
                f"<div style='text-align:right'>"
                f"<span style='background:color-mix(in srgb, {color} 11%, transparent);"
                f"color:{color};font-size:0.78rem;font-weight:600;letter-spacing:.01em;"
                f"padding:3px 8px;border-radius:5px;display:inline-block;min-width:60px;text-align:center'>"
                f"{wchange_pct:+.2f}%</span></div>"
            )

            # 真正持仓(shares>0)才算市值/浮盈——纯关注(shares=0)不显示这一行，
            # 跟原来持仓的观感保持一致，不会突然多出一堆"0股"的噪音信息。
            if shares > 0:
                market_value = shares * wspot["最新价"]
                pnl = market_value - cost_total
                pnl_pct = (pnl / cost_total * 100) if cost_total else 0
                pnl_color = UP_COLOR if pnl >= 0 else DOWN_COLOR
                pnl_html = (
                    f"<div style='text-align:right;font-size:0.72rem;margin-top:2px'>"
                    f"<span style='color:var(--fa-muted)'>{shares:g}股 · 市值{market_value:,.0f}</span> "
                    f"<span style='color:{pnl_color}'>{pnl:+,.0f}（{pnl_pct:+.1f}%）</span></div>"
                )
        else:
            price_html = "<div style='text-align:right;color:var(--fa-muted)'>—</div>"
            badge_html = ""

        # 行容器不再用 border=True。原来每一行是一张带边框的卡片，卡片里又套
        # 一个带边框的"AI持仓判断"折叠框——二十来行就是二十来个"方框套方框"，
        # 用户反馈的"看上去很冗杂"就是这么来的。改成没有边框的平铺行，行与行
        # 之间只用一条发丝线分隔（CSS 里的 .st-key-pos_row_*），信息密度更高，
        # 视觉噪声大幅下降，也更像一张真正的行情列表而不是一堆卡片。
        with st.container(key=f"pos_row_{item_market}_{symbol}"):
            # 比例是把原来单列里 名称2.1:走势1.1:价格1.3:涨跌幅1 这四段按
            # "静态(名称+走势)/动态(价格+涨跌幅)"拆成两组，再按原比例
            # 换算回外层st.columns([9,1])的尺度（9*3.2/5.5≈5.24，9*2.3/5.5≈3.76），
            # 保证拆分前后每一段的实际宽度不变，不会因为拆列导致布局跳动。
            static_col, dynamic_col, del_col = st.columns([5.24, 3.76, 1], vertical_alignment="center")
            href = (
                f"?open_symbol={urllib.parse.quote(symbol)}"
                f"&open_market={urllib.parse.quote(item_market)}"
                f"&open_name={urllib.parse.quote(item['name'])}"
                # 带上真正的来源分区，不再写死"pos"。2026-09-01把"自选"从持仓里
                # 拆成独立分区之后，这两个分区共用同一个 _render_position_rows，
                # 于是从自选点进详情、再点返回，会被送回"持仓"——去了一个自己
                # 根本没在看的分区。整页导航会重建 session，记不住来路，只能靠
                # URL 显式带过去。
                f"&open_from={urllib.parse.quote(st.session_state.get('_active_section', '持仓'))}"
                f"{_auth_qs()}"
            )
            # 名称+走势图（静态部分）和价格+涨跌幅（动态部分）拆成两个独立的
            # st.markdown调用——同一个href两边都能点，视觉上还是整行可点，
            # 但静态部分的HTML字符串在数据没变时保持不变，Streamlit的diff能
            # 跳过它不用每3秒都重绘，只有动态部分真正需要每次刷新。
            static_col.markdown(
                f"<a class='pos-card-link' href='{href}' target='_self'>"
                f"<div class='fa-flex-row' style='display:flex;align-items:center'>"
                # 颜色直接写在这个div自己身上，不靠继承父级<a>的color——之前靠
                # a.pos-card-link{{color:inherit!important}}死活压不过浏览器
                # 默认的a:link蓝色，元素自己的inline style优先级天然最高，不用
                # 再跟CSS特异性较劲。
                f"<div style='flex:2.1;font-weight:600;color:var(--fa-text);text-decoration:none'>{_esc(item['name'])}（{_esc(symbol)}）</div>"
                f"<div style='flex:1.1;display:flex;justify-content:center'>{spark_svg}</div>"
                f"</div></a>",
                unsafe_allow_html=True,
            )
            dynamic_col.markdown(
                f"<a class='pos-card-link' href='{href}' target='_self'>"
                f"<div class='fa-flex-row' style='display:flex;align-items:center'>"
                f"<div style='flex:1.3'>{price_html}</div>"
                f"<div style='flex:1'>{badge_html}</div>"
                f"</div>{pnl_html}</a>",
                unsafe_allow_html=True,
            )
            if del_col.button("", icon=":material/close:", key=f"pos_del_{symbol}", help="卖出/取消关注", type="tertiary"):
                # 不能在这里直接调_confirm_sell_dialog——这个函数(_render_position_rows)
                # 是@st.fragment(run_every=3)，弹窗打开后绑定的是当下这个fragment实例，
                # 但每3秒的自动刷新会让fragment在后台重新生成一份，弹窗还留在界面上、
                # 看着正常，可点"确认卖出"时服务端发现绑定的fragment id已经不存在了，
                # 点击被静默丢弃、什么反应都没有(2026-09-01真实复现：VPS日志能看到
                # "The fragment with id ... does not exist anymore"，前端完全没有报错
                # 提示，只是点了没用)。改成在这里只记一个"要打开哪个标的的卖出弹窗"的
                # session_state标记+st.rerun()（默认整页作用域，会跳出这个fragment），
                # 真正调用_confirm_sell_dialog的代码挪到本函数外层不会自动刷新的稳定
                # 作用域里（持仓/自选两个分区各自调用_render_position_rows之后），
                # 这样弹窗绑定的就是稳定作用域，不会被后台定时刷新顶掉。
                st.session_state["_confirm_sell_target"] = {
                    "symbol": symbol, "item": item, "market": item_market,
                    "cur_price": wspot.get("最新价") if wspot else None,
                }
                st.rerun()

            adv = _advice_map.get(symbol)
            if adv:
                adv_action = adv.get("action", "观望")
                adv_color = _ADVICE_ACTION_COLOR.get(adv_action, NEUTRAL_COLOR)
                adv_parts = _parse_advice_text(_clean_ai_markdown(adv.get("fundamental_verdict", "")))
                # 标题从"AI持仓判断：观望（2026-09-03）"缩成"观望 · 09-03"——
                # 前缀每行都一样，重复二十遍不提供任何信息；日期只留月-日。
                # 折叠框本身在 CSS 里去掉了边框和底色（见 .st-key-pos_row_ 那段），
                # 变成一行安静的可展开文字，不再是卡片里的第二个方框。
                # 2026-09-11前端审计：原来这行是"持有 · 09-04"，用户第一反应
                # 是"我持有这支"，其实说的是"AI在09-04给的评级"——主语完全
                # 反了。补上"AI："前缀消歧；再把距今天数标出来，7天前的判断
                # 跟今天的判断摆在一起而看不出新旧，是另一种误导（审计里也
                # 提到自选页显示"持有"、首页同一支显示"买入"，其实是两个
                # 时间点的判断）。超过3天的算过期，文字整体压灰。
                _adv_dt = _to_cn_dt(adv.get("created_at", ""))
                _age_days = (datetime.now(timezone(timedelta(hours=8))) - _adv_dt).days if _adv_dt else None
                if _age_days is None:
                    _age_text = ""
                elif _age_days <= 0:
                    _age_text = "（今天）"
                else:
                    _age_text = f"（{_age_days}天前）"
                _label = f"AI：{adv_action}{_age_text}"
                if _age_days is not None and _age_days >= 3:
                    _label += " · 已过期"
                with st.expander(_label):
                    st.markdown(
                        f"<span style='background:{adv_color};color:#fff;border-radius:4px;padding:1px 8px;"
                        f"font-size:0.8rem;font-weight:700'>{_esc(adv_action)}</span> "
                        f"<span style='font-size:0.75rem;color:var(--fa-muted)'>置信度：{_esc(adv_parts.get('置信度','—'))}</span>",
                        unsafe_allow_html=True,
                    )
                    st.markdown(_esc(adv_parts.get("理由", "")))
                    for sec in ("基本面", "技术面", "价格位置"):
                        if adv_parts.get(sec):
                            st.markdown(_labeled_line(sec, adv_parts[sec]), unsafe_allow_html=True)
                    # 2026-09-12（前端审计"排行榜说买入、自选里同一只标观望"）：
                    # 这两个结论来自两条不同的判断链路，回答的根本不是同一个
                    # 问题——这里读的是 source='position'，问的是"已经持有的
                    # 这笔仓位要不要继续拿/加仓"（prompt里那段_HOLDING_ADDENDUM）；
                    # 排行榜读的是 source='screen'，问的是"现在值不值得新建仓"。
                    # 同一支票"值得新建仓"和"已有仓位先别加"完全可以同时成立。
                    # 所以不该像审计建议的那样把两处强行读同一个字段——那是把
                    # 两个不同的判断压成一个、真的丢信息；缺的是界面没讲清楚
                    # 各自在回答什么，补一句说明即可。
                    st.caption(
                        "这条回答的是「已持有的仓位要不要继续拿」。排行榜里同一支票的结论"
                        "回答的是「现在值不值得新建仓」，是另一次独立判断，两者不一致是正常的。"
                    )


def _backfill_due_reviews(email: str):
    """把到期（满7天）该补录回看价格的分析记录补上——从"历史回看"侧边栏
    expander里抽出来的独立函数，"回看"主页面和侧边栏摘要都要用同一套
    节流逻辑，不能各自维护一份容易跑偏。

    节流原因见调用方：get_due_for_review+并发get_stock_realtime这一整套
    实测过Futu需要重连时接近10秒，而"7天后补录"这个需求是天级颗粒度的，
    没必要每次切页面都重新触发一遍网络请求，同一个会话内每
    _REVIEW_RECHECK_INTERVAL 秒最多真正跑一次。

    2026-08-30修复：deadline原来是3秒，比上面这段自己docstring里写的
    "接近10秒"还短，等于这条回填路径实测经常连一个都补不上——既然
    _REVIEW_RECHECK_INTERVAL已经把真正发请求的频率控制在每60秒最多一次，
    这里不需要为了"页面快0.几秒"牺牲"功能根本跑不起来"，调到10秒对齐
    实测延迟。
    """
    _REVIEW_RECHECK_INTERVAL = 60
    _review_checked_at = st.session_state.get("_review_checked_at", 0.0)
    if time.time() - _review_checked_at <= _REVIEW_RECHECK_INTERVAL:
        return
    due = get_due_for_review(email, min_age_days=7)

    def _fetch_review_price(item):
        try:
            spot = get_stock_realtime(item["symbol"], market=item.get("market", "A"))
            return item, spot
        except Exception:
            return item, None

    if due:
        review_results = _run_concurrent_with_deadline(due, _fetch_review_price, timeout=10)
        for item, spot in review_results.values():
            # 价格跟入场价一模一样大概率是当天没开盘取回的还是同一个交易日
            # 收盘价（周末/节假日回填），不是真的"事后零涨跌"——留着不记，
            # 下次真实交易日再试，跟advisor.py的_backfill_due_advice同一个
            # 修复思路，不能当0%收益记进一致率统计。
            if spot and spot.get("最新价") and float(spot["最新价"]) != item.get("price_at_analysis"):
                try:
                    record_review(item["id"], float(spot["最新价"]))
                except Exception:
                    continue
    st.session_state["_review_checked_at"] = time.time()


def _render_accuracy_dashboard(email: str):
    """"回看"页——把原来塞在侧边栏折叠面板里的方向一致率统计，提升成
    主内容区的独立页面。数据和统计口径完全复用tracker.py已有的
    get_accuracy_stats/get_accuracy_trend（没有新造轮子），新增的是这个
    页面本身的呈现方式：把"一个孤零零的百分比"变成一个真正像"过往战绩
    公开可查"的仪表盘——这是finance-agent"不做黑箱荐股，拿数据说话"这个
    产品定位最该被看见的地方，不该被折叠面板埋起来。

    新增的日历热力图故意不用红涨绿跌那套配色（UP_COLOR/DOWN_COLOR在这个
    App里全局代表"价格涨/跌"，这里如果借用会让用户以为热力图在讲价格
    涨跌，而这里讲的是完全不同的"预测准不准"）。改用同一个UP_COLOR的
    单一色相、只调深浅（浅→深表示当天有判断且一致率从低到高），跟品牌色
    保持同源但语义不冲突。
    """
    if not st.session_state.get("logged_in"):
        st.write("")
        _, mid_empty, _ = st.columns([1, 2, 1])
        with mid_empty:
            st.markdown(
                "<div style='text-align:center;color:var(--fa-muted);padding:40px 0 10px'>"
                "回看是个人功能，需要登录后使用<br>"
                "<span style='font-size:0.82rem'>行情/详情页/AI分析等其它功能无需登录即可查看</span>"
                "</div>",
                unsafe_allow_html=True,
            )
            if st.button("登录 / 注册", use_container_width=True, key="_review_page_login_btn"):
                st.session_state["guest_mode"] = False
                st.rerun()
        return

    _backfill_due_reviews(email)

    stats = get_accuracy_stats(email)
    if stats["总数"] == 0:
        st.caption("还没有满7天可回看的记录")
        return

    # ── 头条卡片：先给一句人话结论，细节留到下面 ──────────────────────────
    # 用户反馈"一致率""滑动窗口"这些词看着费劲——不是不懂百分比，是这套
    # 统计学黑话本身就没在"讲人话"。改成"先说结论、再摆证据"的顺序：
    # 大字号的总体准确率 + 一句自动生成的人话点评（哪个方向/哪个市场判断
    # 更准），剩下细分数字降级成小字辅助信息，不再是一排并列的st.metric
    # 让人自己去比大小。
    _market_label = {"A": "沪深", "HK": "港股", "US": "美股"}
    _dir_bull = stats.get("按方向", {}).get("偏多", {})
    _dir_bear = stats.get("按方向", {}).get("偏空", {})
    _mkt_stats = stats.get("按市场", {})

    _insight = ""
    if _dir_bull.get("总数", 0) >= 3 and _dir_bear.get("总数", 0) >= 3:
        _diff = _dir_bull["一致率"] - _dir_bear["一致率"]
        if abs(_diff) >= 10:
            _better = "看涨" if _diff > 0 else "看跌"
            _worse = "看跌" if _diff > 0 else "看涨"
            _insight = f"AI「{_better}」的判断比「{_worse}」更准一些。"
    if not _insight and len(_mkt_stats) >= 2:
        _qualified = {m: s for m, s in _mkt_stats.items() if s["总数"] >= 3}
        if len(_qualified) >= 2:
            _best_m = max(_qualified, key=lambda m: _qualified[m]["一致率"])
            _worst_m = min(_qualified, key=lambda m: _qualified[m]["一致率"])
            if _best_m != _worst_m and _qualified[_best_m]["一致率"] - _qualified[_worst_m]["一致率"] >= 10:
                _insight = (
                    f"在「{_market_label.get(_best_m, _best_m)}」判断得最准，"
                    f"「{_market_label.get(_worst_m, _worst_m)}」相对差一些。"
                )
    if not _insight:
        _insight = "各个方向/市场的准确率暂时看不出明显差别，样本还不算多。"

    with st.container(border=True):
        st.markdown(
            f"<div style='text-align:center;padding:8px 0'>"
            f"<div style='font-size:0.85rem;color:var(--fa-muted)'>AI说对的比例</div>"
            f"<div style='font-size:3rem;font-weight:800;color:{UP_COLOR};line-height:1.1'>{stats['一致率']:.0f}%</div>"
            f"<div style='font-size:0.85rem;color:var(--fa-muted)'>"
            f"过去 {stats['总数']} 次「涨/跌」判断里，对了 {stats['一致数']} 次</div>"
            f"</div>",
            unsafe_allow_html=True,
        )
        st.markdown(
            f"<div style='text-align:center;font-size:0.92rem;margin-top:4px'>{_insight}</div>",
            unsafe_allow_html=True,
        )

    st.write("")
    st.markdown("<div style='color:var(--fa-muted);font-size:0.85rem'>细分数据</div>", unsafe_allow_html=True)
    d1, d2, d3, d4, d5 = st.columns(5)
    _pairs = [
        (d1, "看涨判断", _dir_bull),
        (d2, "看跌判断", _dir_bear),
    ] + [
        (col, _market_label.get(m, m), _mkt_stats.get(m, {}))
        for col, m in zip([d3, d4, d5], ["A", "HK", "US"])
    ]
    for _col, _label, _s in _pairs:
        with _col:
            if _s.get("总数", 0) >= 3:
                st.metric(_label, f"{_s['一致率']:.0f}%", help=f"{_s['总数']} 次判断")
            else:
                st.metric(_label, "还太少", help=f"目前只有 {_s.get('总数', 0)} 次，攒够3次才统计")

    st.divider()

    _trend = get_accuracy_trend(email, window=5)
    if _trend:
        st.markdown("**最近是变准了还是变不准了**")
        _trend_df = pd.DataFrame(_trend).set_index("日期")[["一致率"]]
        st.line_chart(_trend_df, height=200)

    st.markdown("**每天判断得准不准**")
    _daily = get_daily_accuracy(email, days=91)
    if not _daily:
        st.caption("暂无足够的每日数据。")
    else:
        _by_date = {d["日期"]: d for d in _daily}
        _today = datetime.now(timezone.utc).date()
        _start = _today - timedelta(days=90)
        _start -= timedelta(days=_start.weekday())  # 对齐到那一周的周一，格子排布整齐

        _dates, _weeks_idx, _weekdays, _rates, _hover = [], [], [], [], []
        _cursor = _start
        _week_i = 0
        while _cursor <= _today:
            entry = _by_date.get(_cursor.isoformat())
            _dates.append(_cursor)
            _weeks_idx.append(_week_i)
            _weekdays.append(_cursor.weekday())
            _rates.append(entry["一致率"] if entry else None)
            _hover.append(
                f"{_cursor.isoformat()}<br>{entry['一致数']}/{entry['总数']} 一致（{entry['一致率']:.0f}%）"
                if entry else f"{_cursor.isoformat()}<br>无记录"
            )
            if _cursor.weekday() == 6:
                _week_i += 1
            _cursor += timedelta(days=1)

        import plotly.graph_objects as go

        _z = [[None] * (_week_i + 1) for _ in range(7)]
        _text = [[""] * (_week_i + 1) for _ in range(7)]
        for wi, wd, rate, hv in zip(_weeks_idx, _weekdays, _rates, _hover):
            _z[wd][wi] = rate if rate is not None else -1
            _text[wd][wi] = hv

        _fig = go.Figure(
            go.Heatmap(
                z=_z, text=_text, hoverinfo="text",
                colorscale=[
                    [0.0, "#eee"], [0.001, "#fbe1df"], [0.5, UP_COLOR], [1.0, "#7a0f0f"],
                ],
                zmin=-1, zmax=100, showscale=False,
                xgap=3, ygap=3,
            )
        )
        _fig.update_layout(
            height=170, margin=dict(l=30, r=10, t=10, b=10),
            yaxis=dict(
                tickmode="array", tickvals=[0, 2, 4, 6], ticktext=["一", "三", "五", "日"],
                autorange="reversed", showgrid=False,
            ),
            xaxis=dict(showgrid=False, showticklabels=False),
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(_fig, use_container_width=True, config=_PLOTLY_CONFIG, key="_accuracy_calendar_heatmap")

    st.divider()
    st.markdown("**最近这些判断，一条条看**")
    # 之前这里只列"当时X → 现在Y"，要用户自己心算"这算涨了还是跌了、
    # 跟判断对不对得上"——现在直接算好、直接说结论，不用用户再动脑子。
    _verdict_label = {"偏多": "看涨", "偏空": "看跌", "中性": "没明确方向"}
    history = get_history(email, limit=20)
    for h in history:
        verdict_color = {"偏多": UP_COLOR, "偏空": DOWN_COLOR, "中性": NEUTRAL_COLOR}.get(h["verdict"], NEUTRAL_COLOR)
        verdict_text = _verdict_label.get(h["verdict"], h["verdict"])
        name_line = f"{_esc(h.get('name') or h['symbol'])}（{_esc(h['symbol'])}）"

        if h.get("review_price") and h["verdict"] != "中性":
            went_up = h["review_price"] > h["price_at_analysis"]
            correct = (h["verdict"] == "偏多" and went_up) or (h["verdict"] == "偏空" and not went_up)
            result_badge = (
                f"<span style='color:{UP_COLOR};font-weight:600'>✓ 说对了</span>" if correct
                else f"<span style='color:{DOWN_COLOR};font-weight:600'>✗ 说反了</span>"
            )
            detail = f"当时 {h['price_at_analysis']:.2f} → 一周后 {h['review_price']:.2f}"
        elif h.get("review_price"):
            result_badge = "<span style='color:var(--fa-muted)'>不算方向判断，不参与对错统计</span>"
            detail = f"当时 {h['price_at_analysis']:.2f} → 一周后 {h['review_price']:.2f}"
        else:
            result_badge = "<span style='color:var(--fa-muted)'>还没到一周，等着看结果</span>"
            detail = f"当时 {h['price_at_analysis']:.2f}"

        with st.container(border=True):
            st.markdown(
                f"<div style='display:flex;justify-content:space-between;align-items:center;font-size:0.85rem'>"
                f"<span>{name_line}　"
                f"<span style='color:{verdict_color}'>AI说：{verdict_text}</span></span>"
                f"<span style='color:var(--fa-muted);font-size:0.78rem'>{h['created_at'][:10]}</span>"
                f"</div>"
                f"<div style='font-size:0.8rem;margin-top:4px;display:flex;justify-content:space-between'>"
                f"<span style='color:var(--fa-muted)'>{detail}</span>{result_badge}"
                f"</div>",
                unsafe_allow_html=True,
            )


@st.dialog("卖出确认")
def _confirm_sell_dialog(email: str, item: dict, market: str, cur_price: float | None):
    """shares=0（纯关注，没有真实持仓）走原来的简单确认删除；shares>0是真的
    在卖持仓，要展示股数/均价/现价/浮盈，让用户输入卖出股数+成交金额——
    默认卖出全部、金额按现价估算，用户可以改成真实成交价。
    """
    symbol, name = item["symbol"], item["name"]
    shares = item.get("shares") or 0
    cost_total = item.get("cost_total") or 0

    if shares <= 0:
        st.write(f"确定要取消关注「{name}」（{symbol}）吗？")
        dc1, dc2 = st.columns(2)
        if dc1.button("确认", type="primary", use_container_width=True):
            delete_position(email, symbol)
            st.session_state.pop("_confirm_sell_target", None)
            st.rerun()
        if dc2.button("取消", use_container_width=True):
            st.session_state.pop("_confirm_sell_target", None)
            st.rerun()
        return

    avg_cost = cost_total / shares
    st.write(f"**{name}**（{symbol}·{market}）")
    st.caption(
        f"持仓 {shares:g} 股 · 均价 {avg_cost:.2f} · 现价 "
        f"{f'{cur_price:.2f}' if cur_price else '—'}"
    )
    if cur_price:
        pnl = (cur_price - avg_cost) * shares
        pnl_color = UP_COLOR if pnl >= 0 else DOWN_COLOR
        st.markdown(f"浮动盈亏：<span style='color:{pnl_color}'>{pnl:+,.2f}</span>", unsafe_allow_html=True)

    sell_shares = st.number_input("卖出股数", min_value=0.0, max_value=float(shares), value=float(shares), step=1.0, key=f"_sell_shares_{symbol}")
    default_amount = sell_shares * cur_price if cur_price else sell_shares * avg_cost
    sell_amount = st.number_input(
        "成交金额（默认按现价估算，可改成真实成交价×股数）", min_value=0.0,
        value=float(default_amount), step=1.0, key=f"_sell_amount_{symbol}",
    )
    dc1, dc2 = st.columns(2)
    if dc1.button("确认卖出", type="primary", use_container_width=True):
        if sell_shares <= 0:
            st.error("卖出股数要大于0。")
        else:
            reduce_position(email, symbol, sell_shares, sell_amount)
            st.session_state.pop("_confirm_sell_target", None)
            st.rerun()
    if dc2.button("取消", use_container_width=True):
        st.session_state.pop("_confirm_sell_target", None)
        st.rerun()


def _resolve_confirmed_symbol(email: str, q: str, market_code: str) -> dict | None:
    """两阶段添加持仓的第一阶段：只做"这个代码/名字真实存在且能拿到行情"的
    确认，不落库。返回值直接存进session_state供第二阶段（填股数/金额）用，
    避免像老版一步到位那样——多市场候选分支rerun一次就把已经填的股数/金额
    输入框冲掉。成功才记一笔搜索历史（失败的搜索没必要占历史记录的位置）。
    """
    q = q.strip()
    if not q:
        return None
    add_symbol = _resolve_add_symbol(q, market_code)
    if not add_symbol:
        st.error(f"没查到「{q}」的行情——检查一下代码对不对，或者这家公司没上市（比如私营公司本来就没有股票代码）。")
        return None
    # check_stock_valid 只覆盖 沪深（内部走 BaoStock，港美股没有等价数据源）——
    # 能提前区分"代码格式对但公司已退市"和"数据源临时故障"这两种情况，
    # 不再一律甩给用户一句含糊的"检查一下代码对不对"。
    if market_code == "A":
        valid, info = check_stock_valid(add_symbol)
        # "没有找到代码"不直接拒绝——BaoStock的基础信息表对场内基金覆盖不全
        # （比如510300沪深300ETF查不到基础信息，但真实行情是有的），这种情况
        # 交给下面的get_stock_realtime做终审：查得到真实价格就认，查不到才
        # 真的算失败。只有"已退市"/"不是个股也不是基金"这两种明确结论才在
        # 这里直接拒绝，不用再往下查。
        if not valid and "没有找到代码" not in info:
            st.error(info)
            return None
    try:
        add_spot = get_stock_realtime(add_symbol, market=market_code)
    except Exception:
        add_spot = {}
    if not add_spot or not add_spot.get("最新价"):
        if market_code == "A":
            # 代码本身已经过 check_stock_valid 确认真实存在且在市，这里查不到
            # 行情就真的是数据源临时故障，不是代码错误，提示要区分开。
            st.error(f"「{q}」的行情暂时获取不到（数据源可能临时抖动），请稍后重试。")
        else:
            st.error(f"没查到「{q}」的行情——检查一下代码对不对，或者这家公司没上市（比如私营公司本来就没有股票代码）。")
        return None
    add_search_history(email, q, market_code)
    return {
        "symbol": add_symbol, "market": market_code,
        "name": add_spot.get("名称", add_symbol), "price": add_spot["最新价"],
    }


@st.dialog("搜索")
def _show_stock_search_dialog(email: str):
    """纯搜索——找到标的直接跳详情页，不问股数/金额，跟"添加持仓"（+号）是
    两个独立入口：这个只看行情，添加持仓才是真正记一笔仓位。跟历史记录那条
    "点了直接跳详情页"是同一个模式。
    """
    query = st.text_input("代码或名称（如 600519 / 腾讯 / 特斯拉）", key="_pos_search_query_dialog")
    candidates = st.session_state.get("_pos_search_candidates")

    if st.button("搜索", type="primary", use_container_width=True, key="_pos_search_btn_dialog") and query:
        cands = detect_symbol_candidates(query)
        if not cands:
            st.error(f"没查到「{query}」——试试直接输代码，或者换个更常见的名称。")
        elif len(cands) == 1:
            sym = _resolve_add_symbol(query, cands[0]["market"])
            if sym:
                add_search_history(email, query, cands[0]["market"])
                st.session_state["_detail_symbol"] = sym
                st.session_state["_detail_market"] = cands[0]["market"]
                st.session_state["_detail_name"] = query
                st.session_state.pop("_pos_search_candidates", None)
                st.rerun()
            else:
                st.error(f"「{query}」查不到行情。")
        else:
            st.session_state["_pos_search_candidates"] = cands
            st.session_state["_pos_search_query"] = query
            st.rerun(scope="fragment")

    if candidates:
        cq = st.session_state.get("_pos_search_query", "")
        st.info(f"「{cq}」在多个市场都有上市，选一个：")
        cand_cols = st.columns(len(candidates))
        for ccol, c in zip(cand_cols, candidates):
            if ccol.button(
                f"{c['market_label']}（{c['symbol']}）", key=f"_pos_search_cand_{c['market']}_{c['symbol']}",
                use_container_width=True, config=_PLOTLY_CONFIG,
            ):
                sym = _resolve_add_symbol(cq, c["market"])
                if sym:
                    add_search_history(email, cq, c["market"])
                    st.session_state["_detail_symbol"] = sym
                    st.session_state["_detail_market"] = c["market"]
                    st.session_state["_detail_name"] = cq
                    st.session_state.pop("_pos_search_candidates", None)
                    st.session_state.pop("_pos_search_query", None)
                    st.rerun()

    history = get_search_history(email, limit=10)
    if history:
        st.divider()
        st.caption("最近搜索")
        _hist_market_label = {"A": "沪深", "HK": "港股", "US": "美股"}
        for h in history:
            row_label = f"{h['query']}（{_hist_market_label.get(h['market'], h['market'])}）"
            if st.button(row_label, key=f"_pos_search_hist_{h['id']}", use_container_width=True):
                sym = _resolve_add_symbol(h["query"], h["market"])
                if sym:
                    st.session_state["_detail_symbol"] = sym
                    st.session_state["_detail_market"] = h["market"]
                    st.session_state["_detail_name"] = h["query"]
                    st.rerun()
                else:
                    st.error(f"没查到「{h['query']}」的行情。")


@st.dialog("添加自选")
def _show_add_watchlist_dialog(email: str):
    """自选专用的添加入口：找到标的就直接加进自选，不问股数和金额。

    2026-09-04用户反馈"我加自选股为啥给我跳出来持仓的界面，就直接加到自选股
    里面去好了"。之前这个加号跟持仓页共用 _show_add_position_dialog，弹出来
    标题写着"添加持仓"、要填买入金额和股数——可自选的定义就是"只关注、不持仓"，
    让用户为了加一个关注去面对一张持仓表单，本身就是把两件事搞混了。虽然那张
    表单在两个数字都留空时会退化成加自选，但那是个要用户自己发现的隐藏行为，
    不是入口该有的样子。

    落库走 add_watch_only（shares=0），跟持仓表是同一张表的两种状态，
    自选分区和持仓分区各自按 shares 是否大于0 过滤，不需要新建表。
    """
    query = st.text_input("代码或名称（如 600519 / 腾讯 / 特斯拉）", key="_wl_add_query")
    candidates = st.session_state.get("_wl_add_candidates")

    def _add(sym_info: dict):
        add_watch_only(email, sym_info["symbol"], sym_info["name"], market=sym_info["market"])
        st.session_state.pop("_wl_add_candidates", None)
        st.session_state.pop("_wl_add_query_text", None)
        st.rerun()

    if st.button("添加", type="primary", use_container_width=True, key="_wl_add_btn") and query:
        cands = detect_symbol_candidates(query)
        if not cands:
            st.error(f"没查到「{query}」——试试直接输代码，或者换个更常见的名称。")
        elif len(cands) == 1:
            info = _resolve_confirmed_symbol(email, query, cands[0]["market"])
            if info:
                _add(info)
        else:
            st.session_state["_wl_add_candidates"] = cands
            st.session_state["_wl_add_query_text"] = query
            st.rerun(scope="fragment")

    if candidates:
        cq = st.session_state.get("_wl_add_query_text", "")
        st.info(f"「{cq}」在多个市场都有上市，选一个：")
        cand_cols = st.columns(len(candidates))
        for ccol, c in zip(cand_cols, candidates):
            if ccol.button(f"{c['market_label']}（{c['symbol']}）",
                           key=f"_wl_add_cand_{c['market']}_{c['symbol']}", use_container_width=True):
                info = _resolve_confirmed_symbol(email, cq, c["market"])
                if info:
                    _add(info)


@st.dialog("添加持仓")
def _show_add_position_dialog(email: str):
    confirmed = st.session_state.get("_pos_add_confirmed")

    # 第二阶段：标的已确认，填股数/金额（不填股数=只关注不持仓）
    if confirmed:
        st.write(f"**{confirmed['name']}**（{confirmed['symbol']}·{confirmed['market']}） 现价 {confirmed['price']:.2f}")
        # 金额的币种是标的所在市场的原始币种，不是人民币——HK股买入金额是
        # 港币、US股是美元，upsert_position存的cost_total也是原始币种（跟
        # positions表的currency字段一致）。之前这里无条件写"¥"，港美股用户
        # 照着标签心算成人民币填进去，会把成本按错误币种存进去，均价/浮盈
        # 全部跟着错且没法自动发现——这是真实的资金核算bug，不是措辞问题。
        _CURRENCY_LABEL = {"A": "¥", "HK": "HK$", "US": "US$"}
        cur_label = _CURRENCY_LABEL.get(confirmed["market"], "¥")

        # 真实故障记录（2026-08-25）：原来的实现是两个number_input在表单外，
        # 靠on_change互相同步（改股数自动算金额、反之亦然），"确认添加"是
        # 普通按钮。生产环境实测踩到过一次：用户填了股数、肉眼看到金额也
        # 跟着换算出来了，点确认后却按shares<=0落到了add_watch_only()分支，
        # 数据库里存进去的是shares=0/cost_total=0——按Streamlit的组件模型，
        # 普通按钮点击和旁边number_input失焦提交是两条独立的前端事件，输入
        # 又快又紧跟着点确认时，按钮这次rerun携带的可能还是上一次的旧值，
        # 界面上"看起来算对了"不代表这一次rerun里Python这边真的拿到了新值。
        # 改用st.form()：表单内部所有输入框的当前值，只在点提交按钮那一刻
        # 一次性打包发送，不再有"哪个先到"的时序竞争。代价是表单内部件不支持
        # on_change实时互算，所以放弃"边打字边看到另一个框跟着变"这个效果，
        # 换成提交后台由代码统一按"填了哪个就用哪个算另一个"来处理。
        with st.form("_pos_add_form", border=False):
            st.caption("股数、成交均价、金额可填任意两项；只填一项时按当前报价估算。")
            amount = st.number_input(
                f"买入金额（{cur_label}）", min_value=0.0, value=0.0, step=100.0, key="_pos_add_amount",
            )
            shares = st.number_input(
                "股数", min_value=0, value=0, step=1, key="_pos_add_shares",
                help="股票按整数股记录；港股请按券商显示的每手股数填写。",
            )
            cost_price = st.number_input(
                f"成交均价（{cur_label}）", min_value=0.0, value=0.0,
                step=0.01, format="%.4f", key="_pos_add_cost_price",
                help="已成交的仓位请填写实际成交均价，浮盈亏会以它为成本计算。",
            )
            submitted = st.form_submit_button("确认添加", type="primary", use_container_width=True)

        if submitted:
            # 用用户明确输入的成交均价优先；没有成交价才以当前报价估算。
            # 这一步只做确定性算术，不交给模型，避免成本价/浮盈亏被自由文本改写。
            basis_price = cost_price if cost_price > 0 else confirmed["price"]
            if shares <= 0 and amount > 0:
                shares = max(1, round(amount / basis_price))
            elif amount <= 0 and shares > 0:
                amount = round(shares * basis_price, 2)
            elif shares > 0 and cost_price > 0:
                amount = round(shares * cost_price, 2)

            if shares > 0:
                upsert_position(email, confirmed["symbol"], confirmed["name"], confirmed["market"], shares, amount)
                st.session_state.pop("_pos_add_confirmed", None)
                st.rerun()
            elif cost_price > 0:
                # 成交均价本身不是仓位。以前会静默退化为“只关注”，用户以为
                # 已录入成本、实际却没有任何持仓，之后浮盈亏会一直不对。
                st.error("仅填写成交均价还不能建立持仓；请再填写股数或买入金额。")
            else:
                add_watch_only(email, confirmed["symbol"], confirmed["name"], market=confirmed["market"])
                st.session_state.pop("_pos_add_confirmed", None)
                st.rerun()
        if st.button("返回重新搜索", use_container_width=True):
            # 不调st.rerun()——dialog函数本身是@st.fragment，按钮点击已经会
            # 触发它自己重跑，弹窗留在原地。之前这里调了st.rerun()，会触发
            # 全脚本重跑，dialog判定为"这轮脚本没再调用打开它的那行代码"就
            # 直接关掉了，表现成"点了下一步后窗口突然消失，得重新点放大镜
            # 才看到填股数的第二步"——这是Streamlit官方文档明确写的行为
            # （st.dialog继承st.fragment的重跑范围，st.rerun()会跳出fragment
            # 范围触发整页重跑，从而关闭弹窗）。
            st.session_state.pop("_pos_add_confirmed", None)
            st.rerun(scope="fragment")
        return

    # 第一阶段：搜索定标的
    add_query = st.text_input("代码或名称（如 600519 / 腾讯 / 特斯拉）", key="_pos_add_query_dialog")

    if st.button("下一步", type="primary", use_container_width=True, key="_pos_add_btn_dialog") and add_query:
        # 不再让用户先选市场——大多数公司名字只在一个市场上市，自动判断就够了
        # （比如"苹果"只有美股）。只有像"阿里巴巴"这种港股美股都有的名字，
        # 才需要用户自己选，见下面的候选按钮。
        candidates = detect_symbol_candidates(add_query)
        if not candidates:
            st.error(f"没查到「{add_query}」——试试直接输代码，或者换个更常见的名称。")
        elif len(candidates) == 1:
            confirmed = _resolve_confirmed_symbol(email, add_query, candidates[0]["market"])
            if confirmed:
                st.session_state["_pos_add_confirmed"] = confirmed
                # scope="fragment"：只重跑这个dialog本身，弹窗不关，直接从
                # "搜索"无缝切到"填股数"——不能用默认的st.rerun()（=整页重跑），
                # 那会把弹窗关掉，见下面"返回重新搜索"按钮那条注释的详细说明。
                st.rerun(scope="fragment")
        else:
            st.session_state["_pos_add_candidates"] = candidates
            st.session_state["_pos_add_candidates_query"] = add_query

    if st.session_state.get("_pos_add_candidates"):
        cands = st.session_state["_pos_add_candidates"]
        cq = st.session_state.get("_pos_add_candidates_query", "")
        st.info(f"「{cq}」在多个市场都有上市，选一个：")
        cand_cols = st.columns(len(cands))
        for ccol, c in zip(cand_cols, cands):
            if ccol.button(
                f"{c['market_label']}（{c['symbol']}）", key=f"_pos_cand_{c['market']}_{c['symbol']}",
                use_container_width=True, config=_PLOTLY_CONFIG,
            ):
                confirmed = _resolve_confirmed_symbol(email, cq, c["market"])
                if confirmed:
                    st.session_state.pop("_pos_add_candidates", None)
                    st.session_state.pop("_pos_add_candidates_query", None)
                    st.session_state["_pos_add_confirmed"] = confirmed
                    st.rerun(scope="fragment")

    history = get_search_history(email, limit=10)
    if history:
        st.divider()
        st.caption("最近搜索")
        _hist_market_label = {"A": "沪深", "HK": "港股", "US": "美股"}
        for h in history:
            row_label = f"{h['query']}（{_hist_market_label.get(h['market'], h['market'])}）"
            # 点历史记录直接跳去那只股票的详情页，不是再加一遍持仓——
            # 用户反馈"再加"这个按钮没必要，点了就想直接看那只股票。
            if st.button(row_label, key=f"_pos_hist_open_{h['id']}", use_container_width=True):
                sym = _resolve_add_symbol(h["query"], h["market"])
                if sym:
                    _open_detail_route(sym, h["market"], h["query"], "持仓")
                else:
                    st.error(f"没查到「{h['query']}」的行情。")


@st.dialog("持仓对比")
def _show_compare_dialog(positions: list):
    """build_multi_comparison（charts.py）之前写好了但一直没接界面——这里补上
    唯一缺的入口：勾选几支持仓，起点归一化到100画在一张图上，直接看
    "这段时间谁涨得多"，跟单只详情页里的"对比大盘"是同一套归一化思路，
    只是不限定跟大盘比，持仓互相之间也能比。

    生成结果存进 session_state 并记下当时的选股+区间参数——多选框/区间单选
    在 st.dialog 里改动都会触发这个函数重新整个跑一遍，如果不记参数直接显示
    上次的图，选项已经变了图却没跟着变，会让人以为点了什么但没生效；比对
    参数不一致就不显示旧图，逼用户重新点一次"生成对比图"。
    """
    if len(positions) < 2:
        st.caption("持仓至少2只才能对比")
        return

    options = {f"{w['name']}（{w['symbol']}）": w for w in positions}
    labels = list(options.keys())
    picked_labels = st.multiselect(
        "对比标的", labels, default=labels[: min(3, len(labels))], key="_pos_compare_pick",
    )
    period_label = st.radio(
        "区间", ["近1月", "近3月", "近6月", "近1年"], index=1, horizontal=True, key="_pos_compare_period",
    )
    period_days = {"近1月": 30, "近3月": 90, "近6月": 180, "近1年": 365}[period_label]

    if len(picked_labels) < 2:
        st.caption("至少选2只才能对比。")
        return
    if len(picked_labels) > 6:
        st.caption("最多选6只，太多线挤在一起反而看不清。")
        return

    if st.button("生成对比图", type="primary", use_container_width=True, key="_pos_compare_go"):
        end = cn_now().strftime("%Y%m%d")
        start = (cn_now() - timedelta(days=period_days + 10)).strftime("%Y%m%d")

        def _fetch(label):
            item = options[label]
            try:
                hist = get_stock_history(item["symbol"], start, end, market=item.get("market", "A"))
            except Exception:
                hist = None
            return label, hist

        with st.spinner("加载行情..."):
            # 每只标的都是一次独立的网络请求，并发起来跟持仓列表
            # (_render_position_rows) 是同一个道理，不用互相等。原来是
            # with ThreadPoolExecutor() as ex: ex.map(...)——退出with块时会
            # 等所有线程真正跑完才返回，没有截止时间，某一只标的的数据源
            # 卡得久，这次"生成对比图"点击就跟着卡多久。改用共享的
            # _run_concurrent_with_deadline给整批一个统一截止时间，超时的
            # 那几只直接跳过不算，不会拖累这次点击的响应时间。这是手动点击
            # 触发的一次性操作（不是自动刷新循环），给的deadline比持仓
            # 列表/回看补录那两处更宽松一些。
            fetch_map = _run_concurrent_with_deadline(picked_labels, _fetch, timeout=15)
            results = list(fetch_map.values())

        hist_by_name = {label: hist for label, hist in results if hist is not None and not hist.empty}
        if len(hist_by_name) < 2:
            st.error("至少要有2只成功取到行情才能对比，换一批试试，或者稍后重试。")
        else:
            st.session_state["_pos_compare_result"] = {
                "params": (tuple(picked_labels), period_label),
                "hist_by_name": hist_by_name,
            }

    cached = st.session_state.get("_pos_compare_result")
    if cached and cached["params"] == (tuple(picked_labels), period_label):
        st.plotly_chart(build_multi_comparison(cached["hist_by_name"]), use_container_width=True, config=_PLOTLY_CONFIG)


# 休市公告放在页面主体渲染之前：st.dialog 是模态弹窗，先弹出来再渲染下面的
# 内容，用户一进站就看到，不用等整页数据加载完。放在详情页分支之前也意味着
# 从卡片点进详情页时同样会提示——休市信息跟当前看哪一页无关。
_maybe_show_closure_notice()

_page_slot = st.empty()

if st.session_state.get("_detail_symbol"):
    with _page_slot.container():
        # 详情页要拉实时行情+K线历史，是全站最慢的一跳，给个加载提示，
        # 避免点完卡片之后好几秒白屏、让人以为没点上。
        with st.spinner("加载行情…"):
            _render_stock_detail(
                st.session_state["_detail_symbol"],
                st.session_state.get("_detail_market", "A"),
                st.session_state.get("_detail_name", st.session_state["_detail_symbol"]),
            )
elif st.session_state.get("_index_detail_code"):
    with _page_slot.container():
        _render_index_detail(
            st.session_state.get("_index_detail_name", ""),
            st.session_state["_index_detail_code"],
            st.session_state.get("_index_detail_market", "A"),
        )
elif st.session_state.get("_sector_detail_name"):
    with _page_slot.container():
        _render_sector_detail(
            st.session_state["_sector_detail_name"],
            st.session_state.get("_sector_detail_market", "A"),
        )
elif st.session_state.get("_macro_detail_name"):
    with _page_slot.container():
        with st.spinner("加载行情…"):
            _render_macro_detail(st.session_state["_macro_detail_name"])
else:
    with _page_slot.container():
        # 页眉。原来是一条通栏的品牌红横幅+白色粗体字，那是整个页面上最抢眼
        # 的元素，但它承载的信息只有一个产品名——最重的视觉权重给了最不重要
        # 的信息。而且页面上真正需要被一眼看到的是涨跌色，横幅一红，涨跌红就
        # 不再突出了。改成一个安静的字标，视觉权重让回给数据。
        #
        # 右边那行"沪深 · 港股 · 美股 · 虚拟货币"2026-09-13 去掉：它不回答任何
        # 问题——用户点开"行情"就有市场切换、自选里也分市场，这行字既不能点
        # 也不随页面变化，只是把页眉从一个字标变成了两团东西。
        st.markdown(
            """
            <div style='margin:2px 0 20px'>
                <span style='font-size:1.14rem;font-weight:650;letter-spacing:-.022em;
                             color:var(--fa-text)'>Invest Agent</span>
            </div>
            """,
            unsafe_allow_html=True,
        )


        # "行情"分区的快速搜索框去掉了——用户反馈是累赘（"持仓"分区里
        # "新增持仓"自己就有搜索框，两边都放显得重复）。指数/个股的浏览
        # 入口保留在下面的指数卡片列表和涨跌幅排行榜里。

        # 用 radio 手动实现 tab 切换，不用 st.tabs()——st.tabs() 选中哪个是纯前端状态，
        # 代码控制不了；从持仓点进详情页再返回时，需要能把选中项强制拨回"持仓"。
        # "首页"放在最前面且是默认分区——打开网站先看首页（世界地图+今日资讯），
        # 不是直接扔进"行情"这种数据密集页面。
        # 分区也写进地址栏。个股详情页已经有稳定地址了（见上面 symbol/market/name
        # 那段），但主导航一直是纯 session 状态：刷新、收藏、或者把链接发给别人，
        # 一律弹回"首页"——用户在"自选"里看到一半刷新一下就得重新点回去。用英文
        # slug 而不是中文分区名，是因为中文要 percent-encode，复制出来的链接会是
        # 一长串 %E8%87%AA%E9%80%89，看不出来是哪个页面。
        if "_active_section" not in st.session_state:
            st.session_state["_active_section"] = _SECTION_BY_SLUG.get(
                st.query_params.get("tab", ""), "首页",
            )

        # 包一层带key的容器：Streamlit会给它加上 st-key-fa_nav 这个class，
        # CSS靠它把这一组radio单独渲染成下划线标签页，而不影响页面里其它
        # 横向radio（市场切换、K线周期那些仍然是分段控件的样子）。
        with st.container(key="fa_nav"):
            active_section = st.radio(
                "分区", ["首页", "行情", "持仓", "自选", "AI模拟炒股", "我的"],
                key="_active_section", horizontal=True, label_visibility="collapsed",
            )

        # 切分区时给一个加载提示（2026-09-05用户要求"一个界面到另一个界面
        # 实在反应不过来可以用加载中的界面辅助一下"）。只在分区真的变了那一次
        # 显示：同一个分区里的交互重跑（点按钮、切市场）本来就很快，每次都
        # 转圈反而更吵。判据是拿上一次渲染的分区名比对，存在一个非widget的
        # key 里，不会被 Streamlit 的 widget 状态清理掉。
        _prev_sec = st.session_state.get("_last_rendered_section")
        _switching = _prev_sec is not None and _prev_sec != active_section
        st.session_state["_last_rendered_section"] = active_section

        # 把当前分区同步回地址栏。只在值真的变了时才写，避免每次 rerun 都碰
        # query_params。
        _tab_slug = _SLUG_BY_SECTION.get(active_section, "home")
        if st.query_params.get("tab") != _tab_slug:
            st.query_params["tab"] = _tab_slug
        _sec_spinner = st.spinner("加载中…") if _switching else _nullcontext()

        if active_section == "首页":
            with _sec_spinner:
                _render_home_page()

        elif active_section == "行情":
            # 指数快照/大盘统计+涨停跌停池/南向资金+核心股/热门板块，这几块
            # 之前全挤在一段代码里顺序往下跑——点其中任何一个的交互按钮
            # （比如"显示更多"、"更多板块"）都会带动其余几块跟着重新拉一遍
            # 数据，是页面交互卡顿的主要原因。现在各自是独立的@st.fragment，
            # 点一个按钮只重新跑对应那一块。
            # 跨资产温度计（VIX/美债/美元/金油铜）本来放在这里，2026-09-12
            # 按用户要求移到首页，跟指数横条、世界地图叠成"今天全球什么情况"
            # 的第一屏。这里不再重复渲染——同一条数据在两个页面各画一遍，用户
            # 会以为是两份不同的东西。
            mkt_pick = st.radio("市场", ["沪深", "港股", "美股", "虚拟货币"], horizontal=True, key="_market_overview_pick")
            mkt_code = {"沪深": "A", "港股": "HK", "美股": "US", "虚拟货币": "CC"}[mkt_pick]

            # 虚拟货币走单独一条渲染路径，不是"少调用几个函数"那么简单：
            # 指数快照、涨停跌停池、南向资金、热门板块、新股这几块的概念在
            # 这个市场里全都不存在，硬套上去只会渲染出一排空壳。它需要的是
            # 另一组信息（24小时不休市、没有涨跌停、没有板块划分），所以给
            # 它自己的概览函数。
            if mkt_code == "CC":
                _render_crypto_overview()
            else:
                _render_index_snapshot(mkt_code)
                st.divider()

                if mkt_code == "A":
                    _render_a_share_overview()
                elif mkt_code == "HK":
                    _render_hk_overview()
                else:
                    _render_us_overview()

                st.divider()
                # 先看形状（热力图：钱往哪走），再读数字（列表：具体哪几个
                # 板块涨了多少），最后点进成分股。从面到点。
                _render_sector_heatmap(mkt_code)
                st.markdown("**热门板块**")
                # 跟上面热力图标同一件事：两处用的是不同的行业分类体系，
                # 同名板块涨跌幅对不上是分类差异不是数据错误（审计第11条）。
                st.caption(
                    "同花顺行业分类，按成交额排——跟上面热力图不是同一套，数值对不上正常。"
                    if mkt_code == "A" else "按成交额排热度，跟上面热力图同源。"
                )
                _render_hot_sectors(mkt_code)
                # 异动榜/热度榜/新股放在板块之后：板块回答"哪个方向在动"，
                # 这一块回答"具体哪几支在动"，从面到点，顺序上是收敛的。
                _render_market_extras(mkt_code)

        elif active_section == "持仓":
            if not st.session_state.get("logged_in"):
                st.write("")
                _, mid_empty, _ = st.columns([1, 2, 1])
                with mid_empty:
                    st.markdown(
                        "<div style='text-align:center;color:var(--fa-muted);padding:40px 0 10px'>"
                        "持仓管理是个人功能，需要登录后使用<br>"
                        "<span style='font-size:0.82rem'>行情/详情页/AI分析等其它功能无需登录即可查看</span>"
                        "</div>",
                        unsafe_allow_html=True,
                    )
                    if st.button("登录 / 注册", use_container_width=True):
                        st.session_state["guest_mode"] = False
                        st.rerun()
            else:
                _email = st.session_state["user_email"]
                positions = get_positions(_email)
                # 自选（shares=0，只关注不持仓）挪到独立的"自选"分区显示，这里
                # 只处理真实持仓——之前两者混在同一个列表里，用户反馈新加的自选
                # 股票"怎么跑到持仓里去了"，跟真金白银的仓位堆在一起分不清。
                holding_items = [p for p in positions if (p.get("shares") or 0) > 0]

                # 搜索（纯查行情，跳详情页）和添加持仓（真正记一笔仓位）是两个
                # 独立入口，不要合并——之前合并成一个放大镜图标时，点开就是"添加
                # 持仓"弹窗，没有单纯查一下行情的入口。build_multi_comparison
                # （charts.py）之前写好了没接界面，这里补上入口：持仓至少2只时才
                # 显示"对比"图标，三个图标并排。
                if len(holding_items) >= 2:
                    title_col, compare_col, search_col, add_col = st.columns([9, 1, 1, 1], vertical_alignment="center")
                    if compare_col.button("", icon=":material/show_chart:", key="pos_compare_icon", type="tertiary", help="对比持仓走势"):
                        _show_compare_dialog(holding_items)
                else:
                    title_col, search_col, add_col = st.columns([10, 1, 1], vertical_alignment="center")
                if search_col.button("", icon=":material/search:", key="pos_search_icon", type="tertiary", help="搜索"):
                    _show_stock_search_dialog(_email)
                if add_col.button("", icon=":material/add:", key="pos_add_icon", type="tertiary", help="添加持仓"):
                    _show_add_position_dialog(_email)

                if not holding_items:
                    st.write("")
                    _, mid_empty, _ = st.columns([1, 2, 1])
                    with mid_empty:
                        st.markdown(
                            "<div style='text-align:center;color:var(--fa-muted);padding:20px 0 10px'>"
                            "还没有持仓<br>"
                            "<span style='font-size:0.82rem'>点右上角的 + 按钮添加；填写股数或金额后才算持仓，成交均价可选填——"
                            "股数和金额都留空会加进「自选」分区</span>"
                            "</div>",
                            unsafe_allow_html=True,
                        )

                if holding_items:
                    # 环形图 | 持仓列表，左右各半——环形图不用@st.fragment(run_every=3)
                    # （见_render_positions_donut docstring：Plotly图3秒重绘会闪烁），
                    # 右边列表沿用原来的3秒自动刷新fragment，两边各自独立刷新节奏。
                    donut_col, list_col = st.columns([1, 1])
                    with donut_col:
                        _render_positions_donut(holding_items)
                    with list_col:
                        # 用户反馈持仓一般也就几只，市场筛选(全部/沪深/港股/美股)没有实际
                        # 必要，反而多一层点击——去掉筛选，统一直接展示全部持仓。
                        _render_position_rows(holding_items, _email)

                    # 卖出确认弹窗调用挪到这个稳定作用域（不是_render_position_rows
                    # 那个run_every=3的fragment内部）——见_render_position_rows里
                    # pos_del_按钮那段注释，原因是fragment的定时自动刷新会让弹窗绑定
                    # 的fragment失效，点"确认卖出"没反应。按shares>0过滤，避免自选
                    # 分区点的卖出误在这个持仓分区弹出来。
                    _sell_target = st.session_state.get("_confirm_sell_target")
                    if _sell_target and (_sell_target["item"].get("shares") or 0) > 0:
                        _confirm_sell_dialog(_email, _sell_target["item"], _sell_target["market"], _sell_target["cur_price"])

                    st.divider()
                    pnl_col, ai_col = st.columns([1, 1])
                    with pnl_col:
                        _render_positions_today_pnl(holding_items)
                        st.divider()
                        _render_max_capital_input(_email)
                    with ai_col:
                        _render_portfolio_advice(_email, holding_items)

                    # 组合体检放在AI组合分析之后、整页最下面：它是本地算的客观
                    # 结构（暴露/相关性/波动率/VaR），通栏展示——相关性矩阵塞进
                    # 半宽列里会挤成一团。
                    st.divider()
                    _render_portfolio_risk(holding_items)

        elif active_section == "自选":
            if not st.session_state.get("logged_in"):
                st.write("")
                _, mid_empty, _ = st.columns([1, 2, 1])
                with mid_empty:
                    st.markdown(
                        "<div style='text-align:center;color:var(--fa-muted);padding:40px 0 10px'>"
                        "自选是个人功能，需要登录后使用<br>"
                        "<span style='font-size:0.82rem'>行情/详情页/AI分析等其它功能无需登录即可查看</span>"
                        "</div>",
                        unsafe_allow_html=True,
                    )
                    if st.button("登录 / 注册", use_container_width=True, key="_watch_login_btn"):
                        st.session_state["guest_mode"] = False
                        st.rerun()
            else:
                _email = st.session_state["user_email"]
                # 自选＝positions表里shares=0的行（"只关注不持仓"），跟持仓
                # 是同一张表，靠shares区分，不是独立的表——见add_watch_only。
                watch_items = [p for p in get_positions(_email) if (p.get("shares") or 0) == 0]

                # 复用持仓分区那套圆形图标按钮样式（见上面"持仓"分支同款CSS的
                # 注释）——两个分区各自独立渲染，键名前缀不同，样式要各放一份。
                _, search_col, add_col = st.columns([10, 1, 1], vertical_alignment="center")
                if search_col.button("", icon=":material/search:", key="watch_search_icon", type="tertiary", help="搜索"):
                    _show_stock_search_dialog(_email)
                if add_col.button("", icon=":material/add:", key="watch_add_icon", type="tertiary", help="添加自选"):
                    _show_add_watchlist_dialog(_email)

                if not watch_items:
                    st.write("")
                    _, mid_empty, _ = st.columns([1, 2, 1])
                    with mid_empty:
                        st.markdown(
                            "<div style='text-align:center;color:var(--fa-muted);padding:20px 0 10px'>"
                            "还没有自选股票<br>"
                            "<span style='font-size:0.82rem'>点右上角的 + 按钮添加；股数和金额都留空即为自选</span>"
                            "</div>",
                            unsafe_allow_html=True,
                        )
                else:
                    # 2026-09-12（前端审计第9条）：52支混在一个列表里、不能
                    # 筛也不能排。市场筛选放在这里（只看 item 自带的 market
                    # 字段，不需要行情数据，最便宜）；排序要用到行情和AI评分，
                    # 塞不进这一层，作为参数交给 _render_position_rows 在取完
                    # 数之后做。
                    _mkt_labels = {"全部": None, "港股": "HK", "美股": "US", "沪深": "A", "加密": "CC"}
                    _present = {it.get("market", "A") for it in watch_items}
                    _opts = ["全部"] + [k for k, v in _mkt_labels.items() if v in _present]
                    _f_col, _s_col = st.columns([2, 1], vertical_alignment="center")
                    with _f_col:
                        _mkt_pick = st.radio(
                            "市场", _opts, horizontal=True, label_visibility="collapsed",
                            key="_watch_market_filter",
                        )
                    with _s_col:
                        _sort_pick = st.selectbox(
                            "排序", ["默认", "涨幅", "跌幅", "AI评分"],
                            label_visibility="collapsed", key="_watch_sort_mode",
                        )
                    _want = _mkt_labels.get(_mkt_pick)
                    _shown = [it for it in watch_items if _want is None or it.get("market", "A") == _want]
                    if not _shown:
                        st.caption("这个市场下没有自选。")
                    else:
                        st.caption(f"共 {len(_shown)} 支")
                        _render_position_rows(_shown, _email, sort_mode=_sort_pick)

                # 同上——挪到稳定作用域，避开run_every fragment失效的问题；
                # shares<=0过滤只处理"自选"这边点的卖出/取消关注。
                _sell_target = st.session_state.get("_confirm_sell_target")
                if _sell_target and (_sell_target["item"].get("shares") or 0) <= 0:
                    _confirm_sell_dialog(_email, _sell_target["item"], _sell_target["market"], _sell_target["cur_price"])

        elif active_section == "AI模拟炒股":
            # 2026-09-01用户明确要求"回看那边全部改成AI模拟炒股"，分区
            # 名字本身也在同一天改成"AI模拟炒股"——完全替换掉原来的AI判断
            # 准确率追踪入口，_render_accuracy_dashboard函数本身和它依赖
            # 的历史数据都还在，只是不再从这个入口调用。
            _render_ai_sim_dashboard()

        elif active_section == "我的":
            _render_my_page()

        _render_ai_assistant()
