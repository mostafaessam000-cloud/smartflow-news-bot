import os, re, time, json, hashlib, feedparser, requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from openai import OpenAI
import httpx

# for article extraction
from readability import Document
from bs4 import BeautifulSoup

# =========================
# Env & Settings
# =========================
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY")
SLEEP_SECONDS    = int(os.getenv("SLEEP_SECONDS", "60"))

# Optional filters
MIN_CONFIDENCE   = int(os.getenv("MIN_CONFIDENCE", "0"))          # e.g. 70
HIDE_NEUTRAL     = os.getenv("HIDE_NEUTRAL", "false").lower() in ("1","true","yes")

# Feeds (add extra RSS like X/Truth via EXTRA_RSS)
FEEDS_ENV  = os.getenv("FEEDS", "").strip()
EXTRA_RSS  = os.getenv("EXTRA_RSS", "").strip()
FEEDS = [u.strip() for u in (FEEDS_ENV + ("," if FEEDS_ENV and EXTRA_RSS else "") + EXTRA_RSS).split(",") if u.strip()]
if not FEEDS:
    FEEDS = [
        "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",          # WSJ Markets
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",  # CNBC Top
        "https://www.reuters.com/rssFeed/businessNews",           # Reuters Business
    ]

KEYWORDS_ENV = os.getenv("KEYWORDS", "")
HIGH_IMPACT_TERMS = [k.strip().lower() for k in KEYWORDS_ENV.split(",") if k.strip()] or [
    "trump","tariff","china","ban","export","import","sanction","retaliat",
    "fed","powell","rate","hike","cut","inflation","cpi","nfp","yield","treasury",
    "war","attack","strike","missile","shutdown",
    "semiconductor","chip","ai","regulation","export control","rare earth"
]

assert TELEGRAM_TOKEN and TELEGRAM_CHAT_ID and OPENAI_API_KEY, \
    "Missing env vars: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, OPENAI_API_KEY"

# OpenAI client (httpx avoids unsupported 'proxies' kwarg path)
client = OpenAI(
    api_key=OPENAI_API_KEY,
    http_client=httpx.Client(follow_redirects=True, timeout=20),
)

# =========================
# Dedup storage
# =========================
SEEN_PATH = "seen.json"         # store hashes here
SEEN_LIMIT = 5000               # keep last N items

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
_norm_re = re.compile(r"[^\w\s]")  # remove punctuation

def normalize_title(t: str) -> str:
    t = (t or "").strip().lower()
    t = _norm_re.sub(" ", t)
    t = re.sub(r"\s+", " ", t)
    return t

def make_uid(title: str) -> str:
    # Hash ONLY the normalized title so duplicates across different feeds collapse
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
                "parse_mode": "HTML",              # enable HTML formatting
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
        # yellow box, no percentage
        return "🟨 Neutral"
    elif s == "Bearish":
        # red down arrow, bold word + percentage
        return f"🔻 <b>Bearish</b> ({conf}%)"
    else:  # Bullish
        # green up arrow, bold word + percentage
        return f"🔺 <b>Bullish</b> ({conf}%)"

# ---------- Article extraction ----------

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36"}

def extract_article_text(url: str) -> tuple[str, datetime | None]:
    """
    Return (main_text, published_dt_utc or None).
    Uses readability + BeautifulSoup; safe fallbacks on failure.
    """
    if not url:
        return "", None
    try:
        r = requests.get(url, headers=UA, timeout=12)
        r.raise_for_status()
        html = r.text

        # Try readability for main content
        doc = Document(html)
        summary_html = doc.summary()
        soup = BeautifulSoup(summary_html, "lxml")
        # get a solid chunk of text
        paras = [p.get_text(" ", strip=True) for p in soup.find_all(["p","li"]) if p.get_text(strip=True)]
        article_text = " ".join(paras)
        if len(article_text) < 300:  # too short? try full page fallbacks
            full = BeautifulSoup(html, "lxml")
            # look for <article> or long paragraphs
            art = full.find("article")
            if art:
                paras = [p.get_text(" ", strip=True) for p in art.find_all(["p","li"]) if p.get_text(strip=True)]
            else:
                paras = [p.get_text(" ", strip=True) for p in full.find_all("p") if len(p.get_text(strip=True)) > 60]
            article_text = " ".join(paras)[:5000]

        # Try to extract published time from meta tags
        published = None
        try:
            full = BeautifulSoup(html, "lxml")
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
    # very lenient parser for common formats
    try:
        # try fromisoformat first
        dt = datetime.fromisoformat(s.replace("Z","+00:00"))
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    # RFC-like formats from some sites
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

def published_dt_from_entry(entry, link_html_published=None) -> datetime | None:
    # 1) RSS published/updated
    for attr in ("published_parsed","updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            try:
                dt = datetime(*t[:6], tzinfo=timezone.utc)
                return dt
            except Exception:
                pass
    # 2) From article HTML meta if provided
    if link_html_published:
        return link_html_published
    return None

# ---------- AI classification with full article context ----------
def ai_classify(title: str, source: str, article_text: str):
    # Trim text to keep tokens reasonable
    ctx = article_text[:4000] if article_text else ""
    system = (
        "You are a senior macro/market analyst advising a NASDAQ day trader. "
        "Read the provided article context (if any) and the headline. "
        "Give a crisp <=25-word summary that does NOT repeat the headline verbatim. "
        "Read between the lines for market-relevant implications and risks (but avoid unfounded speculation). "
        "Classify overall impact on NASDAQ as Bullish, Bearish, or Neutral. "
        "Provide a confidence 0-100, and 1-3 tags chosen from: "
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
def fetch_once(limit_per_feed=6):
    global seen
    # Collect entries from all feeds first so de-dupe works across the union
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

    # Iterate unique by title-hash
    seen_now = set()
    for it in items:
        uid = make_uid(it["title"])
        if uid in seen or uid in seen_now:
            continue
        if not looks_relevant(it["title"]):
            continue

        article_text, published_from_html = extract_article_text(it["link"])
        ai = ai_classify(it["title"], it["source"], article_text)

        # Optional filters
        if HIDE_NEUTRAL and ai["sentiment"] == "Neutral":
            continue
        if ai["confidence"] < MIN_CONFIDENCE:
            continue

        # Published time (prefer feed/HTML time) → show as EST
        dt_utc = published_dt_from_entry(it["entry"], published_from_html)
        if not dt_utc:
            dt_utc = datetime.now(timezone.utc)
        dt_est = dt_utc.astimezone(ZoneInfo("America/New_York"))
        when = dt_est.strftime("%-I:%M %p EST • %b %-d")  # e.g., 6:24 PM EST • Oct 12

        # avoid showing a summary identical to the title (unlikely now, but safe)
        summary = (ai.get("summary") or "").strip()
        if summary.lower() == it["title"].strip().lower():
            summary = ""

        # source line + hyperlink if link exists
        if it["link"]:
            src_line = f'🔗 Source: <a href="{html_escape(it["link"])}">{html_escape(it["source"])}</a>'
        else:
            src_line = f"🔗 Source: {html_escape(it['source'])}"

        summary_line = f"✍️ {html_escape(summary)}\n" if summary else ""
        msg = (
            f"📰 {html_escape(it['title'])}\n"
            f"{summary_line}"
            f"{format_sentiment(ai)}\n"
            f"{src_line}\n"
            f"🕒 {html_escape(when)}"
        )

        send_message(msg)
        seen_now.add(uid)
        time.sleep(1)

    if seen_now:
        seen |= seen_now
        save_seen(seen)

def main():
    send_message("✅ SmartFlow News worker started.")
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
