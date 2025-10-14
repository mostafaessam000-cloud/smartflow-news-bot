# bot.py
# SmartFlow: US Market + ForexFactory Calendar -> Telegram
# Sentiment is NASDAQ-focused: 🟨 NASDAQ Neutral / 🔺 NASDAQ Bullish / 🔻 NASDAQ Bearish

import os, time, json, hashlib, re, html, feedparser, requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from email.utils import parsedate_to_datetime
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET

# ====== REQUIRED SECRETS (either hardcode here OR provide via environment) ======
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "PUT_YOUR_TELEGRAM_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "PUT_YOUR_TELEGRAM_CHAT_ID_HERE")
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY",   "PUT_YOUR_OPENAI_API_KEY_HERE")  # leave blank "" to disable AI

# ====== RUNTIME SETTINGS (kept in code; override by ENV only if you want) ======
SLEEP_SECONDS       = int(os.getenv("SLEEP_SECONDS", "60"))      # main loop wait
MAX_AGE_HOURS       = float(os.getenv("MAX_AGE_HOURS", "4"))     # only post fresh items
MAX_POSTS_PER_CYCLE = int(os.getenv("MAX_POSTS_PER_CYCLE", "25"))
NEWS_TIMEOUT        = int(os.getenv("NEWS_TIMEOUT", "25"))

USE_FOREX_FACTORY   = os.getenv("USE_FOREX_FACTORY", "true").lower() != "false"
FF_URL              = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
FF_LOOKAHEAD_MIN    = int(os.getenv("FF_LOOKAHEAD_MIN", "5"))    # pre-alert window
FF_RESULT_WINDOW_MIN= int(os.getenv("FF_RESULT_WINDOW_MIN","15"))# post window

EST = ZoneInfo("America/New_York")
UTC = timezone.utc

# Store dedupe IDs locally
SEEN_NEWS_PATH = "seen_news.txt"
FF_REMIND_PATH = "ff_reminded.txt"
FF_RESULT_PATH = "ff_released.txt"

# ============ FEEDS: broad US / NASDAQ-relevant firehose ============
# (some sites may occasionally return 403; code is resilient and continues)
FEEDS = [
    # Reuters
    "https://www.reuters.com/markets/us/rss",
    "https://www.reuters.com/markets/earnings/rss",
    "https://www.reuters.com/technology/rss",

    # CNBC
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",   # Top
    "https://www.cnbc.com/id/100727362/device/rss/rss.html",   # Tech

    # Yahoo Finance / MarketWatch
    "https://finance.yahoo.com/news/rssindex",
    "https://www.marketwatch.com/rss/topstories",

    # AP Business / CNN Business / ABC / CBS / BBC / NYT (business)
    "https://apnews.com/hub/business?output=rss",
    "https://rss.cnn.com/rss/money_news_international.rss",
    "https://abcnews.go.com/abcnews/moneyheadlines",           # HTML page with feed tags; feedparser handles
    "https://www.cbsnews.com/latest/rss/business",
    "https://feeds.bbci.co.uk/news/business/rss.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",

    # NASDAQ official
    "https://www.nasdaq.com/feed/rssoutbound?category=MarketNews",
    "https://www.nasdaq.com/feed/rssoutbound?category=Earnings",

    # US Gov / Policy (press -> markets)
    "https://www.whitehouse.gov/briefing-room/feed/",
    "https://home.treasury.gov/news/press-releases/all/feed",
    "https://www.federalreserve.gov/feeds/press_all.xml",
    "https://www.federalreserve.gov/feeds/press_monetary.xml",
    "https://www.bls.gov/feed/news.rss",
    "https://www.bea.gov/news/rss.xml",
    "https://www.sec.gov/news/pressreleases.rss",
    "https://www.sec.gov/news/speeches.rss",
]

