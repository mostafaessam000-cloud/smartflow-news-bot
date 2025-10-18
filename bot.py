# bot.py — NASDAQ news bot (Gemini 1.5 Pro, headline-first, 5 newest per cycle)
import os, re, time, json, hashlib, feedparser, requests
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from email.utils import parsedate_to_datetime

# ───────────────────────── Timezone ─────────────────────────
try:
    from zoneinfo import ZoneInfo
    EST = ZoneInfo("America/New_York")
    _tz_label = "EST"
except Exception:
    EST = timezone.utc
    _tz_label = "UTC"

# ───────────────────────── Env ─────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SLEEP_SECONDS       = int(os.getenv("SLEEP_SECONDS", "120"))
MAX_POSTS_PER_CYCLE = int(os.getenv("MAX_POSTS_PER_CYCLE", "5"))
MAX_AGE_HOURS       = int(os.getenv("MAX_AGE_HOURS", "6"))
MIN_CONFIDENCE      = int(os.getenv("MIN_CONFIDENCE", "0"))
HIDE_NEUTRAL        = os.getenv("HIDE_NEUTRAL", "false").lower() in ("1","true","yes")
DEBUG_REASON        = os.getenv("DEBUG_REASON", "false").lower() in ("1","true","yes")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
# Pro model = better sentiment + reasoning
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-1.5-pro")

# ───────────────────────── Feeds (CNN removed) ─────────────────────────
DEFAULT_FEEDS = [
    "https://www.reuters.com/markets/us/rss",
    "https://www.reuters.com/markets/earnings/rss",
    "https://www.reuters.com/technology/rss",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "https://www.marketwatch.com/rss/topstories",
    "https://finance.yahoo.com/news/rssindex",
    "https://feeds.bbci.co.uk/news/business/rss.xml",
    "https://apnews.com/hub/business?output=rss",
    "https://www.cbsnews.com/moneywatch/rss/",
    "https://abcnews.go.com/abcnews/moneyheadlines",
    "https://www.theguardian.com/us/business/rss",
]
FEEDS = [u.strip() for u in os.getenv("FEEDS", ",".join(DEFAULT_FEEDS)).split(",") if u.strip()]

# ───────────────────────── Gemini (AI) ─────────────────────────
import google.generativeai as genai
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# Version-agnostic safety config to stop false blocks
GEMINI_SAFETY = [
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUAL_CONTENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]

# ───────────────────────── Dedup store ─────────────────────────
SEEN_PATH  = "seen.json"
SEEN_LIMIT = 6000

def load_seen():
    try:
        if os.path.exists(SEEN_PATH):
            data = json.load(open(SEEN_PATH, "r", encoding="utf-8"))
            return set(data if isinstance(data, list) else [])
    except Exception:
        pass
    return set()

def save_seen(s: set):
    if len(s) > SEEN_LIMIT:
        s = set(list(s)[-SEEN_LIMIT:])
    json.dump(list(s), open(SEEN_PATH, "w", encoding="utf-8"))

seen = load_seen()

# ───────────────────────── Helpers ─────────────────────────
_norm_re = re.compile(r"[^\w\s]")

def normalize_title(t: str) -> str:
    t = (t or "").strip().lower()
    t = _norm_re.sub(" ", t)
    t = re.sub(r"\s+", " ", t)
    return t

def make_uid(title: str) -> str:
    return hashlib.sha1(normalize_title(title).encode("utf-8")).hexdigest()

def html_escape(s: str) -> str:
    return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def send_message(text: str):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID): return
    try:
        requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            params={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
                    "disable_web_page_preview": True},
            timeout=15,
        )
    except Exception as e:
        print("telegram:", e)

def publisher_from_link(link: str, fallback: str) -> str:
    try:
        host = urlparse(link).netloc.lower()
        dom = ".".join(host.split(".")[-2:]) if "." in host else host
        LABELS = {
            "reuters.com": "Reuters", "cnbc.com": "CNBC", "marketwatch.com": "MarketWatch",
            "nasdaq.com": "Nasdaq", "finance.yahoo.com": "Yahoo Finance", "yahoo.com": "Yahoo Finance",
            "apnews.com": "AP News", "theguardian.com": "The Guardian", "cbsnews.com": "CBS News / MoneyWatch",
            "abcnews.go.com": "ABC News", "bbc.com": "BBC", "bbc.co.uk": "BBC",
        }
        return LABELS.get(dom, fallback)
    except Exception:
        return fallback

def format_sentiment(ai: dict) -> str:
    s = (ai.get("sentiment") or "Neutral").title()
    try: conf = int(float(ai.get("confidence", 60)))
    except: conf = 60
    if s == "Bullish": return f"🟢⬆️ <b>NASDAQ Bullish</b> ({conf}%)"
    if s == "Bearish": return f"🔴⬇️ <b>NASDAQ Bearish</b> ({conf}%)"
    return f"🟨 NASDAQ Neutral ({conf}%)"

