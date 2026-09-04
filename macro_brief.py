"""宏观重大议题专区的离线生成器——美联储议息、通胀(CPI)、非农就业等。

2026-09-04新增，用户要求"首页做一个美联储会议、CPI、非农这类重大议题的专区，
不仅仅是一条新闻，把信息全部拿过来让AI通读分析，分析完写进服务器，不用每次
打开都分析一遍"。

为什么做成离线脚本而不是页面现算：这类分析要把十几条资讯汇总起来让AI通读，
一次几十秒、烧一次AI调用；而这份内容在一天之内对所有访客是同一份。放在页面
渲染时现算，等于每个人每次打开首页都重跑一遍，既慢又费。跟 advisor.py 那条
"cron跑完写库、页面只读最近一次结果"是同一个模式。

美联储那块刻意不只喂新闻：富途有现成的 get_fed_watch_target_rate，返回的是
CME FedWatch 的利率概率表（会议日期／目标区间／市场隐含概率），那是市场real
money 押出来的预期，比任何一篇解读文章都硬。有真数据就用真数据，新闻只作为
补充背景。

Futu SDK的线程不是daemon线程，脚本跑完必须显式 os._exit(0)，否则进程挂着不退
（这个项目反复踩过的老坑，见 advisor.py / sim_agent.py 同样的收尾）。
"""
import json
import os
import sys
from datetime import datetime, timezone

import advisor
import data_sources as ds
import tracker

# (topic, 标题, 搜索关键词列表)。关键词给多个是因为富途资讯搜索是精确匹配，
# 单个词容易漏——"非农"和"就业数据"命中的往往不是同一批稿子。
_TOPICS = [
    ("fed", "美联储与利率", ["美联储", "议息", "降息", "加息", "鲍威尔"]),
    ("cpi", "通胀数据", ["CPI", "通胀", "核心通胀", "PCE"]),
    ("payrolls", "就业数据", ["非农", "失业率", "就业数据", "ADP"]),
    ("china", "中国经济与政策", ["社融", "PMI", "央行", "LPR", "政治局会议"]),
]

# 每个议题配套的真实宏观时间序列：(region, 指标名关键词)。
# 2026-09-04补。此前只有美联储那条有真数据（FedWatch概率表），另外三条纯靠
# 新闻标题——那等于让AI复述别人的解读，它自己看不到任何一手数字。富途的
# get_macro_indicator_history 是现成的，而且每期带 predict_value（当时的市场
# 预期），"实际 vs 预期"才是宏观数据真正被交易的那个维度，只看绝对值看不出
# 市场是被超预期还是不及预期打了一巴掌。
_TOPIC_SERIES = {
    "cpi": [("US", "美国CPI同比"), ("US", "美国核心CPI同比"), ("US", "美国PCE同比")],
    "payrolls": [("US", "美国非农就业人数"), ("US", "美国失业率")],
    # fed 不在这里取序列：日频的"美国联邦基金利率"两周之内根本不动，画出来
    # 是一条平线。改成单独取政策利率的历次调整路径（见 _rate_path）。
    "fed": [],
    "china": [("CN", "CPI同比"), ("CN", "制造业PMI")],
}

_SYSTEM = """你是一位宏观策略分析师，服务对象是一位同时持有港股和美股的个人投资者。

你的任务：把下面给到的资讯（以及可能附带的市场数据）通读一遍，就这一个议题写出
一段可用的解读。要求：

1. 只用下面给的材料，不能编任何数字、日期或事件。材料里没提到的数据就当作没有，
   如实说"暂时没有看到明确数据"，不要凭记忆补。你的知识里可能有过时的宏观数据，
   一律以下面的材料为准。
2. 先说清楚"现在的事实是什么"（最新数据／会议时间／市场隐含预期），再说"这意味着
   什么"，最后说"接下来盯什么"。不要一上来就下结论。
3. 必须落在对港股和美股的实际影响上——这个人不炒美债也不炒外汇，泛泛谈"利好风险
   资产"没有用。说清楚是影响估值（贴现率）还是影响盈利（需求），以及哪类资产
   更敏感（成长股/高息股/黄金/港股流动性）。
4. 不确定的地方要明说不确定，并给出"什么信号出现了就说明往哪个方向走"。宏观
   本来就充满分歧，装作确定比说不知道更有害。
5. 不给买卖建议，不推荐具体股票。这是背景判断，不是操作指令。
6. 全文控制在350字以内，不要小标题、不要项目符号，就是几段连贯的话。
7. 不允许出现任何 emoji 或表情符号。

严格按以下格式输出（不要多余寒暄）：
一句话结论：<20字以内，这个议题当前最重要的一句话>
现状：<最新的事实和数据>
影响：<对港股/美股意味着什么，说清楚是估值还是盈利路径>
盯什么：<接下来的关键时间点或信号>"""


