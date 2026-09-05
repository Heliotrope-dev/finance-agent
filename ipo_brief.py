# -*- coding: utf-8 -*-
"""港股新股（IPO）认购简报：抓招股数据 + 新闻，让AI给一份"要不要打"的判断，写库供首页只读。

跟 macro_brief.py 同一套思路，理由也一样：一次要拉数据加AI生成，放在页面渲染
路径里等于每个访客都等一遍，还会把AI额度按访问量烧掉。预先算好落库，首页只是
读一行。

关于数据边界，这个模块里有一条硬约束值得写在最前面：富途的 get_ipo_list 只给
得到招股价区间、每手股数、入场费、上市日、申购截止日这几项硬数据，**拿不到
保荐人、基石投资者、超额认购倍数、绿鞋和回拨机制**。后面这些恰恰是判断"要不要
打新"最关键的信息，只能从新闻搜索里取，而新闻不一定提到。所以提示词里明确
要求：这些项目只有在给定材料里真的出现才能写，没有就写"未查到"，绝对不能凭
常识推测——打新是要真金白银下单的场景，编一个"有绿鞋"出来比不说更糟。

另外纠正一个常见说法：港股新股有"绿鞋"（超额配售选择权，稳定上市后股价）和
"回拨机制"（公开发售超额认购到一定倍数时，从国际配售回拨股份给公开发售），
没有"红鞋"这个机制。这个模块只讲这两个真实存在的。
"""
import json
import os
import sys
from datetime import datetime, timezone

import advisor
import data_sources as ds
import tracker

_SYSTEM = """你是一位熟悉港股新股市场的分析师，服务对象是一位会自己决定要不要
申购的个人投资者。下面会给你一只即将上市的港股新股的招股硬数据，以及搜到的
相关资讯。请就这一只写一份简短的申购参考。

硬性要求：

1. 只能用给定的材料。招股价、每手股数、入场费、上市日期这些是接口给的硬数据，
   可以直接用；保荐人、基石投资者、超额认购倍数、绿鞋（超额配售选择权）、
   回拨机制这些**接口拿不到**，只有在下面的资讯材料里真的提到才能写，没提到
   就明确写"未查到"。绝对不要凭对这家公司或这个行业的印象推测这些数字——
   打新是要真金白银下单的，编一个"超额认购50倍"或"设有绿鞋"出来，比不说危险
   得多。

2. 关于绿鞋和回拨要讲清楚它们的意义，不要只报有无：绿鞋（超额配售选择权）是
   承销商在上市后一段时间内可以按发行价增发一部分股份，作用是稳定股价、通常
   意味着破发时有一定托底；回拨机制是公开发售超额认购到一定倍数时从国际配售
   回拨股份给散户，回拨比例越高散户中签率越高但单签获配也越分散。材料里没提
   就说未查到，不要展开解释一个不存在的机制。港股没有"红鞋"这种机制，用户
   如果这么问，如实说明。

3. 结论要给一个明确的倾向：值得申购 / 谨慎参与 / 建议回避 / 信息不足难以判断。
   最后一个不是逃避——招股信息披露不全的时候，说"信息不足"比硬凑一个结论
   诚实。给倾向时要说清楚是基于什么：是定价相对同业便宜，还是行业景气，还是
   纯粹博上市首日情绪。

4. 入场费（认购一手所需资金）要点出来，这是散户最直接的门槛。

5. 风险必须写，而且要具体到这一只：比如招股价区间上限对应的市盈率明显高于
   行业、基石占比过高导致流通盘小、所处行业近期新股普遍破发等。写不出具体的
   就说"公开材料里没有看到特别的风险点"，不要写"股市有风险"这种废话。

6. 如果给了"近期新股市场整体表现"那段统计，它是真实算出来的数据，可以引用，
   而且在单只材料很薄的时候它往往是最有价值的依据——比如"近期破发率六成，
   这只又没有基石信息，倾向回避"就是一个有据可依的结论，比只写"信息不足"
   有用。但要说清楚这是市场整体统计、不是这一只的特征。

7. 不允许出现任何 emoji 或表情符号。

严格按以下格式输出（每段以段名加中文冒号开头，段名不要改写）：
一句话结论：<值得申购/谨慎参与/建议回避/信息不足难以判断，加一句为什么>
公司概况：<这家公司做什么，几句话，材料里没有就写未查到>
定价与门槛：<招股价区间、每手股数、入场费、发行市盈率如果有的话>
市场热度：<超额认购倍数、基石投资者、保荐人，只写材料里有的，没有就写未查到>
绿鞋与回拨：<只写材料里明确提到的，没有就写未查到；有的话说明它对散户意味着什么>
风险：<具体到这一只>
"""


def _ipo_news_text(name: str, limit: int = 8) -> tuple[str, list]:
    """搜这只新股的相关资讯。名字本身就是最好的搜索词——新股没有历史行情、
    没有财报，能拿到的外部信息几乎全在新闻里。"""
    try:
        df = ds.get_stock_news(name, limit=limit)
        items = df.to_dict("records") if df is not None and not df.empty else []
    except Exception:
        return "", []
    lines = []
    keep = []
    for it in items:
        title = str(it.get("summary") or it.get("title") or "").strip()
        if not title:
            continue
        lines.append(f"- {title}（{it.get('日期') or it.get('date') or ''}）")
        keep.append(it)
    return "\n".join(lines), keep


