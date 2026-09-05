# -*- coding: utf-8 -*-
"""公开网页兜底研究：项目里任何一处"接口拿不到"的数据，都从这里去网上找。

起因是2026-09-05用户看到新股简报上一片"未查到"——公司概况未查到、保荐人
未查到、绿鞋与回拨未查到。根因不是AI偷懒，是它手上真的没材料：ipo_brief
原来只调 data_sources.get_stock_news(名字)，而富途的新闻库是按**已上市**
股票代码索引的，未上市的新股搜出来永远是0条。

用户的要求是"下次找不到挖不到的数据我们都去调用他"，所以这个模块刻意做成
通用的，不是新股专用：

    search(q)                 搜索，返回标题/链接/域名
    read_url(u)               把网页读成正文文本
    research(q)               搜索+读正文，直接返回可以塞进提示词的一段材料
    hk_ipo_facts(symbol)      港股新股的结构化事实（专用快路径）

技术选型上有一条硬约束：不能引入需要密钥的服务。项目的AI额度已经在三家
供应商之间来回倒腾了，再挂一个会过期、会欠费的key上去只是多一个故障点。
所以两个后端都是免费且免注册的：

  DuckDuckGo 的 html 端点     搜索，无需key
  r.jina.ai                   正文抽取，无需key，自动处理JS渲染和排版

实测（VPS在洛杉矶，国际出口通畅）两个都是200。反过来说这个模块在墙内机器上
不一定能用，所以每个函数失败都返回空值而不是抛异常——拿不到材料时让调用方
照常走"未查到"那条路，比让整个简报任务崩掉好。

缓存写在磁盘不放内存：VPS只有1.9GB内存，而且调用方多是定时脚本，进程级
内存缓存跨进程也用不上。
"""
import hashlib
import html as _html
import json
import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

_CACHE_DIR = Path(__file__).resolve().parent / "data" / "web_cache"
_CACHE_TTL = 6 * 3600

# 两个后端要的UA正好相反，实测出来的，别想当然统一成一个：
#
#   DuckDuckGo 的 html 端点  必须给浏览器UA，python-urllib 的默认UA会被当成
#                            异常流量挡掉。
#   r.jina.ai                给完整 Chrome UA 反而返回 403，朴素UA或不带UA才
#                            是200。合理——它是个抓取代理，会拦下声称自己是
#                            真浏览器的请求，免得被人当成翻墙浏览器用。
#
# 第一版两处共用了完整Chrome UA，结果搜索正常、正文一条都读不出来，
# read_url 静默返回空串，表现和"网上确实没这条信息"一模一样。
_UA_BROWSER = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_UA_READER = "finance-agent/1.0"


def _cache_path(key: str) -> Path:
    return _CACHE_DIR / (hashlib.sha1(key.encode("utf-8")).hexdigest() + ".json")


def _cache_get(key: str, ttl: int = _CACHE_TTL):
    p = _cache_path(key)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        if time.time() - d.get("ts", 0) < ttl:
            return d.get("val")
    except Exception:
        pass
    return None


def _cache_put(key: str, val) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(key).write_text(
            json.dumps({"ts": time.time(), "val": val}, ensure_ascii=False),
            encoding="utf-8")
    except Exception:
        pass


# 进程级出站节流。调用方 advisor._judge_one 是并发跑的（自选20线程、
# 持仓10线程），不加约束的话一轮判断会在几秒内打出几十个请求，两个后端都是
# 免费额度，必然被限流——而限流的表现是抓不到内容，跟"网上没有这条信息"
# 长得一模一样，排查起来很费劲。所以宁可串行慢一点：10支持仓每支3个请求，
# 按1.2秒间隔也就多花36秒，相对判断本身几百秒的预算可以忽略。
_THROTTLE_LOCK = threading.Lock()
_MIN_INTERVAL = 1.2
_last_call = [0.0]