# ---------- Optional OpenAI client ----------
USE_AI = bool(OPENAI_API_KEY.strip())
_client = None
if USE_AI:
    try:
        from openai import OpenAI
        _client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        print("OpenAI init error:", e)
        USE_AI = False

UA = {"User-Agent": "Mozilla/5.0 (compatible; SmartFlowBot/1.0; +https://example.com/bot)"}

# ---------- Utilities ----------
def html_escape(s: str) -> str:
    return html.escape(s or "")

def sha_uid(*parts) -> str:
    return hashlib.sha1(("||".join(parts)).encode("utf-8")).hexdigest()

def read_ids(path: str) -> set[str]:
    s = set()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    s.add(line)
    return s

def append_id(path: str, id_: str):
    with open(path, "a", encoding="utf-8") as f:
        f.write(id_ + "\n")

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

def is_fresh(utc_dt: datetime | None, hours: float) -> bool:
    if not utc_dt:
        # if missing, consider "fresh" to avoid dropping real-time items
        return True
    return (datetime.now(UTC) - utc_dt) <= timedelta(hours=hours)

def tg_send(text: str):
    try:
        requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            params={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=NEWS_TIMEOUT,
        )
    except Exception as e:
        print("Telegram error:", e)

def fetch_article_text(url: str) -> str:
    """Lightweight article text fetch using BeautifulSoup (best-effort)."""
    try:
        r = requests.get(url, headers=UA, timeout=NEWS_TIMEOUT)
        if not r.ok:
            return ""
        soup = BeautifulSoup(r.content, "html.parser")
        # prefer article/body text blocks
        for selector in ["article", "main", "div#content", "div.story", "div.article"]:
            node = soup.select_one(selector)
            if node:
                txt = " ".join(node.get_text(separator=" ").split())
                return txt[:4000]
        # fallback: whole-page text (trim)
        txt = " ".join(soup.get_text(separator=" ").split())
        return txt[:4000]
    except Exception:
        return ""

# ---------- AI: NASDAQ sentiment + 1-line summary ----------
def classify_and_summarize_for_nasdaq(title: str, source: str, link: str, snippet: str):
    """
    Returns (label_text, arrow, summary_text) where:
      label_text ∈ {"NASDAQ Bullish","NASDAQ Bearish","NASDAQ Neutral"}
      arrow ∈ {"🔺","🔻","🟨"}
      summary_text: <= 25 words, trader-focused (may be empty if AI disabled or fails)
    """
    if not USE_AI or not _client:
        return ("NASDAQ Neutral", "🟨", "")

    try:
        sys = (
            "You are a veteran US equities day trader. "
            "Classify the headline's immediate impact on NASDAQ (US tech-heavy index) "
            "as Bullish, Bearish, or Neutral, and provide a 1-sentence summary (<=25 words) "
            "focused on trading impact. Consider: Fed policy, yields, inflation, geopolitics, "
            "earnings, guidance, chips/AI, regulation, fiscal news, big-cap tech moves."
            "\nReturn JSON: {\"sentiment\":\"Bullish|Bearish|Neutral\",\"summary\":\"...\"}"
        )
        user = f"Source: {source}\nHeadline: {title}\nURL: {link}\nContext:\n{snippet[:1200]}"
        resp = _client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"system","content":sys},
                      {"role":"user","content":user}],
            temperature=0.1,
        )
        txt = resp.choices[0].message.content.strip()
        data = json.loads(txt) if txt.strip().startswith("{") else {}
        sent = str(data.get("sentiment","Neutral")).strip().title()
        summ = str(data.get("summary","")).strip()[:300]
    except Exception as e:
        print("AI classify error:", e)
        sent, summ = "Neutral", ""

    if sent == "Bullish":
        return ("NASDAQ Bullish", "🔺", summ)
    if sent == "Bearish":
        return ("NASDAQ Bearish", "🔻", summ)
    return ("NASDAQ Neutral", "🟨", summ)

