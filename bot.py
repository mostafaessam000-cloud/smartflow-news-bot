# bot.py (stable UA + fresh Reuters feeds + TZ fallback)
# AI: Google Gemini
import google.generativeai as genai
import os, re, time, json, hashlib, feedparser, requests
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
from bs4 import BeautifulSoup

# ===== timezone (safe fallback) =====
try:
    from zoneinfo import ZoneInfo
    EST = ZoneInfo("America/New_York")
    _tz_label = "EST"
except Exception:
    EST = timezone.utc
    _tz_label = "UTC"

# =========================
# Env & Settings
# =========================
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

SLEEP_SECONDS    = int(os.getenv("SLEEP_SECONDS", "60"))   # poll interval
MAX_AGE_HOURS    = int(os.getenv("MAX_AGE_HOURS", "2"))    # fresh-only window
MIN_CONFIDENCE   = int(os.getenv("MIN_CONFIDENCE", "0"))   # e.g., 70
HIDE_NEUTRAL     = os.getenv("HIDE_NEUTRAL", "false").lower() in ("1","true","yes")
MAX_POSTS_PER_CYCLE = int(os.getenv("MAX_POSTS_PER_CYCLE", "0"))  # 0 = unlimited
REQUIRE_KEYWORDS = os.getenv("REQUIRE_KEYWORDS", "false").lower() in ("1","true","yes")

# Domains to NOT fetch (paywalls/anti-bot); still post from RSS
SKIP_FETCH_DOMAINS = [
    d.strip().lower()
    for d in os.getenv("SKIP_FETCH_DOMAINS", "wsj.com,ft.com,bloomberg.com").split(",")
    if d.strip()
]

# Feeds (override with FEEDS / extend with EXTRA_RSS)
FEEDS_ENV  = os.getenv("FEEDS", "").strip()
EXTRA_RSS  = os.getenv("EXTRA_RSS", "").strip()

# ---- Updated, reliable feeds (Reuters endpoints changed) ----
DEFAULT_FEEDS = [
    # Reuters (current endpoints)
    "https://www.reuters.com/markets/us/rss",
    "https://www.reuters.com/markets/earnings/rss",
    "https://www.reuters.com/technology/rss",

    # Core finance/business
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",      # CNBC Top
    "https://www.marketwatch.com/rss/topstories",
    "https://www.nasdaq.com/feed/rssoutbound?category=MarketNews",
    "https://finance.yahoo.com/news/rssindex",

    # Broad business
    "https://feeds.bbci.co.uk/news/business/rss.xml",
    "https://rss.cnn.com/rss/money_latest.rss",
    "https://apnews.com/hub/business?output=rss",
    "https://www.cbsnews.com/moneywatch/rss/",
    "https://abcnews.go.com/abcnews/moneyheadlines",
    "https://www.theguardian.com/us/business/rss",
]

FEEDS = [u.strip() for u in (FEEDS_ENV if FEEDS_ENV else ",".join(DEFAULT_FEEDS)).split(",") if u.strip()]
if EXTRA_RSS:
    FEEDS += [u.strip() for u in EXTRA_RSS.split(",") if u.strip()]

KEYWORDS_ENV = os.getenv("KEYWORDS", "")
HIGH_IMPACT_TERMS = [k.strip().lower() for k in KEYWORDS_ENV.split(",") if k.strip()] or [
    "trump","tariff","china","ban","export","import","sanction","retaliat",
    "fed","powell","rate","hike","cut","inflation","cpi","nfp","yield","treasury",
    "war","attack","strike","missile","shutdown",
    "semiconductor","chip","ai","regulation","export control","rare earth"
]

# Optional Gemini
USE_AI = bool(GEMINI_API_KEY)
if USE_AI:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
    except Exception as e:
        print("Gemini init error:", e)
        USE_AI = False

# =========================
# Dedup storage
# =========================
SEEN_PATH = "seen.json"
SEEN_LIMIT = 5000