def _throttle() -> None:
    with _THROTTLE_LOCK:
        wait = _MIN_INTERVAL - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()


def _http(url: str, *, data: bytes | None = None, timeout: int = 25,
          ua: str = _UA_BROWSER) -> str:
    _throttle()
    req = urllib.request.Request(url, data=data, headers={"User-Agent": ua})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return raw.decode("utf-8", "ignore")


def search(query: str, limit: int = 8, *, ttl: int = _CACHE_TTL) -> list[dict]:
    """网页搜索，返回 [{"title","url","domain"}]。失败返回空列表。"""
    ck = f"ddg::{query}::{limit}"
    hit = _cache_get(ck, ttl)
    if hit is not None:
        return hit

    try:
        h = _http("https://html.duckduckgo.com/html/",
                  data=urllib.parse.urlencode({"q": query}).encode())
    except Exception as e:
        print(f"[web_research] 搜索请求失败({type(e).__name__})，本次拿不到材料：{query[:40]}")
        return []

    # 限流要能被看见。2026-09-05实测：调用频繁之后DDG会返回一个200的页面，
    # 里面一条结果都没有、但带着 anomaly 字样——如果只看"解析出0条"就返回
    # 空列表，调用方和AI都会把它理解成"网上确实没有这条信息"，而真相是
    # "我们被挡住了"。这两种情况对打新判断的意义完全相反：前者是事实，
    # 后者是我们的数据缺口。所以宁可在日志里吵一句，也不要静默退化。
    if "anomaly" in h.lower() or "unusual traffic" in h.lower():
        print(f"[web_research] 搜索被限流（DuckDuckGo 反爬拦截），本次没有材料。"
              f"这不代表网上没有相关信息：{query[:40]}")
        return []

    out, seen = [], set()
    for m in re.finditer(r'class="result__a" href="(.*?)".*?>(.*?)</a>', h, re.S):
        u = _html.unescape(m.group(1))
        # DDG 的结果链接是 //duckduckgo.com/l/?uddg=<真实地址> 的跳转形式，
        # 直接拿去请求会拿到跳转页而不是内容，必须把 uddg 参数解出来。
        if u.startswith("//duckduckgo.com/l/") or u.startswith("/l/"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse("https:" + u.lstrip(":")).query)
            u = qs.get("uddg", [""])[0]
        if not u.startswith("http"):
            continue
        title = _html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        if u in seen:
            continue
        seen.add(u)
        out.append({"title": title, "url": u,
                    "domain": urllib.parse.urlparse(u).netloc})
        if len(out) >= limit:
            break
    if out:
        _cache_put(ck, out)
    return out


def read_url(url: str, *, max_chars: int = 6000, timeout: int = 30,
             ttl: int = _CACHE_TTL) -> str:
    """把网页读成正文文本。走 r.jina.ai，它会处理JS渲染并去掉导航和广告。

    对财经站点这一步很关键：直连 etnet 之类的页面，抓回来的前一千字全是
    页边的恒指报价和汇率滚动条，真正的招股信息被淹掉了。
    """
    ck = f"read::{url}::{max_chars}"
    hit = _cache_get(ck, ttl)
    if hit is not None:
        return hit

    try:
        raw = _http("https://r.jina.ai/" + url, timeout=timeout, ua=_UA_READER)
    except Exception:
        return ""

    # r.jina.ai 的响应前面有 Title/URL Source 几行头，正文在 Markdown Content 之后
    i = raw.find("Markdown Content:")
    text = raw[i + len("Markdown Content:"):] if i >= 0 else raw
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    # 拿到一张跟踪像素说明这个站把 reader 挡掉了，当作没抓到，别把这句
    # 噪音喂给AI（实测 etnet 就是这样）。
    if not text or "1x1 image" in text[:200]:
        return ""

    text = text[:max_chars]
    _cache_put(ck, text)
    return text


