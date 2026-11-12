# -*- coding: utf-8 -*-
"""
Polymarket 事件概率抓取（在 GitHub Actions 的境外机器上运行）
输出 data/latest.json + data/history/YYYY-MM-DD_HHMM.json
"""
import os, sys, json, time
from datetime import datetime, timezone

import requests

GAMMA = "https://gamma-api.polymarket.com"
UA = {"User-Agent": "pm-monitor/1.0 (+github actions)"}
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# 分类关键词（命中即归类，顺序即优先级）
GROUPS = [
    ("fed",   "美联储与利率", ["fed ", "federal reserve", "fomc", "powell", "rate cut",
                             "rate hike", "interest rate", "basis point", "bps"]),
    ("macro", "通胀与就业",   ["cpi", "inflation", "unemployment", "jobs report", "payroll",
                             "recession", "gdp", "pce", "jobless"]),
    ("ai",    "美股与 AI",    ["nvidia", "openai", "anthropic", "apple", "tesla", "microsoft",
                             "alphabet", "amazon", "meta ", "s&p", "nasdaq", "earnings",
                             "ipo ", "market cap", "stock"]),
    ("cmdty", "大宗商品",     ["gold", "silver", "oil", "crude", "copper", "opec",
                             "natural gas", "wheat", "corn"]),
    ("geo",   "地缘与政策",   ["china", "taiwan", "russia", "ukraine", "tariff", "war ",
                             "invade", "ceasefire", "sanction", "election", "trump",
                             "government shutdown"]),
    ("crypto", "加密",        ["bitcoin", "ethereum", "btc", "eth ", "crypto", "solana",
                             "stablecoin", "xrp"]),
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


def classify(text):
    t = " " + text.lower() + " "
    for gid, gname, kws in GROUPS:
        for kw in kws:
            if kw in t:
                return gid, gname
    return "other", "其他"


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
    for e in events:
        title = (e.get("title") or "").strip()
        desc = (e.get("description") or "")[:400]
        gid, gname = classify(title + " " + desc)
        mkts = []
        for m in (e.get("markets") or []):
            if m.get("closed"):
                continue
            pm = parse_market(m, title)
            if pm["prob"] is None:
                continue
            mkts.append(pm)
        if not mkts:
            continue
        mkts.sort(key=lambda x: -(x["vol24"] or 0))
        out.append({
            "id": e.get("id"),
            "title": title,
            "slug": e.get("slug") or "",
            "group": gid,
            "groupName": gname,
            "vol24": num(e, "volume24hr", "volume24Hr"),
            "vol": num(e, "volumeNum", "volume"),
            "liq": num(e, "liquidityNum", "liquidity"),
            "ends": (e.get("endDate") or "")[:10],
            "commentCount": e.get("commentCount"),
            "markets": mkts,
        })

    out = [x for x in out if x["group"] != "crypto"]
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