def _fed_watch_rows() -> list[dict]:
    """FedWatch原始概率表，取最近4次会议的全部区间——这份是留给页面画图的，
    跟喂给AI的那份精简文本不是一回事：图上多画几个区间才看得出分布形状，
    而喂给AI时尾部那些1%以下的区间只会淹没重点。"""
    try:
        ret, df = ds._futu_call(lambda ctx: ctx.get_fed_watch_target_rate(), timeout=15, default=(None, None))
    except Exception:
        return []
    if df is None or getattr(df, "empty", True):
        return []
    try:
        meetings = list(dict.fromkeys(df["meeting_date"].tolist()))[:4]
        sub = df[df["meeting_date"].isin(meetings)]
        return [
            {"meeting": str(r["meeting_date"]), "range": str(r["target_range"]),
             "prob": float(r["probability"])}
            for _, r in sub.iterrows() if float(r["probability"]) >= 1.0
        ]
    except Exception:
        return []


def _fed_watch_text() -> str:
    """CME FedWatch 的利率概率表整理成文字。只取最近三次会议，每次会议里
    只保留概率最高的两个区间——完整表63行全喂进去，AI会淹没在数字里，而且
    尾部那些概率1%以下的区间对判断没有任何帮助。"""
    try:
        ret, df = ds._futu_call(lambda ctx: ctx.get_fed_watch_target_rate(), timeout=15, default=(None, None))
    except Exception:
        return ""
    if df is None or getattr(df, "empty", True):
        return ""
    lines = []
    try:
        for meeting in list(dict.fromkeys(df["meeting_date"].tolist()))[:3]:
            sub = df[df["meeting_date"] == meeting].sort_values("probability", ascending=False).head(2)
            bits = [f"{r['target_range']} {r['probability']:.1f}%" for _, r in sub.iterrows()]
            lines.append(f"  {meeting} 会议：" + "、".join(bits))
    except Exception:
        return ""
    if not lines:
        return ""
    return (
        "CME FedWatch 市场隐含的联邦基金目标利率概率（这是市场真金白银押出来的预期，"
        "不是评论员观点）：\n" + "\n".join(lines)
    )


def _rate_path() -> dict:
    """美联储政策利率的历次调整路径，只有 fed 议题用。取不到就返回空字典，
    不让它拖垮整篇简报的生成。"""
    try:
        return ds.get_fed_rate_path() or {}
    except Exception as e:
        print(f"[fed] 利率路径取失败（跳过，不影响其余材料）: {e}")
        return {}


def _series_for(topic: str) -> list[dict]:
    """把这个议题配套的宏观序列都取回来。取不到的静默跳过，不影响其它。"""
    out = []
    for region, kw in _TOPIC_SERIES.get(topic, []):
        try:
            sr = ds.get_macro_series(region, kw, max_count=14)
        except Exception:
            sr = None
        if sr and sr.get("points"):
            out.append(sr)
    return out


def _series_text(series: list[dict]) -> str:
    """序列整理成给AI读的文字。只列最近6期，并且把"实际/预期/前值"三列都
    摆出来——超预期还是不及预期，是这类数据唯一真正重要的信息。
    百分比类的value富途返回的是小数（0.0247表示2.47%），这里统一换算成
    百分数再喂，免得AI自己去猜量纲、或者把2.47%说成0.02%。"""
    if not series:
        return ""
    blocks = []
    for sr in series:
        is_pct = sr.get("unit") == "PERCENT"
        lines = []
        for p in sr["points"][-6:]:
            def _f(v):
                if v is None:
                    return "—"
                return f"{v * 100:.2f}%" if is_pct else f"{v:,.0f}"
            lines.append(
                f"    {p['date']}：实际{_f(p['value'])}／市场预期{_f(p['predict'])}／前值{_f(p['previous'])}"
            )
        blocks.append(f"  {sr['name']}（最近6期）：\n" + "\n".join(lines))
    return (
        "真实宏观数据（来自富途宏观指标库，实际值/当时的市场预期/前值三列都给了，"
        "\"实际对比预期\"是这类数据最重要的维度）：\n" + "\n".join(blocks)
    )


