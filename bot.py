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
    Returns:
      {
        "summary": str,                  # <=25 words
        "sentiment": "Bullish|Bearish|Neutral",
        "confidence": int,              # model certainty 0-100
        "impact": int                   # market-moving impact 0-100 (how much it can move NASDAQ in 1–3 sessions)
      }
    Works with headline only (article_text may be empty).
    """
    if not USE_AI:
        return {"summary": "", "sentiment": "Neutral", "confidence": 60, "impact": 50}

    headline = (title or "").strip()
    # Always give the model SOMETHING: headline is enough; add context if available
    ctx = (article_text or "").strip()
    ctx_block = f"Context:\n{ctx[:3500]}" if ctx else "Context: (none — classify using headline alone)"

    system = (
        "Act as a senior macro/market analyst for a NASDAQ day trader. "
        "Using the headline (and context if any), classify the next 1–3 sessions effect on NASDAQ.\n"
        "Return STRICT JSON ONLY with keys:\n"
        '{"summary":"<=25 words, specific, not repeating the headline",'
        '"sentiment":"Bullish|Bearish|Neutral",'
        '"confidence":0-100,'
        '"impact":0-100}\n'
        "- 'impact' = how market-moving this is for NASDAQ (0 = trivial, 100 = very market-moving).\n"
        "Prefer Bullish/Bearish when any directional cue exists (beats/misses, guidance, rates, regulation, war, tariffs, chip export controls, etc.); "
        "use Neutral only if truly no likely direction."
    )

    user = f"Source: {source}\nHeadline: {headline}\n{ctx_block}\nJSON only."

    try:
        model = genai.GenerativeModel(GEMINI_MODEL)
        res = model.generate_content(
            [{"text": system}, {"text": user}],
            generation_config={
                "temperature": 0.15,
                "max_output_tokens": 256,
                "response_mime_type": "application/json",  # force JSON
            },
            safety_settings={
                "HARASSMENT": "block_none",
                "HATE_SPEECH": "block_none",
                "SEXUAL_CONTENT": "block_none",
                "DANGEROUS_CONTENT": "block_none",
            },
        )
        raw = (res.text or "").strip()
        data = {}
        try:
            data = json.loads(raw)
        except Exception:
            start, end = raw.find("{"), raw.rfind("}")
            if start != -1 and end != -1:
                data = json.loads(raw[start:end+1])

        # normalize keys (case-insensitive)
        norm = {str(k).lower(): v for k, v in (data or {}).items()}
        summary = (norm.get("summary") or "").strip()
        sentiment = str(norm.get("sentiment", "Neutral")).title()
        try:
            confidence = int(float(norm.get("confidence", 60)))
        except Exception:
            confidence = 60
        try:
            impact = int(float(norm.get("impact", confidence)))
        except Exception:
            impact = confidence

        # strict normalize
        s = sentiment.lower()
        if "bull" in s:
            sentiment = "Bullish"
        elif "bear" in s:
            sentiment = "Bearish"
        else:
            sentiment = "Neutral"

        if not summary or summary.lower() in {"bullish", "bearish", "neutral", headline.lower()}:
            # Make a short, useful line even with just the headline
            summary = f"Headline suggests near-term move; watch mega-cap tech — {headline[:120]}"

        return {"summary": summary[:220], "sentiment": sentiment, "confidence": confidence, "impact": impact}
    except Exception as e:
        print("AI error (Gemini):", e)
        # safe fallback still analyzes headline notionally
        return {
            "summary": f"Headline suggests near-term move; watch mega-cap tech — {title[:120]}",
            "sentiment": "Neutral",
            "confidence": 60,
            "impact": 50,
        }


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
RANK_BY_IMPACT = os.getenv("RANK_BY_IMPACT", "true").lower() in ("1","true","yes")
IMPACT_WEIGHT  = float(os.getenv("IMPACT_WEIGHT", "0.7"))
RECENCY_WEIGHT = float(os.getenv("RECENCY_WEIGHT", "0.3"))

def recency_score(minutes_old: int) -> float:
    """
    Map age in minutes to a 0..100 score (newer = higher).
    0 min -> ~100, 60 min -> ~70, 120 min -> ~50, 180+ -> lower.
    """
    if minutes_old <= 0:
        return 100.0
    # exponential decay tuned for news
    import math
    return max(0.0, 100.0 * math.exp(-minutes_old / 120.0))  # 2h half-life-ish

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
    # 3) Score candidates first (AI + ranking), then post top N
    candidates = []
    seen_now = set()
    now_utc = datetime.now(timezone.utc)

    for dt_rss, it in enriched:
        uid = make_uid(it["title"])
        if uid in seen or uid in seen_now:
            continue
        if not looks_relevant(it["title"]):
            continue

        # Extract article text; if empty, we'll classify from headline only
        article_text, published_from_html = extract_article_text(it["link"])
        if not article_text:
            rss_summary = getattr(it["entry"], "summary", "") or getattr(it["entry"], "description", "")
            if rss_summary:
                article_text = BeautifulSoup(rss_summary, "html.parser").get_text(" ", strip=True)

        dt_utc = published_dt_from_entry(it["entry"], published_from_html) or dt_rss or now_utc

        # Freshness filter
        if dt_utc and (now_utc - dt_utc) > timedelta(hours=MAX_AGE_HOURS):
            continue

        ai = ai_classify(it["title"], it["source"], article_text or "")

        if HIDE_NEUTRAL and ai["sentiment"] == "Neutral":
            continue
        if ai["confidence"] < MIN_CONFIDENCE:
            continue

        minutes_ago = int((now_utc - dt_utc).total_seconds() // 60)
        rscore = recency_score(minutes_ago)
        # Combine impact (from AI) and recency
        if RANK_BY_IMPACT:
            final_score = IMPACT_WEIGHT * float(ai.get("impact", ai.get("confidence", 60))) + \
                          RECENCY_WEIGHT * rscore
        else:
            final_score = float(ai.get("confidence", 60))

        candidates.append({
            "score": final_score,
            "uid": uid,
            "it": it,
            "ai": ai,
            "dt_utc": dt_utc,
            "minutes_ago": minutes_ago,
        })

    # Sort by score (desc), then newest
    candidates.sort(key=lambda x: (x["score"], x["dt_utc"]), reverse=True)

    # Apply MAX_POSTS_PER_CYCLE limit HERE (top-N most impactful)
    limit = MAX_POSTS_PER_CYCLE if MAX_POSTS_PER_CYCLE > 0 else len(candidates)
    winners = candidates[:limit]

    posted = 0
    for c in winners:
        it = c["it"]
        ai = c["ai"]
        dt_utc = c["dt_utc"]

        # Time formatting
        dt_est = dt_utc.astimezone(EST)
        minutes_ago = c["minutes_ago"]
        ago_str = f"{minutes_ago} min ago" if minutes_ago < 90 else f"{minutes_ago//60} hr ago"
        when = f"{dt_est.strftime('%-I:%M %p ')}{_tz_label} • {dt_est.strftime('%b %-d')} ({ago_str})"

        nice_src = publisher_from_link(it["link"], it["source"])
        if it["link"]:
            src_line = f'🔗 Source: <a href="{html_escape(it["link"])}">{html_escape(nice_src)}</a>'
        else:
            src_line = f"🔗 Source: {html_escape(nice_src)}"

        summary = (ai.get("summary") or "").strip()
        if not summary:
            summary = f"Headline suggests near-term move; watch mega-cap tech — {it['title'][:120]}"

        msg = (
            f"{format_sentiment(ai)}\n"
            f"📰 {html_escape(it['title'])}\n"
            f"✍️ {html_escape(summary)}\n"
            f"{src_line}\n"
            f"🕒 {html_escape(when)}"
        )

        send_message(msg)
        seen_now.add(c["uid"])
        posted += 1
        time.sleep(1)

    if seen_now:
        seen |= seen_now
        save_seen(seen)


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
