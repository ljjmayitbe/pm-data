# -*- coding: utf-8 -*-
"""
Polymarket 事件概率抓取（在 GitHub Actions 的境外机器上运行）
输出 data/latest.json + data/history/YYYY-MM-DD_HHMM.json
"""
import os, sys, json, time, re
from datetime import datetime, timezone

import requests

GAMMA = "https://gamma-api.polymarket.com"
UA = {"User-Agent": "pm-monitor/1.0 (+github actions)"}
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# 白名单式分类：只保留明确跟金融决策相关的事件，其余（体育/电竞/娱乐）直接丢弃
GROUPS = [
    ("fed", "美联储与利率", [
        "fed ", "fed'", "fomc", "federal reserve", "powell", "rate cut", "rate hike",
        "interest rate", "basis point", "bps ", "rate decision"]),
    ("infl", "通胀与就业", [
        "cpi", "inflation", "pce ", "unemployment", "jobless", "payroll",
        "recession", "gdp", "jobs report", "economic data"]),
    ("equity", "美股与科技", [
        "nvidia", "openai", "anthropic", "apple", "tesla", "microsoft", "alphabet",
        "amazon", "meta platforms", "s&p", "nasdaq", "dow jones", "market cap",
        " ipo", "ipo ", "earnings", "largest company", "ai bubble", "stock market",
        "ceo of", "valuation"]),
    ("commodity", "大宗商品", [
        "gold", "silver", "oil", "wti", "brent", "crude", "copper",
        "natural gas", "opec", "xauusd", "wheat", "corn", "commodity"]),
    ("geo", "地缘与政策", [
        "china", "taiwan", "russia", "ukraine", "tariff", "iran", "nuclear deal",
        "hormuz", "sanction", "trade deal", "shutdown", "invade", "ceasefire"]),
]

# 命中即判为噪声（即使命中了上面的关键词，体育类的奇怪措辞也先挡掉）
NOISE = [
    "nba", "nfl", "nhl", "mlb", "ufc", "lol:", "cs:", "dota", "counter-strike",
    "champions league", "premier league", "la liga", "serie a", "big bash",
    "grand prix", "superbowl", "playoff", "rookie of the year", "most valuable player",
    " vs. ", " vs ", "gta", "oscar", "grammy", "eurovision", "tweets",
    "divorce", "arrested", "jail", "dating", "married",
]


def log(*a):
    print(*a, flush=True)


def get(path, params, tries=3):
    for i in range(tries):
        try:
            r = requests.get(GAMMA + path, params=params, headers=UA, timeout=40)
            if r.status_code == 200:
                return r.json()
            log("  HTTP %s %s" % (r.status_code, r.text[:160]))
        except Exception as e:
            log("  ERR %s" % e)
        time.sleep(3)
    return None


def jlist(x):
    if isinstance(x, list):
        return x
    try:
        v = json.loads(x) if x else []
        return v if isinstance(v, list) else []
    except Exception:
        return []


def num(d, *keys):
    """按候选键取第一个有效数字"""
    for k in keys:
        v = d.get(k)
        if v is None or v == "":
            continue
        try:
            f = float(v)
            if f == f:  # 非 NaN
                return f
        except Exception:
            continue
    return None


DATE_RE = r"(20\d\d|january|february|march|april|june|july|august|september|october|november|december)"


def classify(text):
    """返回 (group_id, group_name)；None 表示不跟金融决策相关，丢弃"""
    t = " " + text.lower() + " "
    for kw in NOISE:
        if kw in t:
            return None, None
    for gid, gname, kws in GROUPS:
        for kw in kws:
            if kw in t:
                return gid, gname
    return None, None


def detect_kind(ms, title=""):
    """dist=互斥档位(概率和≈100)  ladder=按截止日/阈值的累计概率  single=单一二项"""
    if len(ms) <= 1:
        return "single"
    probs = [m["prob"] for m in ms if m["prob"] is not None]
    if len(probs) < 2:
        return "single"
    total = sum(probs)
    t = " " + title.lower() + " "
    # 「... by <日期>」句式 = 到某日之前发生的累计概率
    if (" by " in t or "by..." in t or "by…" in t or "before " in t) and len(probs) >= 2:
        return "ladder"
    # 概率和远超 100% 说明是「超过某阈值」的累计
    if total > 130:
        return "ladder"
    return "dist"


# Polymarket 有一类未公开选项用占位符，价格恒为 50%，是噪声不是信号
PLACEHOLDER = re.compile(r'^(candidate|company|option|team|person|entity|model)\s+[a-z0-9]{1,2}$', re.I)


