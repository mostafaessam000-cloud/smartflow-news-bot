# bot.py — Keyword-only NASDAQ news (no AI)
import os, re, time, json, hashlib, requests, feedparser
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from email.utils import parsedate_to_datetime

try:
    from zoneinfo import ZoneInfo
    EST = ZoneInfo("America/New_York"); _tz_label = "EST"
except Exception:
    EST = timezone.utc; _tz_label = "UTC"

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SLEEP_SECONDS       = 300    # 5 minutes
MAX_POSTS_PER_CYCLE = 5
MAX_AGE_MINUTES     = 15     # 15 minutes freshness

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

SEEN_PATH, SEEN_LIMIT = "seen.json", 5000
def load_seen():
    if os.path.exists(SEEN_PATH):
        try:
            data = json.load(open(SEEN_PATH, "r", encoding="utf-8"))
            return set(data if isinstance(data, list) else [])
        except: pass
    return set()
def save_seen(s:set):
    if len(s) > SEEN_LIMIT: s = set(list(s)[-SEEN_LIMIT:])
    json.dump(list(s), open(SEEN_PATH,"w",encoding="utf-8"))
seen = load_seen()

UA = {"User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36"}
def html_escape(s:str)->str: return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
def normalize_title(t:str)->str: return re.sub(r"\s+"," ",re.sub(r"[^\w\s]"," ",(t or "").lower())).strip()
def make_uid(t:str)->str: return hashlib.sha1(normalize_title(t).encode("utf-8")).hexdigest()
def send_message(text:str):
    try:
        requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                     params={"chat_id":TELEGRAM_CHAT_ID,"text":text,"parse_mode":"HTML",
                             "disable_web_page_preview":True},timeout=15)
    except Exception as e: print("telegram:",e)

def publisher_from_link(link:str,fallback:str)->str:
    try:
        host=urlparse(link).netloc.lower(); dom=".".join(host.split(".")[-2:]) if "." in host else host
        labels={"reuters.com":"Reuters","cnbc.com":"CNBC","marketwatch.com":"MarketWatch",
                "nasdaq.com":"Nasdaq","finance.yahoo.com":"Yahoo Finance","apnews.com":"AP News",
                "theguardian.com":"The Guardian","cbsnews.com":"CBS","abcnews.go.com":"ABC News",
                "bbc.com":"BBC","bbc.co.uk":"BBC"}
        return labels.get(dom,fallback)
    except: return fallback

def published_dt(entry)->datetime|None:
    for a in ("published_parsed","updated_parsed"):
        t=getattr(entry,a,None)
        if t: return datetime(*t[:6], tzinfo=timezone.utc)
    for a in ("published","updated","created"):
        s=getattr(entry,a,None)
        if s:
            try:
                dt=parsedate_to_datetime(s)
                if not dt.tzinfo: dt=dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except: pass
    return None

def human_ago(delta: timedelta)->str:
    m=int(delta.total_seconds()//60)
    if m<1: return "just now"
    if m<90: return f"{m} min ago"
    return f"{m//60} hr ago"

def parse_feed(url:str):
    try:
        r=requests.get(url, headers=UA, timeout=15)
        if r.ok and r.content: return feedparser.parse(r.content)
    except: pass
    return None

# --- Market keywords ---
KEYWORDS = [
    "nasdaq","stocks","equities","risk","volatility","fed","powell","fomc","yields","treasury",
    "inflation","cpi","ppi","jobs","payrolls","nfp","guidance","earnings","outlook","revenue","profit",
    "tariff","ban","sanction","export control","chip","semiconductor","ai","tech","big tech","mega-cap",
    "downgrade","upgrade","cut rates","rate cut","rate hike","recession","guidance","misses","beats",
    "yields rise","inflation cools","inflation data","central bank","interest rates","bond yields",
]

def has_keywords(t:str)->bool:
    t=t.lower()
    return any(k in t for k in KEYWORDS)

def fetch_once():
    global seen
    items=[]
    now_utc=datetime.now(timezone.utc)
    for url in FEEDS:
        feed=parse_feed(url)
        if not feed: continue
        src=(getattr(getattr(feed,"feed",None),"title",None) or url).strip()
        for e in getattr(feed,"entries",[])[:10]:
            title=(getattr(e,"title","") or "").strip()
            link=(getattr(e,"link","") or "").strip()
            if not title: continue
            if not has_keywords(title): continue
            dt=published_dt(e) or now_utc
            if (now_utc - dt) > timedelta(minutes=MAX_AGE_MINUTES): continue
            uid=make_uid(title)
            if uid in seen: continue
            items.append({"title":title,"src":src,"link":link,"dt":dt})
    items.sort(key=lambda x:x["dt"], reverse=True)
    posted=0
    for it in items[:MAX_POSTS_PER_CYCLE]:
        dt_est=it["dt"].astimezone(EST)
        when=f"{dt_est.strftime('%-I:%M %p ')}{_tz_label} • {dt_est.strftime('%b %-d')} ({human_ago(datetime.now(timezone.utc)-it['dt'])})"
        src=publisher_from_link(it["link"],it["src"])
        msg=(f"📰 {html_escape(it['title'])}\n"
             f"🔗 <a href=\"{html_escape(it['link'])}\">{html_escape(src)}</a>\n"
             f"🕒 {html_escape(when)}")
        send_message(msg)
        seen.add(make_uid(it["title"]))
        posted+=1
        time.sleep(1)
    if posted: save_seen(seen)

def main():
    send_message("✅ Keyword NASDAQ news bot started (every 5 min, ≤15 min old).")
    while True:
        try: fetch_once()
        except Exception as e: print("loop error:",e)
        time.sleep(SLEEP_SECONDS)

if __name__ == "__main__":
    main()
