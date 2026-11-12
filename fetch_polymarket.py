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


MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june",
     "july", "august", "september", "october", "november", "december"])}
RE_DATE = re.compile(r'(january|february|march|april|may|june|july|august|september|'
                     r'october|november|december)\s+(\d{1,2})(?:,?\s*(\d{4}))?', re.I)


def date_key(s):
    """把 'December 31' / 'March 31, 2027' 这类档位名转成可排序的 (年,月,日)。
    没有年份的按『未来最近一次』推断（月份早于当月就算明年）。"""
    m = RE_DATE.search(s or "")
    if not m:
        return (9999, 13, 32)
    mon = MONTHS[m.group(1).lower()]
    day = int(m.group(2))
    if m.group(3):
        yr = int(m.group(3))
    else:
        now = time.gmtime()
        yr = now.tm_year + (1 if mon < now.tm_mon else 0)
    return (yr, mon, day)


# Polymarket 有一类未公开选项用占位符，价格恒为 50%，是噪声不是信号
PLACEHOLDER = re.compile(r'^(candidate|company|option|team|person|entity|model)\s+[a-z0-9]{1,2}$', re.I)

MON_CN = {'january': '1月', 'february': '2月', 'march': '3月', 'april': '4月',
          'may': '5月', 'june': '6月', 'july': '7月', 'august': '8月',
          'september': '9月', 'october': '10月', 'november': '11月', 'december': '12月'}


def zh_date(s):
    """'December 31' -> '12月31日'；'March 31, 2027' -> '2027年3月31日'"""
    m = RE_DATE.search(s or "")
    if not m:
        return s
    mon = MON_CN[m.group(1).lower()]
    day = m.group(2)
    if m.group(3):
        return '%s年%s%s日' % (m.group(3), mon, day)
    return '%s%s日' % (mon, day)


def zh_sub(s):
    """概率档位名中文化：优先规则（术语固定，比机翻准），识别不了就返回原文"""
    t = (s or '').strip()
    if not t:
        return t
    # 纯日期
    if re.fullmatch(r'[A-Za-z]+ \d{1,2}(,? \d{4})?', t):
        return zh_date(t)
    # 基点
    t2 = re.sub(r'(\d+)\+\s*bps\s+increase', r'加息\1基点以上', t, flags=re.I)
    t2 = re.sub(r'(\d+)\+\s*bps\s+decrease', r'降息\1基点以上', t2, flags=re.I)
    t2 = re.sub(r'(\d+)\s*bps\s+increase', r'加息\1个基点', t2, flags=re.I)
    t2 = re.sub(r'(\d+)\s*bps\s+decrease', r'降息\1个基点', t2, flags=re.I)
    if t2 != t:
        return t2
    low = t.lower()
    fixed = {
        'no change': '不变', 'yes': '是', 'no': '否',
        'resigned/removed; over $4t': '辞职/免职 且 市值>4T',
        'resigned/removed; under $4t': '辞职/免职 且 市值<4T',
        'not resigned/removed; over $4t': '未辞职 且 市值>4T',
        'not resigned/removed; under $4t': '未辞职 且 市值<4T',
    }
    if low in fixed:
        return fixed[low]
    return t


# 阶梯型标题常以 "by...?" / "through...?" 结尾，机翻会把 by 译成"通过"，语义全错。
# 做法：先剥掉这个尾巴，只译主干，再自己补回中文后缀。
TAIL_RE = re.compile(r'\s+(by|through|on|before)\s*[\.。…_]*\s*\??\s*\??$', re.I)
TAIL_CN = {'by': '（什么时候？）', 'before': '（什么时候之前？）',
           'through': '（能持续到什么时候？）', 'on': '（哪一天？）'}


def strip_tail(t):
    m = TAIL_RE.search(t)
    if m:
        return t[:m.start()].rstrip(), TAIL_CN.get(m.group(1).lower(), '')
    return t, ''


CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'zh_cache.json')

