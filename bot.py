import os, re, time, json, hashlib, feedparser, requests
from datetime import datetime, timezone
from openai import OpenAI
import httpx

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

# Feeds
FEEDS_ENV  = os.getenv("FEEDS", "").strip()
EXTRA_RSS  = os.getenv("EXTRA_RSS", "").strip()  # put X/Truth RSS here if you have them
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

# OpenAI client (httpx avoids proxy kwarg issue)
client = OpenAI(
    api_key=OPENAI_API_KEY,
    http_client=httpx.Client(follow_redirects=True)
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
    # trim to recent N
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

def send_message(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.get(url, params={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True}, timeout=20)
    except Exception as e:
        print("Telegram error:", e)

def ai_classify(title: str, source: str):
    system = (
        "You are a fast market analyst for a NASDAQ trader. "
        "In <=25 words, summarize the headline and classify overall impact on NASDAQ as "
        "Bullish, Bearish, or Neutral. Give confidence 0-100 and 1-3 tags "
        "(Tariff, China, Fed, CPI, NFP, Regulation, War, Energy, AI, Earnings, Sanctions, Rates, Yields, FX). "
        "Return JSON with keys: summary, sentiment, confidence, tags."
    )
    user = f"Source: {source}\nHeadline: {title}\nReturn JSON only."
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
                items.append({"source": src, "title": title, "link": link})
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

        ai = ai_classify(it["title"], it["source"])
        # Optional filters
        if HIDE_NEUTRAL and ai["sentiment"] == "Neutral":
            continue
        if ai["confidence"] < MIN_CONFIDENCE:
            continue

        ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
        src_line = f"🔗 Source: {it['source']}" + (f" — {it['link']}" if it['link'] else "")
        msg = (
            f"📰 {it['title']}\n"
            f"✍️ {ai['summary']}\n"
            f"📊 Sentiment: {ai['sentiment']} ({ai['confidence']}%)"
            + (f"\n🏷️ Tags: {', '.join(ai['tags'])}" if ai['tags'] else "")
            + f"\n{src_line}"
            + f"\n⏱️ {ts}"
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
