# bot.py — Gemini simplified (headline-first, max 5 posts per cycle)
import os, re, time, json, hashlib, feedparser, requests
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
from bs4 import BeautifulSoup

# timezone fallback
try:
    from zoneinfo import ZoneInfo
    EST = ZoneInfo("America/New_York")
    _tz_label = "EST"
except Exception:
    EST = timezone.utc
    _tz_label = "UTC"

# ==== env ====
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SLEEP_SECONDS    = int(os.getenv("SLEEP_SECONDS", "120"))
MAX_POSTS_PER_CYCLE = int(os.getenv("MAX_POSTS_PER_CYCLE", "5"))
MAX_AGE_HOURS    = int(os.getenv("MAX_AGE_HOURS", "3"))
GEMINI_API_KEY   = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL     = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

# ==== gemini ====
import google.generativeai as genai
genai.configure(api_key=GEMINI_API_KEY)

GEMINI_SAFETY = [
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUAL_CONTENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]

# ==== feeds (CNN removed) ====
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

# ==== storage ====
SEEN_PATH = "seen.json"
def load_seen():
    try:
        if os.path.exists(SEEN_PATH):
            return set(json.load(open(SEEN_PATH)))
    except Exception: pass
    return set()
def save_seen(s):
    json.dump(list(s), open(SEEN_PATH,"w"))
seen = load_seen()

# ==== helpers ====
def html_escape(s): return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
def normalize_title(t):
    t=re.sub(r"[^\w\s]"," ",t.lower().strip());return re.sub(r"\s+"," ",t)
def make_uid(t): return hashlib.sha1(normalize_title(t).encode()).hexdigest()

def send_message(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            params={"chat_id": TELEGRAM_CHAT_ID,"text":text,"parse_mode":"HTML","disable_web_page_preview":True},timeout=15)
    except Exception as e: print("telegram:",e)

def extract_article_text(url):
    try:
        r=requests.get(url,timeout=10,headers={"User-Agent":"Mozilla/5.0"})
        full=BeautifulSoup(r.text,"html.parser")
        ps=[p.get_text(" ",strip=True) for p in full.find_all("p")]
        txt=" ".join([p for p in ps if len(p)>60])[:5000]
        return txt, None
    except: return "", None

def format_sentiment(ai):
    s=ai.get("sentiment","Neutral").title()
    c=ai.get("confidence",60)
    if s=="Bullish": return f"🟢⬆️ <b>NASDAQ Bullish</b> ({c}%)"
    if s=="Bearish": return f"🔴⬇️ <b>NASDAQ Bearish</b> ({c}%)"
    return f"🟨 NASDAQ Neutral ({c}%)"

def _trim_words(s, n=20): return " ".join((s or "").split()[:n])

# ==== AI ====
def ai_classify(title, source, article_text):
    if not GEMINI_API_KEY: 
        return {"summary":"","sentiment":"Neutral","confidence":60}
    headline=title.strip()
    ctx=article_text.strip() if article_text else ""
    system=("Classify NASDAQ impact from headline (and context if any). "
            "Return JSON with keys: summary(<=20 words, may be empty), "
            "sentiment(Bullish|Bearish|Neutral), confidence(0-100).")
    user=f"Headline: {headline}\nContext: {ctx[:2000]}\nJSON only."
    try:
        model=genai.GenerativeModel(GEMINI_MODEL)
        res=model.generate_content(
            [{"text":system},{"text":user}],
            generation_config={"temperature":0.1,"max_output_tokens":256,"response_mime_type":"application/json"},
            safety_settings=GEMINI_SAFETY)
        raw=(res.text or "").strip()
        data=json.loads(raw[raw.find("{"):raw.rfind("}")+1])
        s=_trim_words((data.get("summary") or "").strip(),20)
        sent=str(data.get("sentiment","Neutral")).title()
        try:c=int(float(data.get("confidence",60)))
        except:c=60
        if s.lower() in {"bullish","bearish","neutral",headline.lower()}: s=""
        return {"summary":s,"sentiment":sent,"confidence":c}
    except Exception as e:
        print("ai error:",e)
        return {"summary":"","sentiment":"Neutral","confidence":60}

# ==== feed parse ====
def parse_feed(url):
    try:
        r=requests.get(url,headers={"User-Agent":"Mozilla/5.0"},timeout=15)
        if r.ok and r.content: return feedparser.parse(r.content)
    except Exception as e: print("feed error:",url,e)
    return None

# ==== main loop ====
def fetch_once():
    global seen
    posted=0
    for url in DEFAULT_FEEDS:
        feed=parse_feed(url)
        if not feed: continue
        src=getattr(feed.feed,"title",url)
        for e in getattr(feed,"entries",[])[:10]:
            title=(getattr(e,"title","") or "").strip()
            link =(getattr(e,"link","") or "").strip()
            if not title or make_uid(title) in seen: continue
            article,_=extract_article_text(link)
            ai=ai_classify(title,src,article)
            dt=datetime.now(timezone.utc)
            dt_est=dt.astimezone(EST)
            when=f"{dt_est.strftime('%-I:%M %p')} {_tz_label} • {dt_est.strftime('%b %-d')}"
            src_line=f'🔗 Source: <a href="{html_escape(link)}">{html_escape(src)}</a>' if link else f"🔗 {src}"
            summ=ai.get("summary","").strip()
            line=f"✍️ {html_escape(summ)}\n" if summ else ""
            msg=f"{format_sentiment(ai)}\n📰 {html_escape(title)}\n{line}{src_line}\n🕒 {when}"
            send_message(msg)
            seen.add(make_uid(title))
            posted+=1
            if posted>=MAX_POSTS_PER_CYCLE: save_seen(seen);return
            time.sleep(1)
    save_seen(seen)

def main():
    send_message("✅ Gemini NASDAQ bot started (simple mode).")
    while True:
        try: fetch_once()
        except Exception as e: print("loop:",e)
        time.sleep(SLEEP_SECONDS)

if __name__=="__main__": main()
