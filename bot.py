import os, time, json, hashlib, feedparser, requests, html, re
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo
from openai import OpenAI

# ----------------- ENV -----------------
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID  = os.getenv("TELEGRAM_CHAT_ID")
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY")
SLEEP_SECONDS     = int(os.getenv("SLEEP_SECONDS", "60"))
MAX_AGE_HOURS     = float(os.getenv("MAX_AGE_HOURS", "2"))
MAX_POSTS_PER_CYCLE = int(os.getenv("MAX_POSTS_PER_CYCLE", "4"))  # 0 = unlimited
EXTRA_RSS         = os.getenv("EXTRA_RSS", "").strip()

# AI gating (tuned for NASDAQ-only)
IMPACT_MIN        = int(os.getenv("IMPACT_MIN", "70"))
CONF_MIN          = int(os.getenv("CONF_MIN", "60"))
ALLOW_NEUTRAL     = os.getenv("ALLOW_NEUTRAL", "false").lower() in ("1","true","yes")

assert TELEGRAM_TOKEN and TELEGRAM_CHAT_ID and OPENAI_API_KEY, \
    "Missing env vars: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, OPENAI_API_KEY"

client = OpenAI(api_key=OPENAI_API_KEY)

# ----------------- CONSTANTS -----------------

# Forex Factory USD-only calendar (XML)
FF_FEED = "https://www.forexfactory.com/ffcal_week_this.xml"

# If you want a few optional US-market feeds when you’re ready (kept optional):
DEFAULT_EXTRA = [
    # keep empty by default; add via EXTRA_RSS env if you want more firehose later
]

# High-impact prefilter terms (US macro + market movers)
HIGH_IMPACT_TERMS = [
    # Macro prints
    "cpi", "ppi", "pce", "nfp", "payrolls", "jobs", "unemployment", "ism",
    "pmi", "gdp", "retail sales", "core inflation", "inflation",
    # Fed / rates / yields / dollar
    "fed", "powell", "fomc", "rate", "rates", "hike", "cut", "treasury",
    "yield", "10-year", "10yr", "2-year", "dxy", "usd",
    # Mega caps / semis / ai (NASDAQ sensitivity)
    "nvidia", "nvda", "amd", "aapl", "apple", "msft", "meta", "goog", "alphabet",
    "amazon", "amzn", "avgo", "broadcom", "intc", "arm", "semiconductor", "chip",
    "ai", "gpu", "export control", "sanction", "entity list",
    # Energy/geopolitics (can move NASDAQ through rates/liquidity)
    "opec", "iran", "israel", "ukraine", "houthis", "strait"
]

EST = ZoneInfo("America/New_York")
UTC = timezone.utc

SEEN_PATH = "seen.txt"
seen = set()

# ----------------- UTIL -----------------

def load_seen():
    if os.path.exists(SEEN_PATH):
        with open(SEEN_PATH, "r", encoding="utf-8") as f:
            for line in f:
                uid = line.strip()
                if uid:
                    seen.add(uid)

def save_seen(uid: str):
    with open(SEEN_PATH, "a", encoding="utf-8") as f:
        f.write(uid + "\n")

def html_escape(s: str) -> str:
    return html.escape(s or "")