def _news_text(keywords: list[str], per_kw: int = 4) -> tuple[str, list[dict]]:
    """按关键词抓资讯，去重后拼成文本。同时返回原始条目，落库存档，方便
    以后回头核对AI当时是基于哪些材料写的——不留原材料的分析没法复核。"""
    seen, items = set(), []
    for kw in keywords:
        try:
            df = ds.get_futu_news(kw, max_count=per_kw)
        except Exception:
            continue
        if df is None or df.empty:
            continue
        for _, r in df.iterrows():
            title = str(r.get("新闻标题") or "").strip()
            if not title or title in seen:
                continue
            seen.add(title)
            items.append({"title": title, "date": str(r.get("日期") or ""), "url": str(r.get("url") or "")})
    if not items:
        return "", []
    lines = [f"  {it['date']} {it['title']}" for it in items[:20]]
    return "相关资讯标题：\n" + "\n".join(lines), items[:20]


def build_one(topic: str, title: str, keywords: list[str]) -> bool:
    news_text, items = _news_text(keywords)
    extra = _fed_watch_text() if topic == "fed" else ""
    chart_rows = _fed_watch_rows() if topic == "fed" else []
    rate_path = _rate_path() if topic == "fed" else {}
    series = _series_for(topic)
    series_text = _series_text(series)
    if not news_text and not extra and not series_text:
        print(f"[{topic}] 没有拿到任何材料，跳过（不写一条空解读进库）")
        return False

    user = f"议题：{title}\n\n"
    if rate_path.get("points"):
        _rp = rate_path["points"]
        _lines = [f"- {p['date']} 调整至 {p['value'] * 100:.2f}%" for p in _rp]
        user += ("美联储政策利率（目标利率上限）的历次调整：\n" + "\n".join(_lines)
                 + f"\n当前维持在 {_rp[-1]['value'] * 100:.2f}%\n\n")
    if series_text:
        # 真实数据放在最前面——AI读材料是有顺序效应的，先给硬数据再给评论文章，
        # 结论更容易落在数字上而不是跟着标题的情绪走。
        user += series_text + "\n\n"
    if extra:
        user += extra + "\n\n"
    if news_text:
        user += news_text + "\n\n"
    user += f"当前时间：{datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M')}"

    # 走统一的故障转移入口，千问顶不住自动换智谱（见 advisor.chat_with_failover）。
    text = advisor.chat_with_failover(
        [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
        max_tokens=2000, temperature=0.3, timeout=120, tag=f"macro/{topic}",
    ).strip()
    if not text:
        print(f"[{topic}] AI返回空内容，跳过")
        return False
    tracker.log_macro_brief(
        topic, title, text,
        json.dumps(items, ensure_ascii=False),
        json.dumps(
            {"fed_watch": chart_rows, "series": series, "rate_path": rate_path},
            ensure_ascii=False,
        ) if (chart_rows or series or rate_path) else "",
    )
    print(f"[{topic}] 已写入，{len(text)}字，材料{len(items)}条")
    return True


def main() -> int:
    advisor._load_secrets_into_env()
    ok = 0
    for topic, title, kws in _TOPICS:
        try:
            if build_one(topic, title, kws):
                ok += 1
        except Exception as e:
            print(f"[{topic}] 失败：{type(e).__name__} {e}")
    print(f"完成，成功 {ok}/{len(_TOPICS)} 个议题")
    return 0


if __name__ == "__main__":
    code = main()
    # Futu SDK的线程不是daemon线程，不强制退出的话进程会一直挂着不退出，
    # 变成占着Futu连接的僵尸进程（advisor.py / sim_agent.py 同一个老坑）。
    sys.stdout.flush()
    os._exit(code)
