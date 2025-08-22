# Latest-Funding-News-In-Automation F&B-
# F&B Automation Funding Scraper

This small tool searches Google (via **Programmable Search Engine/CSE**) for **latest funding** news in **restaurant automation / food robotics**, then visits each article and tries to extract:
- Company (heuristic from title)
- Round (Seed/Series A/B/C...)
- Amount (normalized to USD)
- Investors (simple clause extraction)
- Publish date
- Source URL & snippet

## Why CSE (and not raw Google scraping)?
Scraping Google SERPs directly violates Google's Terms of Service. Use **Programmable Search Engine** or a compliant API (e.g., SerpAPI) to stay within policy.

## Quick Start

1. **Set environment variables**
   ```bash
   export GOOGLE_API_KEY=<your_api_key>
   export GOOGLE_CSE_ID=<your_cse_id>
   ```

2. **Output**
   - `data/funding_YYYY-MM-DD.csv` (append)
   - `data/funding_latest.csv` (fresh snapshot)

## Tuning What It Finds
- Edit `queries.txt` to add/remove keyword phrases.
- Adjust `DEFAULT_SITES` and `FUNDING_KEYWORDS` in `funding_scraper.py`.
- Raise `--limit` per query if you want more results (CSE caps pages).

## Scheduling
On macOS/Linux, cron example (run daily 8:30am):
```
30 8 * * * cd /path/to/project && /usr/bin/env bash -lc 'export GOOGLE_API_KEY=xxx; export GOOGLE_CSE_ID=yyy; /usr/bin/python3 funding_scraper.py --days 60 --limit 100 >> scrape.log 2>&1'
```

## Notes & Caveats
- Amount/round/investors extraction is **best-effort** using regex and simple heuristics. For production-grade accuracy, consider a small LLM or a ruleset per publisher.
- Respect robots.txt for any secondary page fetches, add delays (`--sleep`) to be nice.
- If you have access to **Crunchbase/PitchBook/CB Insights APIs**, prefer those sources for structured data (and obey their terms).
- You can also add **RSS feeds** from key outlets to reduce API calls and improve freshness.
