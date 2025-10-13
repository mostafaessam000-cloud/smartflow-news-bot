import os, re, time, json, hashlib, feedparser, requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from openai import OpenAI
import httpx
from bs4 import BeautifulSoup

# =========================
# Env & Settings
# =========================
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY")

SLEEP_SECONDS    = int(os.getenv("SLEEP_SECONDS", "60"))   # poll interval
MAX_AGE_HOURS    = int(os.getenv("MAX_AGE_HOURS", "2"))    # fresh-only window
MIN_CONFIDENCE   = int(os.getenv("MIN_CONFIDENCE", "0"))   # e.g., 70
HIDE_NEUTRAL     = os.getenv("HIDE_NEUTRAL", "false").lower() in ("1","true","yes")
MAX_POSTS_PER_CYCLE = int(os.getenv("MAX_POSTS_PER_CYCLE", "0"))  # 0 = unlimited

# Domains to NOT fetch (paywalls/anti-bot); still post from RSS
SKIP_FETCH_DOMAINS = [
    d.strip().lower() for d in os.getenv("SKIP_FETCH_DOMAINS", "wsj.com,cnbc.com").split(",") if d.strip()
]

# Feeds (override with FEEDS / extend with EXTRA_RSS)
FEEDS_ENV  = os.getenv("FEEDS", "").strip()
EXTRA_RSS  = os.getenv("EXTRA_RSS", "").strip()

DEFAULT_FEEDS = [
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",  # CNBC Top
    "https://www.reuters.com/rssFeed/businessNews",            # Reuters Business
    "https://www.marketwatch.com/rss/topstories",              # MarketWatch
    "https://www.investing.com/rss/news.rss",                  # Investing.com
    "https://feeds.bbci.co.uk/news/business/rss.xml",          # BBC Business
    "https://www.nasdaq.com/feed/rssoutbound?category=MarketNews",
    "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",           # WSJ Markets
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

assert TELEGRAM_TOKEN and TELEGRAM_CHAT_ID and OPENAI_API_KEY, \
    "Missing env vars: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, OPENAI_API_KEY"

# OpenAI client
client = OpenAI(
    api_key=OPENAI_API_KEY,
    http_client=httpx.Client(follow_redirects=True, timeout=20),
)

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
    t = (title or "").lower()
    return any(k in t for k in HIGH_IMPACT_TERMS)

def html_escape(s: str) -> str:
    return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

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
        return "🟨 Neutral"
    elif s == "Bearish":
        return f"🔻 <b>Bearish</b> ({conf}%)"
    else:
        return f"🔺 <b>Bullish</b> ({conf}%)"

# ---------- Article extraction ----------
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36"}

def extract_article_text(url: str) -> tuple[str, datetime | None]:
    """
    Try to fetch page & extract main text + publish time.
    If blocked/paywalled (401/403), returns ("", None) but the caller will fall back to RSS summary.
    """
    if not url:
        return "", None

    # Skip known paywalled/anti-bot domains (still post from RSS)
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc.lower()
        if any(d and d in host for d in SKIP_FETCH_DOMAINS):
            return "", None
    except Exception:
        pass

    try:
        r = requests.get(url, headers=UA, timeout=12)
        # Stop on 401/403 but don't crash; caller will fallback to RSS summary
        if r.status_code in (401, 403):
            return "", None
        r.raise_for_status()
        html = r.text
        full = BeautifulSoup(html, "html.parser")

        # Prefer <article>
        text_chunks: list[str] = []
        art = full.find("article")
        if art:
            for p in art.find_all(["p","li"]):
                txt = p.get_text(" ", strip=True)
                if len(txt) >= 60:
                    text_chunks.append(txt)

        # Fallback: long paragraphs
        if len(" ".join(text_chunks)) < 300:
            for p in full.find_all("p"):
                txt = p.get_text(" ", strip=True)
                if len(txt) >= 100:
                    text_chunks.append(txt)

        article_text = " ".join(text_chunks)[:5000]

        # Meta publish time
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
                    published = parse_any_ts(ts)
                    if published:
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
    ctx = article_text[:4000] if article_text else ""
    system = (
        "You are a senior macro/market analyst advising a NASDAQ day trader. "
        "Read the article context (if any) and the headline. "
        "Deliver a crisp <=25-word market takeaway that does NOT repeat the headline verbatim. "
        "When evidence suggests direction, prefer a decisive classification (Bullish or Bearish). "
        "Choose Neutral ONLY if there is truly no directional signal in the next 1–3 sessions. "
        "Classify overall impact on NASDAQ as Bullish, Bearish, or Neutral. "
        "Provide a confidence 0–100. Include 1–3 tags chosen from: "
        "Tariff, China, Fed, CPI, NFP, Regulation, War, Energy, AI, Earnings, Sanctions, Rates, Yields, FX. "
        "Return JSON ONLY with keys: summary, sentiment, confidence, tags."
    )
    user = f"Source: {source}\nHeadline: {title}\nArticle context:\n{ctx}\nReturn JSON only."

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"system","content":system},
                      {"role":"user","content":user}],
            temperature=0.2,
        )
        text = resp.choices[0].message.content.strip()
        data = json.loads(text) if text.startswith("{") else {
            "summary": text[:200],
            "sentiment": "Neutral",
            "confidence": 60,
            "tags": []
        }
        data["sentiment"]  = str(data.get("sentiment","Neutral")).title()
        try:
            data["confidence"] = int(float(data.get("confidence", 60)))
        except Exception:
            data["confidence"] = 60
        if not isinstance(data.get("tags", []), list):
            data["tags"] = []
        return data
    except Exception as e:
        print("AI error:", e)
        return {"summary": title, "sentiment":"Neutral","confidence":50,"tags":[]}