def load_seen():
    if os.path.exists(SEEN_PATH):
        try:
            with open(SEEN_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                return set(data if isinstance(data, list) else [])
        except Exception:
            return set()
    return set()

def save_seen(s: set):
    if len(s) > SEEN_LIMIT:
        s = set(list(s)[-SEEN_LIMIT:])
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(list(s), f)

seen = load_seen()

# =========================
# Helpers
# =========================
_norm_re = re.compile(r"[^\w\s]")

def normalize_title(t: str) -> str:
    t = (t or "").strip().lower()
    t = _norm_re.sub(" ", t)
    t = re.sub(r"\s+", " ", t)
    return t

def make_uid(title: str) -> str:
    return hashlib.sha1(normalize_title(title).encode("utf-8")).hexdigest()

def looks_relevant(title: str) -> bool:
    if not REQUIRE_KEYWORDS:
        return True
    t = (title or "").lower()
    return any(k in t for k in HIGH_IMPACT_TERMS)

def html_escape(s: str) -> str:
    return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

DOMAIN_LABELS = {
    "reuters.com": "Reuters",
    "cnbc.com": "CNBC",
    "marketwatch.com": "MarketWatch",
    "nasdaq.com": "Nasdaq",
    "finance.yahoo.com": "Yahoo Finance",
    "yahoo.com": "Yahoo Finance",
    "cnn.com": "CNN Business",
    "apnews.com": "AP News",
    "theguardian.com": "The Guardian",
    "cbsnews.com": "CBS News / MoneyWatch",
    "abcnews.go.com": "ABC News",
    "bbc.com": "BBC",
    "bbc.co.uk": "BBC",
}

def publisher_from_link(link: str, fallback: str) -> str:
    try:
        host = urlparse(link).netloc.lower()
        parts = host.split(".")
        dom = ".".join(parts[-2:]) if len(parts) >= 2 else host
        return DOMAIN_LABELS.get(dom, fallback)
    except Exception:
        return fallback

def send_message(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.get(
            url,
            params={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
    except Exception as e:
        print("Telegram error:", e)

def format_sentiment(ai: dict) -> str:
    s = (ai.get("sentiment") or "Neutral").title()
    try:
        conf = int(float(ai.get("confidence", 60)))
    except Exception:
        conf = 60

    if s == "Neutral":
        return "🟨 NASDAQ Neutral"
    elif s == "Bearish":
        return f"🔻 <b>NASDAQ Bearish</b> ({conf}%)"
    else:
        # Green up arrow for bullish
       return f"<b><font color='green'>▲ NASDAQ Bullish</font></b> ({conf}%)"



# ---------- Article extraction ----------
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36"}

def extract_article_text(url: str) -> tuple[str, datetime | None]:
    if not url:
        return "", None
    try:
        host = urlparse(url).netloc.lower()
        if any(d and d in host for d in SKIP_FETCH_DOMAINS):
            return "", None
    except Exception:
        pass
    try:
        r = requests.get(url, headers=UA, timeout=12)
        if r.status_code in (401, 403):
            return "", None
        r.raise_for_status()
        full = BeautifulSoup(r.text, "html.parser")

        text_chunks = []
        art = full.find("article")
        if art:
            for p in art.find_all(["p","li"]):
                txt = p.get_text(" ", strip=True)
                if len(txt) >= 60:
                    text_chunks.append(txt)

        if len(" ".join(text_chunks)) < 300:
            for p in full.find_all("p"):
                txt = p.get_text(" ", strip=True)
                if len(txt) >= 100:
                    text_chunks.append(txt)

        article_text = " ".join(text_chunks)[:5000]

        # publish time (best effort)
        published = None
        try:
            meta_candidates = [
                ("meta", {"property":"article:published_time"}),
                ("meta", {"name":"article:published_time"}),
                ("meta", {"name":"pubdate"}),
                ("meta", {"property":"og:updated_time"}),
                ("meta", {"property":"og:published_time"}),
                ("time", {"datetime": True}),
            ]
            for tag, attrs in meta_candidates:
                el = full.find(tag, attrs=attrs)
                if el and (el.get("content") or el.get("datetime")):
                    ts = el.get("content") or el.get("datetime")
                    dt = parse_any_ts(ts)
                    if dt:
                        published = dt
                        break
        except Exception:
            pass

        return article_text.strip(), published
    except Exception as e:
        print("extract_article_text error:", e)
        return "", None

def parse_any_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z","+00:00"))
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

def published_dt_from_entry(entry, link_html_published=None) -> datetime | None:
    for attr in ("published_parsed","updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    if link_html_published:
        return link_html_published
    return None

# ---------- AI classification ----------
def ai_classify(title: str, source: str, article_text: str):
    """
    Returns: {"summary": str, "sentiment": "Bullish|Bearish|Neutral", "confidence": int}
    Gemini-first; lightly biases away from 'Neutral' unless clearly no directional info.
    """
    if not USE_AI:
        return {"summary": "", "sentiment": "Neutral", "confidence": 60}

    ctx = (article_text or "")[:4000]
    headline = (title or "").strip()

    FEW_SHOTS = [
        # bullish
        {
            "title": "Nvidia tops estimates, raises full-year revenue outlook",
            "ctx":  "Chip demand strong; data center revenue accelerates; guidance raised",
            "sent": "Bullish", "sum": "Guidance raise and beat point to near-term strength in mega-cap tech.", "conf": 80
        },
        # bearish
        {
            "title": "Apple faces EU antitrust fine over App Store rules",
            "ctx":  "Regulators preparing penalties; potential changes to business model",
            "sent": "Bearish", "sum": "Regulatory risk elevates; headline pressure likely for mega-caps.", "conf": 70
        },
        # neutral
        {
            "title": "U.S. House passes bipartisan budget deal, averts shutdown",
            "ctx":  "Avoids disruption; no new stimulus or austerity",
            "sent": "Neutral", "sum": "Removes a tail risk but lacks clear earnings or rate impact.", "conf": 60
        },
    ]

    system = (
        "Act as a senior macro/market analyst for a NASDAQ day trader. "
        "Classify the **next 1–3 sessions** impact on NASDAQ as exactly one of: Bullish, Bearish, Neutral. "
        "Prefer Bullish/Bearish when any reasonable directional signal exists (beats/misses, guidance changes, rate path shifts, regulation). "
        "Use Neutral only if there is truly no likely directional effect. "
        "Return STRICT JSON: {\"summary\": \"<=25 words\", \"sentiment\": \"Bullish|Bearish|Neutral\", \"confidence\": 0-100}."
    )

    # build few-shot block
    shot_txt = []
    for s in FEW_SHOTS:
        shot_txt.append(
            f"Example\nTitle: {s['title']}\nContext: {s['ctx']}\n"
            f"JSON: {{\"summary\":\"{s['sum']}\",\"sentiment\":\"{s['sent']}\",\"confidence\":{s['conf']}}}"
        )
    shots = "\n\n".join(shot_txt)

    user = (
        f"{shots}\n\n"
        f"Now classify this.\n"
        f"Title: {headline}\n"
        f"Context: {ctx}\n"
        f"JSON only."
    )

    try:
        model = genai.GenerativeModel(GEMINI_MODEL)
        res = model.generate_content(
            [{"text": system}, {"text": user}],
            generation_config={"temperature": 0.15, "max_output_tokens": 200},
            safety_settings={
                "HARASSMENT": "block_none", "HATE_SPEECH": "block_none",
                "SEXUAL_CONTENT": "block_none", "DANGEROUS_CONTENT": "block_none",
            },
        )
        out = (res.text or "").strip()
        blob = out[out.find("{"): out.rfind("}")+1]
        data = json.loads(blob) if blob.startswith("{") and blob.endswith("}") else {}
    except Exception as e:
        print("AI error (Gemini):", e)
        data = {}

    summary = (data.get("summary") or "").strip()[:220]
    sentiment = str(data.get("sentiment", "Neutral")).title()
    try:
        confidence = int(float(data.get("confidence", 60)))
    except Exception:
        confidence = 60

    # ---------- post-process neutrals (gentle nudge) ----------
    if sentiment == "Neutral":
        text_l = f"{headline}. {ctx}".lower()

        bullish_kw = [
            "beat", "beats", "tops estimates", "raises guidance", "raise guidance",
            "upgrade", "upgrades", "surge", "rallies", "cuts rates", "stimulus",
            "expands", "record high", "strong demand", "contracts awarded"
        ]
        bearish_kw = [
            "miss", "misses", "below estimates", "cuts guidance", "lower outlook",
            "downgrade", "downgrades", "probe", "investigation", "antitrust",
            "sanction", "tariff", "ban", "recall", "layoff", "strike", "war",
            "hike rates", "hot inflation", "yields jump", "shutdown"
        ]

        bull_hit = any(k in text_l for k in bullish_kw)
        bear_hit = any(k in text_l for k in bearish_kw)

        if bull_hit and not bear_hit:
            sentiment, confidence = "Bullish", max(confidence, 65)
        elif bear_hit and not bull_hit:
            sentiment, confidence = "Bearish", max(confidence, 65)

    if not summary:
        summary = "Headline-driven setup; watch QQQ/NQ leaders for confirmation."

    return {"summary": summary, "sentiment": sentiment, "confidence": confidence}

# =========================
# Feed fetching with UA
# =========================
FEED_UA = {"User-Agent": UA["User-Agent"]}

def parse_feed_with_ua(url: str):
    # Try with requests + UA first (bypasses a lot of 403s), then fallback
    try:
        r = requests.get(url, headers=FEED_UA, timeout=15)
        if r.ok and r.content:
            return feedparser.parse(r.content)
    except Exception as e:
        print("Feed HTTP fetch error:", url, e)
    try:
        return feedparser.parse(url)
    except Exception as e:
        print("Feedparser error:", url, e)
        return None

# =========================
# Fetch & process
# =========================
def fetch_once(limit_per_feed=10):
    global seen

    # 1) Pull entries (with UA)
    items = []
    for url in FEEDS:
        feed = parse_feed_with_ua(url)
        if not feed:
            continue
        src  = (getattr(getattr(feed, "feed", None), "title", None) or url).strip()
        for entry in getattr(feed, "entries", [])[:limit_per_feed]:
            title = (getattr(entry, "title", "") or "").strip()
            link  = (getattr(entry, "link", "") or "").strip()
            if not title:
                continue
            items.append({"source": src, "title": title, "link": link, "entry": entry})

    # 2) Sort newest first
    enriched = []
    for it in items:
        dt_from_rss = published_dt_from_entry(it["entry"], None)
        enriched.append((dt_from_rss, it))
    enriched.sort(key=lambda x: x[0] or datetime.now(timezone.utc), reverse=True)

    # 3) Iterate and post
    posted = 0
    seen_now = set()
    now_utc = datetime.now(timezone.utc)

    for dt_rss, it in enriched:
        uid = make_uid(it["title"])
        if uid in seen or uid in seen_now:
            continue
        if not looks_relevant(it["title"]):
            continue

        article_text, published_from_html = extract_article_text(it["link"])

        if not article_text:
            rss_summary = getattr(it["entry"], "summary", "") or getattr(it["entry"], "description", "")
            if rss_summary:
                rss_summary = BeautifulSoup(rss_summary, "html.parser").get_text(" ", strip=True)
                article_text = rss_summary

        dt_utc = published_dt_from_entry(it["entry"], published_from_html) or dt_rss or now_utc

        # Freshness
        if dt_utc and (now_utc - dt_utc) > timedelta(hours=MAX_AGE_HOURS):
            continue

        ai = ai_classify(it["title"], it["source"], article_text or "")

        if HIDE_NEUTRAL and ai["sentiment"] == "Neutral":
            continue
        if ai["confidence"] < MIN_CONFIDENCE:
            continue

        # Time formatting
        dt_est = dt_utc.astimezone(EST)
        minutes_ago = int((now_utc - dt_utc).total_seconds() // 60)
        ago_str = f"{minutes_ago} min ago" if minutes_ago < 90 else f"{minutes_ago//60} hr ago"
        when = f"{dt_est.strftime('%-I:%M %p ')}{_tz_label} • {dt_est.strftime('%b %-d')} ({ago_str})"

        # Friendly publisher
        nice_src = publisher_from_link(it["link"], it["source"])
        if it["link"]:
            src_line = f'🔗 Source: <a href="{html_escape(it["link"])}">{html_escape(nice_src)}</a>'
        else:
            src_line = f"🔗 Source: {html_escape(nice_src)}"

        summary = (ai.get("summary") or "").strip()
        if not summary:
            summary = "Headline-driven; watch for confirmation in futures and mega-cap tech."
        if summary.lower() == it["title"].strip().lower():
            summary = "Market takeaway: headline implies near-term volatility; watch QQQ/NQ leaders."

        msg = (
            f"{format_sentiment(ai)}\n"
            f"📰 {html_escape(it['title'])}\n"
            f"✍️ {html_escape(summary)}\n"
            f"{src_line}\n"
            f"🕒 {html_escape(when)}"
        )

        send_message(msg)
        seen_now.add(uid)
        posted += 1
        time.sleep(1)

        if MAX_POSTS_PER_CYCLE > 0 and posted >= MAX_POSTS_PER_CYCLE:
            break

    if seen_now:
        seen |= seen_now
        save_seen(seen)

def main():
    send_message("✅ SmartFlow News worker started (UA-enabled feeds, fresh ≤ 2h).")
    backoff = 5
    while True:
        try:
            fetch_once()
            time.sleep(SLEEP_SECONDS)
            backoff = 5
        except Exception as e:
            print("Loop error:", e)
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)

if __name__ == "__main__":
    main()