# ---------- RSS parsing ----------
def parse_feed_with_ua(url: str):
    """Fetch with UA to reduce 403s, then feedparser on bytes."""
    try:
        r = requests.get(url, headers=UA, timeout=NEWS_TIMEOUT)
        if r.ok:
            return feedparser.parse(r.content)
    except Exception:
        pass
    # fallback
    try:
        return feedparser.parse(url)
    except Exception:
        return None

def harvest_feeds(feed_urls: list[str]):
    items = []
    for url in feed_urls:
        feed = parse_feed_with_ua(url)
        if not feed:
            print("Parse error (feed):", url)
            continue
        src_title = (getattr(getattr(feed,"feed",None),"title",None) or "Feed").strip()
        for e in getattr(feed, "entries", []):
            title = (getattr(e, "title", "") or "").strip()
            link  = (getattr(e, "link", "") or "").strip()
            summary = (getattr(e, "summary", "") or getattr(e, "description","") or "").strip()
            pub = parse_pub(getattr(e, "published", None))
            items.append({
                "title": title,
                "link": link,
                "source": src_title,
                "summary": summary,
                "published": pub
            })
    return items

def send_news_batch():
    seen = read_ids(SEEN_NEWS_PATH)

    items = harvest_feeds(FEEDS)
    items.sort(key=lambda x: x.get("published") or datetime.now(UTC), reverse=True)

    sent = 0
    for it in items:
        title = it["title"]; link = it["link"]; src = it["source"]; pub = it["published"]
        if not title or not link:
            continue
        if not is_fresh(pub, MAX_AGE_HOURS):
            continue

        uid = sha_uid(src, title, link)
        if uid in seen:
            continue

        # Pull some article text to help AI
        body = it["summary"] or fetch_article_text(link)

        label, arrow, summ = classify_and_summarize_for_nasdaq(title, src, link, body)
        when = utc_to_est(pub)
        src_line = f"🔗 <i>Source:</i> {html_escape(src)} —\n{html_escape(link)}" if link else f"🔗 <i>Source:</i> {html_escape(src)}"

        # First line sentiment per your spec:
        msg = (
            f"{arrow} <b>{label}</b>\n"
            f"📰 {html_escape(title)}\n" +
            (f"✍️ {html_escape(summ)}\n" if summ else "") +
            f"{src_line}\n" +
            (f"🕒 {html_escape(when)}" if when else "")
        )

        tg_send(msg)
        append_id(SEEN_NEWS_PATH, uid)
        sent += 1
        time.sleep(0.5)
        if MAX_POSTS_PER_CYCLE and sent >= MAX_POSTS_PER_CYCLE:
            break

# ---------- ForexFactory calendar ----------
def _try_float(x: str | None):
    try:
        if x is None:
            return None
        val = re.sub(r"[^\d\.\-]", "", x)
        if val in {"", ".", "-"}:
            return None
        return float(val)
    except Exception:
        return None

def parse_ff_calendar():
    out = []
    try:
        r = requests.get(FF_URL, headers=UA, timeout=NEWS_TIMEOUT)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        print("FF fetch error:", e)
        return out

    for ev in root.findall(".//event"):
        country = (ev.findtext("country") or "").strip()
        impact  = (ev.findtext("impact")  or "").strip().lower()
        if country != "USD" or "high" not in impact:
            continue

        title     = (ev.findtext("title") or "").strip()
        date_str  = (ev.findtext("date")  or "").strip()
        time_str  = (ev.findtext("time")  or "").strip()
        tz_str    = (ev.findtext("timezone") or "UTC").strip()
        forecast  = (ev.findtext("forecast") or "").strip()
        previous  = (ev.findtext("previous") or "").strip()
        actual    = (ev.findtext("actual") or "").strip()
        ev_id     = (ev.findtext("id") or sha_uid(title, date_str, time_str))

        if not time_str or time_str.lower().startswith("all"):
            # no specific time = skip real-time alerts
            continue

        try:
            dt_local = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
            if "est" in tz_str.lower() or "edt" in tz_str.lower() or "new york" in tz_str.lower():
                dt_utc = dt_local.replace(tzinfo=EST).astimezone(UTC)
            else:
                dt_utc = dt_local.replace(tzinfo=UTC)
        except Exception:
            continue

        out.append({
            "id": ev_id,
            "title": title,
            "dt_utc": dt_utc,
            "forecast": forecast,
            "previous": previous,
            "actual": actual,
        })
    return out