# =========================
# Fetch & process
# =========================
def fetch_once(limit_per_feed=8):
    global seen

    # 1) Pull entries
    items = []
    for url in FEEDS:
        try:
            feed = feedparser.parse(url)
            src  = (getattr(getattr(feed, "feed", None), "title", None) or url).strip()
            for entry in feed.entries[:limit_per_feed]:
                title = (getattr(entry, "title", "") or "").strip()
                link  = (getattr(entry, "link", "") or "").strip()
                if not title:
                    continue
                items.append({"source": src, "title": title, "link": link, "entry": entry})
        except Exception as e:
            print("Feed error:", url, e)

    # 2) Precompute publish times for sorting, then sort newest first
    enriched = []
    for it in items:
        dt_from_rss = published_dt_from_entry(it["entry"], None)
        enriched.append((dt_from_rss, it))
    enriched.sort(key=lambda x: x[0] or datetime.now(timezone.utc), reverse=True)

    # 3) Iterate and post (fresh-only, dedupe, optional cap per cycle)
    posted = 0
    seen_now = set()
    now_utc = datetime.now(timezone.utc)

    for dt_rss, it in enriched:
        uid = make_uid(it["title"])
        if uid in seen or uid in seen_now:
            continue
        if not looks_relevant(it["title"]):
            continue

        # Try page; if blocked, fallback to RSS summary
        article_text, published_from_html = extract_article_text(it["link"])

        if not article_text:
            rss_summary = getattr(it["entry"], "summary", "") or getattr(it["entry"], "description", "")
            if rss_summary:
                rss_summary = BeautifulSoup(rss_summary, "html.parser").get_text(" ", strip=True)
                article_text = rss_summary

        dt_utc = published_dt_from_entry(it["entry"], published_from_html) or dt_rss
        if not dt_utc:
            dt_utc = now_utc

        # Freshness filter (<= MAX_AGE_HOURS)
        if dt_utc and (now_utc - dt_utc) > timedelta(hours=MAX_AGE_HOURS):
            continue

        ai = ai_classify(it["title"], it["source"], article_text or "")

        if HIDE_NEUTRAL and ai["sentiment"] == "Neutral":
            continue
        if ai["confidence"] < MIN_CONFIDENCE:
            continue

        # Time formatting
        dt_est = dt_utc.astimezone(ZoneInfo("America/New_York"))
        minutes_ago = int((now_utc - dt_utc).total_seconds() // 60)
        ago_str = f"{minutes_ago} min ago" if minutes_ago < 90 else f"{minutes_ago//60} hr ago"
        when = f"{dt_est.strftime('%-I:%M %p EST • %b %-d')} ({ago_str})"

        # Summary fallback + avoid duplication with title
        summary = (ai.get("summary") or "").strip()
        if not summary:
            summary = "Headline-driven; watch for confirmation in futures and mega-cap tech."
        if summary.lower() == it["title"].strip().lower():
            summary = "Market takeaway: headline suggests near-term volatility; monitor QQQ/NQ leaders."

        # Source line + hyperlink
        if it["link"]:
            src_line = f'🔗 Source: <a href="{html_escape(it["link"])}">{html_escape(it["source"])}</a>'
        else:
            src_line = f"🔗 Source: {html_escape(it["source"])}"

        msg = (
            f"📰 {html_escape(it['title'])}\n"
            f"✍️ {html_escape(summary)}\n"
            f"{format_sentiment(ai)}\n"
            f"{src_line}\n"
            f"🕒 {html_escape(when)}"
        )

        send_message(msg)
        seen_now.add(uid)
        posted += 1
        time.sleep(1)  # small pacing

        if MAX_POSTS_PER_CYCLE > 0 and posted >= MAX_POSTS_PER_CYCLE:
            break

    if seen_now:
        seen |= seen_now
        save_seen(seen)

def main():
    send_message("✅ SmartFlow News worker started (fresh ≤ 2 hours, RSS fallback enabled).")
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
