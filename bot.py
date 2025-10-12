import os, time, json, hashlib, feedparser, requests
from datetime import datetime, timezone
from openai import OpenAI
import httpx

# create a plain httpx client; avoids passing unsupported 'proxies' kwarg internally
client = OpenAI(
    api_key=OPENAI_API_KEY,
    http_client=httpx.Client(follow_redirects=True)
)

# -------- settings from environment --------
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY")
SLEEP_SECONDS    = int(os.getenv("SLEEP_SECONDS", "60"))

# Comma-separated list allowed in env; else use defaults
FEEDS_ENV = os.getenv("FEEDS", "").strip()
FEEDS = [u.strip() for u in FEEDS_ENV.split(",") if u.strip()] or [
    "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",          # WSJ Markets
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",  # CNBC Top
    "https://www.reuters.com/rssFeed/businessNews"            # Reuters Business
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


SEEN_PATH = "seen.txt"
seen = set()

def load_seen():
    if os.path.exists(SEEN_PATH):
        with open(SEEN_PATH, "r", encoding="utf-8") as f:
            for line in f:
                seen.add(line.strip())

def save_seen(uid):
    with open(SEEN_PATH, "a", encoding="utf-8") as f:
        f.write(uid + "\n")

def send_message(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.get(url, params={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=20)
    except Exception as e:
        print("Telegram error:", e)

def looks_relevant(title: str) -> bool:
    t = title.lower()
    return any(k in t for k in HIGH_IMPACT_TERMS)

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
            model="gpt-4o-mini",             # fast & cost-effective
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
        data["confidence"] = int(float(data.get("confidence", 60)))
        if not isinstance(data.get("tags", []), list): data["tags"] = []
        return data
    except Exception as e:
        print("AI error:", e)
        return {"summary": title, "sentiment":"Neutral","confidence":50,"tags":[]}

def fetch_once(limit_per_feed=3):
    for url in FEEDS:
        feed = feedparser.parse(url)
        src = (getattr(getattr(feed, "feed", None), "title", None) or "Feed").strip()
        for entry in feed.entries[:limit_per_feed]:
            title = entry.title.strip()
            uid = hashlib.sha1(f"{src}||{title}".encode("utf-8")).hexdigest()
            if uid in seen: 
                continue
            if not looks_relevant(title):
                continue
            ai = ai_classify(title, src)
            ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
            msg = (
                f"📰 {title}\n"
                f"✍️ {ai['summary']}\n"
                f"📊 Sentiment: {ai['sentiment']} ({ai['confidence']}%)"
                + (f"\n🏷️ Tags: {', '.join(ai['tags'])}" if ai['tags'] else "")
                + f"\n⏱️ {ts}"
            )
            send_message(msg)
            seen.add(uid)
            save_seen(uid)
            time.sleep(1)

def main():
    load_seen()
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