def _facts_text(ipo: dict) -> str:
    """把接口给的硬数据摆成一段。刻意逐项标注"接口数据"，让AI清楚哪些是可信的
    事实、哪些要靠下面的新闻补。"""
    parts = [f"- 代码：{ipo['symbol']}（港股）", f"- 名称：{ipo['name']}"]
    if ipo.get("list_date"):
        parts.append(f"- 上市日期：{ipo['list_date']}")
    if ipo.get("apply_end"):
        parts.append(f"- 认购截止：{ipo['apply_end']}")
    lo, hi = ipo.get("price_min"), ipo.get("price_max")
    if lo and hi:
        parts.append(f"- 招股价：{lo:,.2f}" + (f"-{hi:,.2f} 港元" if hi != lo else " 港元（定价已确定）"))
    elif hi:
        parts.append(f"- 招股价上限：{hi:,.2f} 港元")
    if ipo.get("lot_size"):
        parts.append(f"- 每手股数：{int(ipo['lot_size'])}")
    if ipo.get("entrance_price"):
        parts.append(f"- 入场费（认购一手所需资金）：{ipo['entrance_price']:,.2f} 港元")
    if ipo.get("issue_pe"):
        parts.append(f"- 发行市盈率：{ipo['issue_pe']:.2f} 倍")
    if ipo.get("industry_pe"):
        parts.append(f"- 行业市盈率：{ipo['industry_pe']:.2f} 倍")
    parts.append("（以上为交易接口提供的硬数据。保荐人、基石投资者、超额认购倍数、"
                 "绿鞋、回拨机制这几项接口拿不到，只能看下面的资讯里有没有提到。）")
    return "\n".join(parts)


def build_one(ipo: dict) -> bool:
    news_text, items = _ipo_news_text(ipo["name"])
    facts = _facts_text(ipo)

    # 把近期新股整体表现一并给AI：单只的招股材料常常很薄（实测这三只新闻
    # 一条都搜不到），而"最近打新整体赚不赚钱、破发率多少"是有真实数据支撑
    # 的背景，能让结论不至于只能写"信息不足"。
    market_ctx = ""
    try:
        _p = tracker.get_latest_ipo_performance()
        _s = (_p or {}).get("stats") or {}
        if _s.get("count"):
            market_ctx = (
                f"近{_s['days']}天港股共{_s['count']}只新股上市，首日涨跌幅"
                f"平均{_s['avg']:+.1f}%、中位数{_s['median']:+.1f}%、"
                f"破发率{_s['break_rate']:.0f}%（最高{_s['max']:+.0f}%、最低{_s['min']:+.0f}%）。"
                "注意均值受个别翻倍股拉高，判断'随便打一只大概赚多少'要看中位数。"
            )
    except Exception:
        market_ctx = ""

    user = (
        f"新股：{ipo['name']}（{ipo['symbol']}）\n\n"
        f"招股硬数据：\n{facts}\n\n"
        + (f"近期新股市场整体表现（真实统计，可以引用）：\n{market_ctx}\n\n" if market_ctx else "")
        + (f"相关资讯：\n{news_text}\n\n" if news_text else "相关资讯：（没有搜到相关新闻）\n\n")
        + f"当前时间：{datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M')}"
    )

    text = advisor.chat_with_failover(
        [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
        max_tokens=2000, temperature=0.3, timeout=120, tag=f"ipo/{ipo['symbol']}",
    ).strip()
    if not text:
        print(f"[{ipo['symbol']}] AI返回空内容，跳过")
        return False

    tracker.log_ipo_brief(
        ipo["symbol"], ipo["name"], ipo.get("list_date", ""), ipo.get("apply_end", ""),
        text, json.dumps(ipo, ensure_ascii=False), json.dumps(items, ensure_ascii=False),
    )
    print(f"[{ipo['symbol']}] {ipo['name']} 已写入，{len(text)}字，资讯{len(items)}条")
    return True


def main() -> int:
    advisor._load_secrets_into_env()
    try:
        ipos = ds.get_ipo_calendar("HK", limit=8)
    except Exception as e:
        print(f"取新股清单失败：{e}")
        return 1
    if not ipos:
        print("当前没有待上市的港股新股")
        return 0

    # 先算近期已上市新股的首日表现。放在最前面是因为它不依赖AI，就算后面
    # AI调用全挂了，这块统计仍然能更新——而这块恰恰是打新判断里最硬的依据。
    try:
        perf = ds.get_recent_ipo_performance(days=120, max_count=60)
        if perf.get("items"):
            tracker.log_ipo_performance(json.dumps(perf, ensure_ascii=False))
            st = perf["stats"]
            print(f"近{st['days']}天共{st['count']}只新股上市："
                  f"首日平均{st['avg']:+.1f}%、中位数{st['median']:+.1f}%、"
                  f"破发率{st['break_rate']:.0f}%")
        else:
            print("没有算出新股首日表现（可能是接口没返回）")
    except Exception as e:
        print(f"首日表现统计失败（不影响后面的单只简报）：{e}")

    ok = 0
    for ipo in ipos:
        # 政府债券这类不是股票打新，跳过——它没有基本面可分析，也没有绿鞋回拨
        # 这套机制，混在新股里只会稀释真正要看的内容。
        if "银债" in ipo["name"] or "债券" in ipo["name"]:
            print(f"[{ipo['symbol']}] {ipo['name']} 是债券，跳过")
            continue
        try:
            if build_one(ipo):
                ok += 1
        except Exception as e:
            print(f"[{ipo['symbol']}] 失败：{e}")
    print(f"完成 {ok} 只")
    return 0


if __name__ == "__main__":
    code = main()
    # 富途相关模块的线程不是daemon线程（项目老坑），不强制退出会挂住；
    # os._exit 跳过stdout缓冲刷新，必须先flush。
    sys.stdout.flush()
    os._exit(code)
