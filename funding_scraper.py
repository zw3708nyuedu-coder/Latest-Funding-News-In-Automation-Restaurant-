#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Restaurant Automation Funding Scraper (filtered)
------------------------------------------------
Uses Google Programmable Search (CSE) to find funding news about
restaurant automation / food robotics, with stricter filters:
  - Excludes job/career results and social noise
  - Enforces recent publish dates (sliding window via --days) and a minimum year
  - Requires funding signals (keywords and/or parsed amount/round)

Usage:
  export GOOGLE_API_KEY=xxx
  export GOOGLE_CSE_ID=yyy
  python3 funding_scraper.py --days 90 --limit 80

Outputs:
  data/funding_YYYY-MM-DD.csv   (append)
  data/funding_latest.csv       (latest snapshot)
"""

import os
import re
import csv
import time
import argparse
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlparse

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

# ---------------------------------
# Config
# ---------------------------------

DEFAULT_SITES = [
    # Tech & funding news
    "techcrunch.com","crunchbase.com","pitchbook.com","cbinsights.com",
    "venturebeat.com","theinformation.com","axios.com","businessinsider.com",
    "forbes.com","reuters.com","bloomberg.com","ft.com",
    # Robotics / industry trades
    "therobotreport.com","robotics247.com","roboticsbusinessreview.com",
    "thespoon.tech","qsrmagazine.com","nrn.com","restaurantdive.com",
    "fastcasual.com","modernrestaurantmanagement.com",
    # Company newsroom / PR
    "prnewswire.com","globenewswire.com","businesswire.com","newswire.com",
    # Hospitality trades
    "asianhospitality.com","hotelmanagement.net","hospitalitynet.org"
]

FUNDING_KEYWORDS = [
    "funding","raises","raised","raise","series A","series B","series C",
    "series D","seed round","pre-seed","angel round","venture funding","equity financing",
    "convertible note","round led by","led by","investment","invests","backs"
]

# Strong funding signals (title/snippet/body should hit at least one category)
FUNDING_HARD_KEYWORDS = {
    "raises","raised","raise","funding","series a","series b","series c",
    "series d","seed round","pre-seed","angel round","investment round",
    "round led by","led by","backs","invests in","equity financing",
    "venture funding"
}

DOMAIN_EXCLUDES = {"facebook.com","x.com","twitter.com","linkedin.com","youtube.com","medium.com"}

# Job/career filters
JOB_DOMAINS = {
    "talents.vaia.com","boards.greenhouse.io","jobs.lever.co","lever.co",
    "careers.google.com","jobs.workable.com","workable.com","smartrecruiters.com",
    "indeed.com","linkedin.com","glassdoor.com","angel.co","wellfound.com",
    "monster.com","ziprecruiter.com","jobvite.com"
}
JOB_KEYWORDS = {
    "job","jobs","career","careers","apply","hiring","recruit","recruiting",
    "talent","vacancy","position","opening","role"
}

# Date/amount gates
MIN_YEAR = 2018
MIN_AMOUNT_FOR_SIGNAL = 100_000  # USD

AMOUNT_PAT = re.compile(r"""(?ix)
    (?<!\w)
    (?:US\$|\$)?\s*
    (\d{1,3}(?:[,\s]\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)
    \s*
    (billion|bn|b|million|mm|m|thousand|k)?
    \s*
    (?:USD|US\s*dollars|dollars)?
""")

ROUND_PAT = re.compile(r"""(?i)\b(pre-?seed|seed|angel|series\s+[A-K]|growth\s+equity|mezzanine|venture\s+debt)\b""")
INVESTOR_PAT = re.compile(r"""(?i)\b(led by|co-led by|participat(?:ed|ing) (?:from|by)|back(?:ed)? by|invest(?:ed|s) by)\b[^.]{0,200}\.""")
DATE_PAT = re.compile(r"""(?ix)\b(?:on\s+)?((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?\s+\d{1,2},\s+\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})\b""")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; FundingBot/1.1; +https://example.com/bot)"}

# ---------------------------------
# Helpers
# ---------------------------------

def normalize_amount(num_str, scale):
    try:
        num = float(num_str.replace(",", "").strip())
    except Exception:
        return None
    scale = (scale or "").lower()
    if scale in ("billion","bn","b"):
        num *= 1_000_000_000
    elif scale in ("million","mm","m"):
        num *= 1_000_000
    elif scale in ("thousand","k"):
        num *= 1_000
    return int(num)

def extract_article_fields(html):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator=" ", strip=True)

    # Amounts
    amounts = []
    for m in AMOUNT_PAT.finditer(text):
        amt = normalize_amount(m.group(1), m.group(2))
        if amt and 10_000 <= amt <= 10_000_000_000:
            amounts.append(amt)
    amount = max(amounts) if amounts else ""

    # Round
    round_match = ROUND_PAT.search(text)
    round_str = round_match.group(1).title() if round_match else ""

    # Investors (first matched clause)
    investors = ""
    inv_match = INVESTOR_PAT.search(text)
    if inv_match:
        investors = inv_match.group(0)

    # Publish date (meta first, fallback to body)
    pub_date = ""
    for tag in [
        {"attr": "property", "value": "article:published_time"},
        {"attr": "name", "value": "date"},
        {"attr": "name", "value": "pubdate"},
        {"attr": "itemprop", "value": "datePublished"},
    ]:
        el = soup.find(attrs={tag["attr"]: tag["value"]})
        if el and (content := el.get("content")):
            try:
                dt = dateparser.parse(content)
                if dt:
                    pub_date = dt.date().isoformat()
                    break
            except Exception:
                pass

    if not pub_date:
        m = DATE_PAT.search(text)
        if m:
            try:
                dt = dateparser.parse(m.group(1))
                if dt:
                    pub_date = dt.date().isoformat()
            except Exception:
                pass

    title = (soup.title.string.strip() if soup.title and soup.title.string else "")
    return {"title": title, "amount_usd": amount, "round": round_str, "investors": investors, "pub_date": pub_date}

def google_cse_search(api_key, cse_id, query, start_index=1):
    params = {"key": api_key, "cx": cse_id, "q": query, "num": 10, "start": start_index, "safe": "off"}
    url = "https://www.googleapis.com/customsearch/v1?" + urlencode(params)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()

def fetch_url(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code == 200 and "text/html" in r.headers.get("content-type", ""):
            return r.text
    except requests.RequestException:
        return None
    return None

def should_skip_result(link: str, title: str) -> bool:
    try:
        host = urlparse(link).netloc.replace("www.", "").lower()
    except Exception:
        host = ""
    tl = (title or "").lower()
    lk = (link or "").lower()

    # Job/career filters
    if host in JOB_DOMAINS:
        return True
    if any(k in tl or k in lk for k in JOB_KEYWORDS):
        return True

    return False

# ---------------------------------
# Main
# ---------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=90, help="Look-back window (days) for publish date")
    parser.add_argument("--limit", type=int, default=80, help="Max results per query from CSE")
    parser.add_argument("--queries", type=str, default="queries.txt", help="Seed queries file")
    parser.add_argument("--outfile", type=str, default="", help="Override output CSV path")
    parser.add_argument("--sleep", type=float, default=1.0, help="Delay between requests (seconds)")
    args = parser.parse_args()

    api_key = os.getenv("GOOGLE_API_KEY")
    cse_id  = os.getenv("GOOGLE_CSE_ID")
    if not api_key or not cse_id:
        raise SystemExit("Please set GOOGLE_API_KEY and GOOGLE_CSE_ID environment variables.")

    with open(args.queries, "r", encoding="utf-8") as f:
        query_lines = [q.strip() for q in f if q.strip() and not q.strip().startswith("#")]

    out_dir = Path("data")
    out_dir.mkdir(exist_ok=True)
    today = datetime.utcnow().date().isoformat()
    outfile = args.outfile or str(out_dir / f"funding_{today}.csv")
    latestfile = str(out_dir / "funding_latest.csv")

    fieldnames = ["found_at","query","source_url","source_domain","title","amount_usd","round","investors","pub_date","snippet"]
    seen = set()

    with open(outfile, "a", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        if csvfile.tell() == 0:
            writer.writeheader()

        for base_q in query_lines:
            site_q = " OR ".join([f"site:{d}" for d in DEFAULT_SITES])
            funding_q = " OR ".join([f'"{k}"' for k in FUNDING_KEYWORDS])
            # Exclude job-ish words right at the query level to reduce noise
            neg = ' -"job" -"jobs" -"career" -"careers" -"apply" -"hiring" -"recruit" -"talent"'
            full_q = f'({base_q}) AND ({funding_q}) AND ({site_q}){neg}'

            fetched = 0
            start_index = 1
            while fetched < args.limit:
                data = google_cse_search(api_key, cse_id, full_q, start_index=start_index)
                items = data.get("items", [])
                if not items:
                    break
                for it in items:
                    link = it.get("link")
                    if not link:
                        continue

                    try:
                        domain = urlparse(link).netloc.replace("www.", "")
                    except Exception:
                        domain = ""

                    if domain in DOMAIN_EXCLUDES:
                        continue

                    title = it.get("title", "")
                    snippet = it.get("snippet", "")

                    # Skip job/career results early
                    if should_skip_result(link, title):
                        continue

                    key = (domain, link)
                    if key in seen:
                        continue
                    seen.add(key)

                    html = fetch_url(link)
                    time.sleep(args.sleep)

                    row = {
                        "found_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                        "query": base_q,
                        "source_url": link,
                        "source_domain": domain,
                        "title": title,
                        "amount_usd": "",
                        "round": "",
                        "investors": "",
                        "pub_date": "",
                        "snippet": snippet,
                    }

                    ok_to_write = True

                    if html:
                        fields = extract_article_fields(html)
                        row.update(fields)

                        # ---- Date gate: must be within --days and >= MIN_YEAR ----
                        pub_date = row.get("pub_date") or ""
                        try:
                            dt = dateparser.parse(pub_date).date() if pub_date else None
                        except Exception:
                            dt = None

                        if dt is None:
                            ok_to_write = False
                        else:
                            cutoff = datetime.utcnow().date() - timedelta(days=args.days)
                            today_d = datetime.utcnow().date()
                            if dt < cutoff or dt.year < MIN_YEAR or dt > today_d:
                                ok_to_write = False

                        # ---- Funding signal gate ----
                        title_l = (row.get("title") or "").lower()
                        snip_l  = (row.get("snippet") or "").lower()

                        text_signal = any(k in title_l or k in snip_l for k in FUNDING_HARD_KEYWORDS)

                        amt_signal = False
                        try:
                            amt = int(row.get("amount_usd") or 0)
                            if amt >= MIN_AMOUNT_FOR_SIGNAL:
                                amt_signal = True
                        except Exception:
                            pass

                        rnd_signal = bool(row.get("round"))

                        if not (text_signal or amt_signal or rnd_signal):
                            ok_to_write = False

                    else:
                        # No HTML: rely on title/snippet keywords (strict)
                        title_l = (title or "").lower()
                        snip_l  = (snippet or "").lower()
                        if not any(k in title_l or k in snip_l for k in FUNDING_HARD_KEYWORDS):
                            ok_to_write = False

                    if not ok_to_write:
                        continue

                    writer.writerow(row)
                    fetched += 1
                    if fetched >= args.limit:
                        break

                start_index += 10
                if start_index > 100:  # CSE page window
                    break

    # latest snapshot
    import shutil
    shutil.copyfile(outfile, latestfile)
    print(f"Saved: {outfile}")
    print(f"Latest: {latestfile}")

if __name__ == "__main__":
    main()