# 高频事件的固定译法（机翻对同一句式译法不统一，这里人工钉死）
OVERRIDE = {
    'Fed Decision in December?': '美联储 12 月利率决议',
    'Fed Decision in January?': '美联储 1 月利率决议',
    'Fed Decision in March?': '美联储 3 月利率决议',
    'How many Fed rate cuts in 2026?': '2026 年美联储降息几次',
    'How many Fed rate hikes in 2026?': '2026 年美联储加息几次',
    'What will the Fed rate be at the end of 2026?': '2026 年底美联储利率到多少',
    'Another Fed rate hike in 2026?': '2026 年会不会再加息一次',
    'Largest Company end of December 2026?': '2026 年 12 月底全球最大市值公司',
    'Largest Company end of November 2026?': '2026 年 11 月底全球最大市值公司',
    '3rd Largest Company end of December 2026?': '2026 年 12 月底全球第三大市值公司',
    'AI bubble burst by...?': 'AI 泡沫何时破裂',
    'Anthropic IPO Closing Market Cap': 'Anthropic IPO 收盘市值',
    'Anthropic IPO Closing Market Cap (Middle Brackets)': 'Anthropic IPO 收盘市值（中间档）',
    "Will Anthropic's valuation hit __ by December 31?": 'Anthropic 估值年底前能突破多少',
    'What will Anthropic\'s IPO valuation be?': 'Anthropic IPO 估值落在哪个区间',
    'How much will Anthropic raise in its IPO?': 'Anthropic IPO 募资多少',
    'Anthropic IPO by __?': 'Anthropic 什么时候 IPO',
    'Anthropic IPO Date': 'Anthropic IPO 具体日期',
    'OpenAI IPO Closing Market Cap': 'OpenAI IPO 收盘市值',
    'Next CEO of NVIDIA?': '英伟达下一任 CEO 是谁',
    "Nvidia's Market Cap end of 2026?": '2026 年底英伟达市值',
    'What will NVIDIA (NVDA) hit in November 2026?': '11 月英伟达股价能到多少',
    'What will NVIDIA (NVDA) hit Week of November 9 2026?': '本周英伟达股价能到多少',
    'Will NVIDIA (NVDA) close above ___ end of November?': '11 月底英伟达能收在多少之上',
    'What will Gold (XAUUSD) hit in November 2026?': '11 月黄金能到多少',
    'What will Gold (GC) hit__ by end of December?': '年底前黄金能到多少',
    'What will WTI Crude Oil (WTI) hit in November 2026?': '11 月 WTI 原油能到多少',
    'Crude Oil all time high by...?': '原油何时创历史新高',
    'Will China invade Taiwan by December 31, 2027?': '中国会不会在 2027 年底前武统台湾',
    'Will China invade Taiwan by June 30, 2027?': '中国会不会在 2027 年 6 月底前武统台湾',
    'Will China invade Taiwan by end of 2026?': '中国会不会在 2026 年底前武统台湾',
    '2026 Taiwanese Local Elections: Party Winner': '2026 台湾地方选举哪个党赢',
    'IPOs before 2027?': '2027 年前有哪些公司 IPO',
    'Largest IPO by market cap in 2026?': '2026 年最大 IPO 是哪家',
    'Jensen Huang resigned or removed & Nvidia above $4T end of 2026?': '黄仁勋去职 × 英伟达年底市值能否守住 4 万亿',
    'Will the U.S. invade Iran before 2027?': '美国会不会在 2027 年前入侵伊朗',
    'US announces end of Iranian blockade by...?': '美国何时宣布解除对伊朗封锁',
    'Strait of Hormuz traffic returns to normal by December 31?': '霍尔木兹海峡年底前能否恢复正常通航',
    'Strait of Hormuz traffic returns to normal by November 30?': '霍尔木兹海峡 11 月底前能否恢复正常通航',
}