def published_dt_from_entry(entry) -> datetime | None:
    for attr in ("published_parsed","updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try: return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception: pass
    for attr in ("published","updated","created"):
        s = getattr(entry, attr, None)
        if s:
            try:
                dt = parsedate_to_datetime(s)
                if not dt.tzinfo: dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except Exception: pass
    return None

def human_ago(delta: timedelta) -> str:
    m = int(delta.total_seconds() // 60)
    if m < 1: return "just now"
    if m < 90: return f"{m} min ago"
    h = m // 60
    return f"{h} hr ago"

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36"}

def extract_article_text(url: str) -> str:
    if not url: return ""
    try:
        r = requests.get(url, headers=UA, timeout=12)
        if r.status_code in (401,403): return ""
        r.raise_for_status()
        full = BeautifulSoup(r.text, "html.parser")
        ps = [p.get_text(" ", strip=True) for p in full.find_all("p")]
        long_ps = [p for p in ps if len(p) >= 80]
        return " ".join(long_ps)[:5000]
    except Exception:
        return ""

# ───────────────────────── Keyword fallback (market impact) ─────────────────────────
# These are tuned for NASDAQ direction when headlines are all you have.
BULLISH_KW = [
    # macro/monetary
    "cut rates","rate cut","pivot","dovish","yields fall","yield falls","inflation cools","cpi cools",
    "disinflation","soft landing","stimulus","qe",
    # earnings/guidance
    "beat","beats","tops estimates","above estimates","raises guidance","raise guidance","hikes outlook",
    "profit jumps","revenue surges","record high","upgrade","upgrades","initiated buy",
    # tech/ai/semis tailwinds
    "ai boom","chip demand","data center demand","contract award","sec approves etf","antitrust case dropped",
    # geopolitics relief
    "ceasefire","deal reached","tariff relief","sanctions lifted",
    # price action language
    "surges","rallies","soars","spikes higher","jumps","gains",
]
BEARISH_KW = [
    # macro/monetary
    "hike rates","rate hike","higher for longer","hawkish","yields jump","yield spikes","hot inflation","cpi heats",
    "recession","hard landing",
    # earnings/guidance
    "miss","misses","below estimates","cuts guidance","lower outlook","warns","profit slumps","revenue declines",
    "downgrade","downgrades","cuts to sell",
    # regulation/geopolitics/ban
    "antitrust probe","ftc sues","doj sues","sec sues","ban","bans","sanction","tariff","export control",
    # negative events
    "recall","layoffs","strike","shutdown","data breach","investigation",
    # price action language
    "tumbles","plunges","sinks","spikes lower","slides","slumps","drops",
]

def keyword_direction(title: str, ctx: str = "") -> str | None:
    t = f"{title}. {ctx}".lower()
    bull = any(k in t for k in BULLISH_KW)
    bear = any(k in t for k in BEARISH_KW)
    if bull and not bear: return "Bullish"
    if bear and not bull: return "Bearish"
    return None

def _trim_words(s: str, n: int = 20) -> str:
    return " ".join((s or "").strip().split()[:n])

# ───────────────────────── AI classify (headline-first, retry, keyword fallback) ─────────────────────────
def ai_classify(title: str, source: str, article_text: str):
    """
    -> {"summary": "<=20 words or ''", "sentiment": Bullish|Bearish|Neutral, "confidence": 0-100}
    If Gemini returns Neutral/empty, we use market-focused keyword fallback for direction only.
    """
    if not GEMINI_API_KEY:
        # no model → use keywords only
        kd = keyword_direction(title, article_text or "")
        sent = kd if kd else "Neutral"
        conf = 75 if kd else 60
        return {"summary": "", "sentiment": sent, "confidence": conf}

    headline = (title or "").strip()
    ctx = (article_text or "").strip()

    system = (
        "You are a market analyst. Using the HEADLINE (and context if any), "
        "classify the next 1–3 session impact on NASDAQ.\n"
        'Return STRICT JSON only: {"summary":"<=20 words (may be empty)",'
        '"sentiment":"Bullish|Bearish|Neutral","confidence":0-100}. '
        "Prefer Bullish/Bearish if any directional cue (beats/misses, guidance, rates, regulation, war/tariffs/chip bans)."
    )
    user = f"Headline: {headline}\nContext: {ctx[:2000] if ctx else '(none)'}\nJSON only."

    # try permissive -> empty -> omitted safety
    tries = [GEMINI_SAFETY, [], None]
    raw = ""
    for safety in tries:
        try:
            model = genai.GenerativeModel(GEMINI_MODEL)
            res = model.generate_content(
                [{"text": system}, {"text": user}],
                generation_config={
                    "temperature": 0.1,
                    "max_output_tokens": 256,
                    "response_mime_type": "application/json",
                },
                **({"safety_settings": safety} if safety is not None else {}),
            )
            raw = (res.text or "").strip()
            start, end = raw.find("{"), raw.rfind("}")
            data = json.loads(raw[start:end+1]) if start != -1 and end != -1 else {}
            norm = {str(k).lower(): v for k, v in (data or {}).items()}
            summary = _trim_words((norm.get("summary") or "").strip(), 20)
            sentiment = str(norm.get("sentiment", "Neutral")).title()
            try: conf = int(float(norm.get("confidence", 60)))
            except: conf = 60
            # normalize
            s = sentiment.lower()
            if "bull" in s: sentiment = "Bullish"
            elif "bear" in s: sentiment = "Bearish"
            else: sentiment = "Neutral"
            if summary.lower() in {"bullish","bearish","neutral", headline.lower()}:
                summary = ""
            # If still neutral → apply keyword fallback for direction only
            if sentiment == "Neutral":
                kd = keyword_direction(headline, ctx)
                if kd:
                    sentiment = kd
                    conf = max(conf, 75)
            return {"summary": summary, "sentiment": sentiment, "confidence": conf}
        except Exception as e:
            if DEBUG_REASON: print("AI error (Gemini):", getattr(e, "message", str(e))[:160])
            time.sleep(1)

    # total failure → keyword only
    kd = keyword_direction(headline, ctx)
    sent = kd if kd else "Neutral"
    conf = 75 if kd else 60
    if DEBUG_REASON and raw: print("AI parse fail. Raw:", raw[:200])
    return {"summary": "", "sentiment": sent, "confidence": conf}

# ───────────────────────── Feed fetch ─────────────────────────
def parse_feed(url: str):
    try:
        r = requests.get(url, headers={"User-Agent": UA["User-Agent"]}, timeout=15)
        if r.ok and r.content:
            return feedparser.parse(r.content)
    except Exception as e:
        print("feed error:", url, e)
    return None

# ───────────────────────── Main cycle (newest 5) ─────────────────────────
def fetch_once(limit_per_feed=12):
    global seen
    items = []
    titles_seen_cycle = set()

    # 1) gather
    for url in FEEDS:
        feed = parse_feed(url)
        if not feed: continue
        src = (getattr(getattr(feed, "feed", None), "title", None) or url).strip()
        for e in getattr(feed, "entries", [])[:limit_per_feed]:
            title = (getattr(e, "title", "") or "").strip()
            link  = (getattr(e, "link", "") or "").strip()
            if not title: continue
            norm_t = normalize_title(title)
            if norm_t in titles_seen_cycle:  # cross-source de-dup this cycle
                continue
            titles_seen_cycle.add(norm_t)
            dt = published_dt_from_entry(e) or datetime.now(timezone.utc)
            items.append({"src": src, "title": title, "link": link, "dt": dt, "entry": e})

    # 2) newest first
    items.sort(key=lambda x: x["dt"], reverse=True)

    # 3) post first N new items
    posted = 0
    now_utc = datetime.now(timezone.utc)

    for it in items:
        uid = make_uid(it["title"])
        if uid in seen:  # already sent before
            continue
        if (now_utc - it["dt"]) > timedelta(hours=MAX_AGE_HOURS):
            if DEBUG_REASON: print("skip old:", it["title"][:120])
            continue

        # Try article; OK if empty (headline-only)
        article = extract_article_text(it["link"])
        ai = ai_classify(it["title"], it["src"], article)

        if HIDE_NEUTRAL and ai["sentiment"] == "Neutral":
            if DEBUG_REASON: print("skip neutral:", it["title"][:120])
            continue
        if ai["confidence"] < MIN_CONFIDENCE:
            if DEBUG_REASON: print("skip low conf:", ai["confidence"], it["title"][:120])
            continue

        dt_est = it["dt"].astimezone(EST)
        ago = human_ago(now_utc - it["dt"])
        when = f"{dt_est.strftime('%-I:%M %p ')}{_tz_label} • {dt_est.strftime('%b %-d')} ({ago})"

        nice_src = publisher_from_link(it["link"], it["src"])
        src_line = (f'🔗 Source: <a href="{html_escape(it["link"])}">{html_escape(nice_src)}</a>'
                    if it["link"] else f"🔗 Source: {html_escape(nice_src)}")

        summary = (ai.get("summary") or "").strip()  # may be empty by design
        summary_line = f"✍️ {html_escape(summary)}\n" if summary else ""

        msg = (
            f"{format_sentiment(ai)}\n"
            f"📰 {html_escape(it['title'])}\n"
            f"{summary_line}"
            f"{src_line}\n"
            f"🕒 {html_escape(when)}"
        )
        send_message(msg)
        seen.add(uid)
        posted += 1
        if posted >= MAX_POSTS_PER_CYCLE:
            break
        time.sleep(1)

    if posted:
        save_seen(seen)

def main():
    send_message("✅ NASDAQ bot online (Gemini Pro • headline-first • ≤20 words • newest 5).")
    while True:
        try:
            fetch_once()
        except Exception as e:
            print("loop error:", e)
        time.sleep(SLEEP_SECONDS)

if __name__ == "__main__":
    main()