# 登录墙站点：抓回来的"正文"其实是登录提示和导航栏，关键词一个都没有，
# 白白占掉提示词预算还可能让AI以为这就是全部材料。雪球实测就是这样。
_SKIP_DOMAINS = ("xueqiu.com", "weibo.com", "zhihu.com", "twitter.com", "x.com")


def _looks_like_junk(text: str) -> bool:
    """判断抓回来的是正文还是导航。

    判据是链接密度：正常文章里markdown链接占比很低，而导航页/登录墙几乎
    整页都是链接。用比例而不是关键词判断，是因为关键词随调用场景变，
    而"这页没有正文"这件事本身跟场景无关。
    """
    if len(text) < 300:
        return True
    links = len(re.findall(r"\]\(http", text))
    return links * 60 > len(text)


def research(query: str, *, read_top: int = 2, limit: int = 6,
             max_chars: int = 4000) -> str:
    """搜索 + 读正文，返回一段可以直接塞进提示词的材料。挖不到就返回空串。

    每段都标注来源域名和链接：AI被要求"只用给定材料"，那材料就得能追溯到
    出处，否则没法判断一条说法是抓来的还是它自己编的。
    """
    hits = search(query, limit=limit)
    if not hits:
        # 返回空串而不是一句"没搜到"：调用方（ipo_brief/advisor）都会在材料
        # 为空时走各自的降级路径，塞一句解释进去反而会被当成材料喂给AI。
        # 真正的可见性在 search() 里的日志，那是给排查的人看的。
        return ""

    parts = ["【搜索结果标题】"]
    parts += [f"- {h['title']}（{h['domain']}）" for h in hits]

    if read_top <= 0:
        # 初筛路径只要标题。提前返回而不是让下面的循环空转，省掉一次无谓的
        # 域名过滤遍历，也让"只读标题"这个意图在代码里是显式的。
        return "\n".join(parts)

    bodies = []
    # 多试几条候选而不是只读前 read_top 条：抓取失败和登录墙都很常见，
    # 固定读前两条经常两条都是废的。读满 read_top 条有效正文就停。
    for h in hits:
        if len(bodies) >= read_top:
            break
        if any(d in h["domain"] for d in _SKIP_DOMAINS):
            continue
        body = read_url(h["url"], max_chars=max_chars)
        if body and not _looks_like_junk(body):
            bodies.append(f"【正文来源：{h['domain']} {h['url']}】\n{body}")
    if bodies:
        parts.append("")
        parts += bodies
    return "\n".join(parts)


# hkipox 把港股新股的招股要素整理成了固定版式的一页，而且URL就是五位代码，
# 不用搜索直接就能定位。实测09976这一页同时给出了保荐人、稳价人、绿鞋有无、
# 认购倍数、回拨机制表、发行比例、业务范围和募资用途——这些正是富途接口
# 全都拿不到、而打新判断最依赖的字段。所以给它开一条专用快路径，比走通用
# 搜索既准又省一次请求。
_IPO_FACT_URL = "https://hkipox.com/stock/{code}"


def hk_ipo_facts(symbol: str, *, max_chars: int = 5000) -> str:
    """港股新股的结构化招股事实。symbol 传 09976 或 9976 都行。"""
    code = re.sub(r"\D", "", str(symbol or ""))
    if not code:
        return ""
    code = code.zfill(5)
    txt = read_url(_IPO_FACT_URL.format(code=code), max_chars=max_chars, ttl=3600)
    if not txt:
        return ""
    return f"【招股要素来源：hkipox.com/stock/{code}】\n{txt}"


if __name__ == "__main__":
    import os
    import sys

    q = " ".join(sys.argv[1:]) or "江波龙 09976 港股 招股 保荐人 基石投资者"
    if re.fullmatch(r"\d{4,5}", q.strip()):
        print(hk_ipo_facts(q.strip())[:3000])
    else:
        print(research(q)[:3000])
    sys.stdout.flush()
    os._exit(0)