def parse_market(m, event_title):
    outs = jlist(m.get("outcomes"))
    prices = jlist(m.get("outcomePrices"))
    prob = None
    label = ""
    if outs and prices and len(outs) == len(prices):
        # 优先取 Yes
        for o, p in zip(outs, prices):
            if str(o).strip().lower() == "yes":
                try:
                    prob = float(p) * 100
                    label = "Yes"
                except Exception:
                    pass
                break
        if prob is None:
            try:
                prob = float(prices[0]) * 100
                label = str(outs[0])
            except Exception:
                pass
    return {
        "q": (m.get("question") or m.get("title") or event_title or "").strip(),
        "sub": (m.get("groupItemTitle") or "").strip(),
        "prob": round(prob, 2) if prob is not None else None,
        "label": label,
        "vol24": num(m, "volume24hr", "volume24Hr", "volume_24hr"),
        "vol": num(m, "volumeNum", "volume"),
        "liq": num(m, "liquidityNum", "liquidity"),
        "chg1d": num(m, "oneDayPriceChange", "oneDayPriceChangePct"),
        "chg1w": num(m, "oneWeekPriceChange"),
        "ends": (m.get("endDate") or m.get("endDateIso") or "")[:10],
        "closed": bool(m.get("closed")),
    }


def main():
    os.makedirs(os.path.join(OUT_DIR, "history"), exist_ok=True)
    events, seen = [], set()

    for page in range(4):
        d = get("/events", {
            "limit": 100, "offset": page * 100,
            "active": "true", "closed": "false",
            "order": "volume24hr", "ascending": "false",
        })
        if not d:
            log("page %d 无返回，停止" % page)
            break
        if isinstance(d, dict):
            d = d.get("data") or d.get("events") or []
        log("page %d: %d events" % (page, len(d)))
        for e in d:
            eid = e.get("id")
            if eid in seen:
                continue
            seen.add(eid)
            events.append(e)
        if len(d) < 100:
            break
        time.sleep(0.5)

    if not events:
        log("FATAL: 一个 event 都没拿到")
        return 1

    # 第一次跑时留一份原始样本，供核对字段名（只存 2 个，避免仓库膨胀）
    if os.environ.get("DUMP_RAW") == "1":
        with open(os.path.join(OUT_DIR, "_raw_sample.json"), "w", encoding="utf-8") as f:
            json.dump(events[:2], f, ensure_ascii=False, indent=1)

    out = []
    dropped = 0
    for e in events:
        title = (e.get("title") or "").strip()
        gid, gname = classify(title)
        if not gid:
            dropped += 1
            continue
        mkts = []
        for m in (e.get("markets") or []):
            if m.get("closed"):
                continue
            pm = parse_market(m, title)
            if pm["prob"] is None:
                continue
            if pm.get("sub") and PLACEHOLDER.match(pm["sub"].strip()):
                continue  # 未公开选项的 50% 占位值
            mkts.append(pm)
        if not mkts:
            continue
        kind = detect_kind(mkts, title)
        if kind == "dist":
            mkts.sort(key=lambda x: -(x["prob"] or 0))
        # ladder / single 保持接口原始顺序（即日期或阈值升序）
        out.append({
            "id": e.get("id"),
            "title": title,
            "slug": e.get("slug") or "",
            "group": gid,
            "groupName": gname,
            "kind": kind,
            "vol24": num(e, "volume24hr", "volume24Hr"),
            "vol": num(e, "volumeNum", "volume"),
            "liq": num(e, "liquidityNum", "liquidity"),
            "ends": (e.get("endDate") or "")[:10],
            "markets": mkts[:12],
        })

    log("过滤：保留 %d，丢弃噪声 %d" % (len(out), dropped))
    out.sort(key=lambda x: -(x["vol24"] or 0))

    now = datetime.now(timezone.utc)
    snap = {
        "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ts_local": (now.astimezone().strftime("%Y-%m-%d %H:%M")),
        "source": "polymarket-gamma",
        "count_events": len(out),
        "events": out,
    }

    with open(os.path.join(OUT_DIR, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, separators=(",", ":"))

    # 每天一个历史文件（当日覆盖），保留 45 天，供看板算概率漂移
    hdir = os.path.join(OUT_DIR, "history")
    with open(os.path.join(hdir, now.strftime("%Y-%m-%d") + ".json"), "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, separators=(",", ":"))
    cutoff = time.time() - 45 * 86400
    for fn in os.listdir(hdir):
        fp = os.path.join(hdir, fn)
        if fn.endswith(".json") and os.path.getmtime(fp) < cutoff:
            os.remove(fp)

    log("OK events=%d markets=%d" % (len(out), sum(len(x["markets"]) for x in out)))
    for x in out[:8]:
        m0 = x["markets"][0]
        log("  [%s] %-52s %5.1f%%  vol24=%s" % (
            x["groupName"], x["title"][:52], m0["prob"],
            ("%.0f" % m0["vol24"]) if m0["vol24"] else "-"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