def translate_titles(titles):
    """标题机翻（MyMemory 免费接口），结果永久缓存，只在遇到新标题时才请求"""
    cache = {}
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, encoding='utf-8') as f:
                cache = json.load(f)
        except Exception:
            cache = {}
    todo = [t for t in titles if t and t not in cache]
    quota_dead = False
    for t in todo:
        if quota_dead:
            break
        try:
            r = requests.get('https://api.mymemory.translated.net/get',
                             params={'q': t[:480], 'langpair': 'en|zh-CN'},
                             headers=UA, timeout=15)
            j = r.json()
            zh = ((j.get('responseData') or {}).get('translatedText') or '').strip()
            if 'MYMEMORY WARNING' in zh.upper() or 'QUOTA' in zh.upper():
                quota_dead = True
                log('  翻译额度用尽，剩余条目保留英文')
                continue
            if zh:
                cache[t] = zh
        except Exception:
            pass
        time.sleep(0.25)
    if todo:
        try:
            with open(CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(cache, f, ensure_ascii=False, indent=1)
            log('  翻译：新增 %d 条，缓存共 %d 条' % (len(todo), len(cache)))
        except Exception as e:
            log('  翻译缓存写入失败 %s' % e)
    return cache


# 同一件事常被拆成多个到期日的市场（12月决议/1月决议/3月决议），
# 或同一标的的多个问法（市值/日期/募资额）。按主题聚类后只保留最近一期。
TOPICS = [
    # 美联储：决议 / 降息次数 / 加息次数 / 利率水平 各算一件
    ('fed_decision', ['fed decision']),
    ('fed_cuts',     ['rate cut']),
    ('fed_hikes',    ['rate hike']),
    ('fed_level',    ['fed rate be', 'fed rate at the end']),
    # 公司：IPO 的问法与市值/其他分开，避免把 "IPO 哪天" 和 "收盘市值" 混成一件事
    ('anthropic_ipo', ['anthropic ipo', 'anthropic’s ipo', "anthropic's ipo",
                       'anthropic raise', 'anthropic’s valuation', "anthropic's valuation"]),
    ('anthropic_other', ['anthropic']),
    ('openai_ipo',   ['openai']),
    ('nvidia_price', ['nvidia (nvda)', 'nvda)', 'nvda ']),
    ('nvidia_other', ['nvidia']),
    ('largest_company', ['largest company']),
    ('largest_ipo',  ['largest ipo', 'ipos before']),
    # 大宗
    ('gold',         ['gold']),
    ('wti',          ['wti']),
    ('crude_other',  ['crude oil', 'oil pipeline']),
    # 地缘：按「事件类型」聚类，不按国家，避免把停火、封锁、选举混为一谈
    ('hormuz_shipping', ['shipping successfully targeted', 'iran target oman']),
    ('hormuz_traffic',  ['hormuz traffic', 'hormuz agreement', 'hormuz management']),
    ('iran_blockade',   ['iranian blockade']),
    ('iran_nuclear',    ['nuclear deal']),
    ('iran_invade',     ['invade iran']),
    ('iran_airspace',   ['airspace closure']),
    ('iran_ceasefire',  ['iran ceasefire', 'israel x iran', 'effective ceasefire']),
    ('taiwan_invasion', ['invade taiwan']),
    ('taiwan_election', ['taiwanese local elections', 'taiwan']),
    ('russia_diesel',   ['russian diesel']),
    ('russia_energy',   ['energy ceasefire']),
    ('russia_other',    ['russia', 'ukraine', 'kyiv', 'nato']),
    ('ai_bubble',       ['ai bubble']),
    ('trump_china',     ['trump leave china']),
    ('shutdown',        ['government shutdown']),
]


def topic_of(title):
    """给事件分配主题 id；返回 None 表示这是独立事件，不参与聚类去重"""
    t = ' ' + title.lower() + ' '
    for tid, keys in TOPICS:
        if any(k in t for k in keys):
            return tid
    return None
def is_relevant(gid, title):
    if gid in ('fed', 'equity', 'commodity'):
        return True
    t = ' ' + title.lower() + ' '
    # 自然灾害/天气类：与 A 股投资决策无关
    if any(k in t for k in ('cyclone', 'hurricane', 'earthquake', 'typhoon', 'wildfire')):
        return False
    # 俄罗斯/乌克兰/北约：只有牵涉能源才留
    if any(k in t for k in ('russia', 'ukraine', 'kyiv', 'putin', 'nato')):
        return any(k in t for k in (' oil', 'diesel', 'energy', 'gas'))
    # 纯政治人物/外交日程：删
    if any(k in t for k in ('regime fall', 'leadership change', 'diplomatic meeting',
                            'iran leader end', 'what will be in a us-iran deal')):
        return False
    # 中国/台海：留
    if any(k in t for k in ('china', 'taiwan', 'chinese')):
        return True
    # 伊朗/霍尔木兹/原油供应链：留（直接影响油价 → A股石化）
    if any(k in t for k in ('iran', 'hormuz', 'oil', 'crude', 'opec',
                            'blockade', 'pipeline', 'airspace', 'diesel')):
        return True
    return False


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
    irrelevant = 0
    for e in events:
        title = (e.get("title") or "").strip()
        gid, gname = classify(title)
        if not gid:
            dropped += 1
            continue
        if not is_relevant(gid, title):
            irrelevant += 1
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
        elif kind == "ladder":
            mkts.sort(key=lambda x: date_key(x.get("sub") or ""))
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

    log("过滤：保留 %d，噪声 %d，与A股无关 %d" % (len(out), dropped, irrelevant))

    # 同主题去重：只留「最近一个尚未到期」的那期；已过期的排最后，同日则留下注最大的
    today = datetime.now().strftime("%Y-%m-%d")

    def pick_key(x):
        ends = x.get("ends") or "9999"
        return (0 if ends >= today else 1, ends, -(x.get("vol24") or 0))

    before = len(out)
    picked = {}
    solo = []
    for x in out:
        tid = topic_of(x["title"])
        x["topic"] = tid
        if not tid:
            solo.append(x)
            continue
        cur = picked.get(tid)
        if cur is None or pick_key(x) < pick_key(cur):
            picked[tid] = x
    out = solo + list(picked.values())
    log("同主题去重：%d → %d（合并 %d 个重复主题）" % (before, len(out), before - len(out)))

    out.sort(key=lambda x: -(x["vol24"] or 0))

    # 标题中文化：人工固定译法 > 剥尾巴机翻主干 + 中文后缀
    cores, tails = [], {}
    for x in out:
        if x["title"] in OVERRIDE:
            continue
        core, tail = strip_tail(x["title"])
        tails[x["title"]] = tail
        cores.append(core)
    zh = translate_titles(cores)
    for x in out:
        if x["title"] in OVERRIDE:
            x["title_zh"] = OVERRIDE[x["title"]]
        else:
            core, _ = strip_tail(x["title"])
            base = (zh.get(core) or "").strip()
            x["title_zh"] = (base + tails.get(x["title"], "")) if base else ""
        for m in x["markets"]:
            m["sub_zh"] = zh_sub(m.get("sub") or "")

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
