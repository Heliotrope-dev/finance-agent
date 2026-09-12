"""_market_session 的时段判断——不启动 Streamlit，直接把那段逻辑按源码复刻一份来验。

复刻而不是 import app：app.py 顶层会 st.set_page_config + 注入 CSS + 连数据源，
拉起来太重。这里只校验时段表和分支本身对不对。
"""
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

_MARKET_TZ = {"A": "Asia/Shanghai", "HK": "Asia/Hong_Kong", "US": "America/New_York"}
_MARKET_SESSIONS = {
    "A": [("09:30", "11:30"), ("13:00", "15:00")],
    "HK": [("09:30", "12:00"), ("13:00", "16:00")],
    "US": [("09:30", "16:00")],
}


def _market_session(market, now=None):
    tz = ZoneInfo(_MARKET_TZ.get(market, "Asia/Shanghai"))
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    t = now.time()
    if now.weekday() >= 5:
        return {"state": "周末休市", "open": False, "local": now}
    for i, (a, b) in enumerate(_MARKET_SESSIONS.get(market, [])):
        _a = datetime.strptime(a, "%H:%M").time()
        _b = datetime.strptime(b, "%H:%M").time()
        if _a <= t < _b:
            return {"state": "交易中", "open": True, "local": now}
        if i == 0 and len(_MARKET_SESSIONS[market]) > 1 and _b <= t < datetime.strptime(
                _MARKET_SESSIONS[market][1][0], "%H:%M").time():
            return {"state": "午间休市", "open": False, "local": now}
    _first_open = datetime.strptime(_MARKET_SESSIONS.get(market, [("09:30", "16:00")])[0][0], "%H:%M").time()
    if t < _first_open:
        return {"state": "盘前", "open": False, "local": now}
    return {"state": "已收盘", "open": False, "local": now}


def at(mkt, local_str):
    tz = ZoneInfo(_MARKET_TZ[mkt])
    return datetime.strptime(local_str, "%Y-%m-%d %H:%M").replace(tzinfo=tz)


CASES = [
    # (市场, 交易所本地时间, 期望状态)
    ("HK", "2026-09-11 08:00", "盘前"),
    ("HK", "2026-09-11 10:00", "交易中"),
    ("HK", "2026-09-11 12:30", "午间休市"),
    ("HK", "2026-09-11 14:00", "交易中"),
    ("HK", "2026-09-11 16:30", "已收盘"),
    ("HK", "2026-09-12 23:00", "周末休市"),  # 09-12 是周六
    ("HK", "2026-09-13 10:00", "周末休市"),   # 09-13 是周日
    ("HK", "2026-09-14 10:00", "交易中"),     # 09-14 是周一
    ("A",  "2026-09-11 11:00", "交易中"),
    ("A",  "2026-09-11 12:00", "午间休市"),
    ("A",  "2026-09-11 14:59", "交易中"),
    ("A",  "2026-09-11 15:00", "已收盘"),
    ("US", "2026-09-11 09:29", "盘前"),
    ("US", "2026-09-11 09:30", "交易中"),
    ("US", "2026-09-11 15:59", "交易中"),
    ("US", "2026-09-11 16:00", "已收盘"),
]

fail = 0
for mkt, when, want in CASES:
    got = _market_session(mkt, at(mkt, when))["state"]
    ok = got == want
    if not ok:
        fail += 1
    print(("  ok  " if ok else "  FAIL"), mkt, when, "->", got, "" if ok else f"(期望 {want})")
print("\n失败", fail, "/", len(CASES))