def infer_direction_for_nasdaq(title: str, actual: str, forecast: str) -> str:
    a = _try_float(actual); f = _try_float(forecast)
    if a is None or f is None:
        return "Neutral"
    t = title.lower()
    # simple heuristics
    if any(k in t for k in ["cpi","pce","ppi","inflation"]):
        return "Bearish" if a > f else ("Bullish" if a < f else "Neutral")
    if "unemployment" in t:
        return "Bearish" if a > f else ("Bullish" if a < f else "Neutral")
    if any(k in t for k in ["nonfarm","payroll"]):
        return "Bullish" if a > f else ("Bearish" if a < f else "Neutral")
    if "retail sales" in t:
        return "Bullish" if a > f else ("Bearish" if a < f else "Neutral")
    if any(k in t for k in ["ism","pmi"]):
        return "Bullish" if a > f else ("Bearish" if a < f else "Neutral")
    return "Neutral"

def send_ff_alerts_and_results():
    if not USE_FOREX_FACTORY:
        return
    reminded = read_ids(FF_REMIND_PATH)
    released = read_ids(FF_RESULT_PATH)
    now = datetime.now(UTC)

    for ev in parse_ff_calendar():
        ev_id = ev["id"]; title = ev["title"]; dt_utc = ev["dt_utc"]
        fcast = ev["forecast"]; prev = ev["previous"]; actual = ev["actual"]

        # Pre-alert 0..FF_LOOKAHEAD_MIN minutes before
        mins_to = (dt_utc - now).total_seconds()/60.0
        if 0 <= mins_to <= FF_LOOKAHEAD_MIN and ev_id not in reminded:
            msg = (
                f"⏳ <b>{html_escape(title)}</b> in ~{int(mins_to)} min\n"
                f"Forecast: {html_escape(fcast or '—')} | Previous: {html_escape(prev or '—')}\n"
                f"🕒 {html_escape(utc_to_est(dt_utc))}"
            )
            tg_send(msg)
            append_id(FF_REMIND_PATH, ev_id)

        # Result 0..FF_RESULT_WINDOW_MIN minutes after (requires 'actual')
        mins_since = (now - dt_utc).total_seconds()/60.0
        if 0 <= mins_since <= FF_RESULT_WINDOW_MIN and ev_id not in released and actual and actual.strip():
            direction = infer_direction_for_nasdaq(title, actual, fcast)
            arrow = "🔺" if direction == "Bullish" else ("🔻" if direction == "Bearish" else "🟨")
            msg = (
                f"📊 <b>{html_escape(title)}</b>\n"
                f"Actual: {html_escape(actual)} | Forecast: {html_escape(fcast or '—')} | Previous: {html_escape(prev or '—')}\n"
                f"{arrow} NASDAQ {direction}\n"
                f"🕒 {html_escape(utc_to_est(dt_utc))}"
            )
            tg_send(msg)
            append_id(FF_RESULT_PATH, ev_id)

# ---------- Main ----------
def main():
    tg_send("✅ SmartFlow NASDAQ bot live — curated US feeds + ForexFactory calendar. AI on.")
    backoff = 5
    while True:
        try:
            send_news_batch()
            send_ff_alerts_and_results()
            time.sleep(SLEEP_SECONDS)
            backoff = 5
        except Exception as e:
            print("Loop error:", e)
            time.sleep(backoff)
            backoff = min(backoff * 2, 180)

if __name__ == "__main__":
    main()