def parse_pub(dt_str: str | None) -> datetime | None:
    if not dt_str:
        return None
    try:
        dt = parsedate_to_datetime(dt_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except Exception:
        return None

def utc_to_est(dt: datetime | None) -> str:
    if not dt:
        return ""
    return dt.astimezone(EST).strftime("%-I:%M %p EST · %b %d")

def is_fresh(utc_dt: datetime | None, max_age_hours: float) -> bool:
    if not utc_dt:
        return True  # if no timestamp, allow but AI gate will catch noise
    age = datetime.now(UTC) - utc_dt
    return age <= timedelta(hours=max_age_hours)

def sha_uid(source: str, title: str, link: str) -> str:
    base = f"{source}||{title}||{link}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()

def send_message(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.get(url, params={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }, timeout=20)
    except Exception as e:
        print("Telegram error:", e)

def format_sentiment_for_nasdaq(direction: str, conf: int) -> str:
    d = (direction or "Neutral").title()
    if d == "Bearish":
        return f"🔻 <b>Bearish for NASDAQ</b>"
    elif d == "Bullish":
        return f"🔺 <b>Bullish for NASDAQ</b>"
    else:
        return f"🟨 Neutral for NASDAQ"

# ----------------- FOREX FACTORY PARSER -----------------

def parse_forex_factory_usd():
    """
    Returns list of dicts with keys: title, link, summary, source, published (UTC dt or None).
    Filters to USD events only by heuristic: look for '(USD)' in title/description
    """
    out = []
    try:
        feed = feedparser.parse(FF_FEED)
    except Exception as e:
        print("FF parse error:", e)
        return out

    for e in getattr(feed, "entries", []):
        title = (e.get("title") or "").strip()
        summary = (e.get("summary") or e.get("description") or "").strip()
        link = (e.get("link") or "").strip()
        src = "ForexFactory (USD)"

        # USD only (FF calendar shows "(USD)" often)
        text = f"{title}\n{summary}".lower()
        if "usd" not in text:
            continue

        pub = parse_pub(e.get("published"))
        out.append({
            "title": title,
            "summary": summary,
            "link": link,
            "source": src,
            "published": pub
        })
    return out

def parse_extra_feeds(urls: list[str]):
    """
    Generic RSS fetcher for optional feeds you may add later via EXTRA_RSS.
    """
    out = []
    for url in urls:
        try:
            feed = feedparser.parse(url.strip())
        except Exception as e:
            print("RSS parse error:", url, e)
            continue
        src_title = (getattr(getattr(feed, "feed", None), "title", None) or "Feed").strip()
        for e in getattr(feed, "entries", []):
            title = (getattr(e, "title", "") or "").strip()
            link = (getattr(e, "link", "") or "").strip()
            summary = (getattr(e, "summary", "") or getattr(e, "description", "") or "").strip()
            pub = parse_pub(getattr(e, "published", None))
            out.append({
                "title": title,
                "summary": summary,
                "link": link,
                "source": src_title,
                "published": pub
            })
    return out

# ----------------- PREFILTERS (NASDAQ focus) -----------------

def looks_relevant(title: str, summary: str) -> bool:
    """
    Heuristic: require NASDAQ-relevant terms in either title or summary.
    (Forex Factory USD feed is already focused, but we reinforce.)
    """
    txt = f"{title}\n{summary}".lower()
    return any(k in txt for k in HIGH_IMPACT_TERMS)

# ----------------- AI CLASSIFIER (NASDAQ ONLY) -----------------

def ai_classify_nasdaq(title: str, source: str, body: str):
    """
    Ask the model for NASDAQ-only trade impact with a strict rubric.
    """
    ctx = (body or "")[:4000]
    system = (
        "You are a professional US equities day-trading news analyst for a NASDAQ scalper. "
        "Only consider effects on the NASDAQ-100 (QQQ/NDX) and US mega-cap tech. "
        "Focus on near-term (0–48h) market impact. If not impactful for NASDAQ, set relevant=false."
    )
    rubric = (
        "Return pure JSON with keys:\n"
        "  relevant: boolean (true ONLY if likely to move NASDAQ in the next 0–48h)\n"
        "  direction: 'Bullish' | 'Bearish' | 'Neutral'\n"
        "  impact_score: integer 0-100 (immediate relevance to NASDAQ)\n"
        "  confidence: integer 0-100\n"
        "  summary: <=25 words with the trade takeaway for NASDAQ\n"
        "Be strict: generic business items are not relevant."
    )
    user = f"Source: {source}\nHeadline: {title}\nContext:\n{ctx}\n{rubric}"

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role":"system","content":system},
                {"role":"user","content":user}
            ],
            temperature=0.1,
        )
        raw = resp.choices[0].message.content.strip()
        data = json.loads(raw) if raw.startswith("{") else {}
    except Exception as e:
        print("AI error:", e)
        data = {}

    # normalize
    data.setdefault("relevant", False)
    data["direction"] = str(data.get("direction","Neutral")).title()
    try:
        data["impact_score"] = int(data.get("impact_score", 0))
    except Exception:
        data["impact_score"] = 0
    try:
        data["confidence"] = int(data.get("confidence", 50))
    except Exception:
        data["confidence"] = 50
    data["summary"] = (data.get("summary") or title)[:200]
    return data

def passes_gate(ai: dict) -> bool:
    if not ai.get("relevant"):
        return False
    if ai.get("impact_score", 0) < IMPACT_MIN:
        return False
    if ai.get("confidence", 0) < CONF_MIN:
        return False
    if ai.get("direction") == "Neutral" and not ALLOW_NEUTRAL:
        # Let neutral through only if very high impact (e.g., Fed minutes ambiguity)
        return ai.get("impact_score", 0) >= (IMPACT_MIN + 10)
    return True

# ----------------- MAIN LOOP -----------------

def fetch_once():
    posts_sent = 0

    # 1) Forex Factory (USD)
    items = parse_forex_factory_usd()

    # 2) Optional extra feeds from ENV (comma-separated)
    extra = [u.strip() for u in EXTRA_RSS.split(",") if u.strip()]
    if extra:
        items += parse_extra_feeds(extra)

    # Sort newest first when pub available
    items.sort(key=lambda x: x.get("published") or datetime.now(UTC), reverse=True)

    for it in items:
        title = it["title"]
        summary = it.get("summary") or ""
        link = it.get("link") or ""
        src = it.get("source") or "Feed"
        pub = it.get("published")

        # freshness
        if not is_fresh(pub, MAX_AGE_HOURS):
            continue

        # UID to avoid dups
        uid = sha_uid(src, title, link)
        if uid in seen:
            continue

        # NASDAQ heuristic prefilter
        if not looks_relevant(title, summary):
            continue

        # AI classification (NASDAQ only)
        ai = ai_classify_nasdaq(title, src, summary)
        if not passes_gate(ai):
            continue

        # Build message
        when_est = utc_to_est(pub)
        sentiment = format_sentiment_for_nasdaq(ai["direction"], ai["confidence"])
        src_line = f"🔗 <i>Source:</i> {html_escape(src)} —\n{html_escape(link)}" if link else f"🔗 <i>Source:</i> {html_escape(src)}"

        # Prefix sentiment BEFORE headline, as requested
        msg = (
            f"{sentiment}\n"
            f"📰 {html_escape(title)}\n"
            f"✍️ {html_escape(ai['summary'])}\n"
            f"{src_line}\n"
            f"🕒 {html_escape(when_est)}"
        )

        send_message(msg)
        seen.add(uid)
        save_seen(uid)
        posts_sent += 1
        time.sleep(0.7)

        if MAX_POSTS_PER_CYCLE and posts_sent >= MAX_POSTS_PER_CYCLE:
            break

def main():
    load_seen()
    send_message("✅ SmartFlow NASDAQ bot started (ForexFactory USD, NASDAQ-only AI).")
    backoff = 5
    while True:
        try:
            fetch_once()
            time.sleep(SLEEP_SECONDS)
            backoff = 5
        except Exception as e:
            print("Loop error:", e)
            time.sleep(backoff)
            backoff = min(backoff * 2, 240)

if __name__ == "__main__":
    main()
