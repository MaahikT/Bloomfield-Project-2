"""
news.py - the distress-news searcher (signals: foreclosure, receivership, lis pendens, UCC sale, auction, lender lawsuit).

Court and lien filings come LATE, so these are a "ready to act" confirmation, not an early warning. The bot reads
the news feeds you list in config/bot.yaml (and, when a search service is on, news search results), keeps only
commercial-property items, and saves each one as a filing marked needs_review: a keyword match is a lead, not a
fact, so a person confirms it. The saved sentence is copied word for word from the article.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import date

from collectors.common import config, db, fetch
from collectors.common.log import get_logger
from scout_reader_schema import upsert

from .opening import page_text
from .search import Budget, allowed_domain, search

log = get_logger("bot.news")
COLLECTOR = "bot/news.py"
ADDRESS = re.compile(r"\b\d{2,5}\s+(?:[NSEW]\.?\s+)?[A-Z][A-Za-z0-9.'-]*(?:\s+[A-Z][A-Za-z0-9.'-]*){0,3}\s+"
                     r"(?:St|Street|Ave|Avenue|Blvd|Boulevard|Rd|Road|Dr|Drive|Way|Pkwy|Parkway|Ln|Lane|Ct|Court|Pl|Place)\b\.?")


def classify(text: str, cfg: dict) -> dict | None:
    """The first rule whose words appear, if the item is about commercial property. None if not."""
    nc = cfg.get("distress_news") or {}
    low = text.lower()
    if any(w in low for w in nc.get("residential_words", [])):
        return None
    if not any(w in low for w in nc.get("commercial_words", [])):
        return None
    for rule in nc.get("rules", []):
        if any(w in low for w in rule["words"]) and (not rule.get("needs") or any(w in low for w in rule["needs"])):
            return rule
    return None


def sentence_with(text: str, words: list[str]) -> str | None:
    for s in re.split(r"(?<=[.!?])\s+", text):
        if any(w in s.lower() for w in words) and 20 <= len(s) <= 300:
            return s.strip()
    return None


def read_feed(url: str, session=None) -> list[dict]:
    resp = fetch.polite_get(url, config.domain_settings(fetch.urlparse(url).netloc), session=session)
    if resp is None:
        return []
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError:
        log.warning("%s is not a readable feed", url)
        return []
    items = []
    for it in root.iter():
        if it.tag.split("}")[-1] in ("item", "entry"):
            g = {c.tag.split("}")[-1]: (c.text or c.attrib.get("href") or "") for c in it}
            link = g.get("link") or g.get("id") or ""
            if link:
                items.append({"title": g.get("title", ""), "url": link.strip(),
                              "snippet": re.sub(r"<[^>]+>", " ", g.get("description") or g.get("summary") or "")})
    return items


def candidates(cfg: dict, budget: Budget, session=None) -> list[dict]:
    nc = cfg.get("distress_news") or {}
    out = []
    for feed in nc.get("rss_feeds") or []:
        out += [{**i, "source_name": feed.get("name") or feed["url"]} for i in read_feed(feed["url"], session)]
    for c in nc.get("cities") or []:
        for q in nc.get("queries") or []:
            out += [{**h, "source_name": "search", "city": c.get("city"), "state": c.get("state")}
                    for h in search(q.format(city=c.get("city", ""), state=c.get("state", "")), cfg, budget, session)]
    seen, uniq = set(), []
    for i in out:
        if i["url"] not in seen and allowed_domain(i["url"], cfg):
            seen.add(i["url"])
            uniq.append(i)
    return uniq


def run(con, cfg: dict, budget: Budget, today: date | None = None, session=None) -> dict:
    summary = {"items_seen": 0, "saved": 0, "skipped": 0}
    for item in candidates(cfg, budget, session):
        summary["items_seen"] += 1
        quick = classify(item["title"] + " " + item["snippet"], cfg)
        if quick is None:                                      # cheap pre-check before opening the article
            summary["skipped"] += 1
            continue
        if not budget.take_page():
            log.info("page limit reached; stopping the news searcher")
            break
        res = fetch.fetch_and_store(con, item["url"], config.domain_settings(fetch.urlparse(item["url"]).netloc),
                                    source="news", doc_category="news_article", collector_name=COLLECTOR, session=session,
                                    state=item.get("state"), city=item.get("city"), notes=f"Found by the web bot: {item['title'][:120]}")
        if res is None or res.status != "new":
            summary["skipped"] += 1
            continue
        text = page_text(config.from_stored_path(res.file_path).read_text(encoding="utf-8", errors="replace"))
        rule = classify(text, cfg) or quick
        quote = sentence_with(text, rule["words"])
        if not quote:
            summary["skipped"] += 1
            continue
        addr = ADDRESS.search(text)
        upsert(con, "filing_fields", {
            "file_id": res.file_id, "filing_type": rule["filing_type"], "property_address": addr.group(0) if addr else None,
            "property_state": item.get("state"), "property_city": item.get("city"), "project_name": item["title"][:150],
            "filing_date": today or date.today(), "distress_tags": json.dumps([rule["tag"]]),
            "quotes": json.dumps({"filing_type": quote}), "confidence": "low", "status": "needs_review",
            "review_reason": "Found by the web bot from a news item by keyword. A person must confirm the property and the facts.",
            "model_name": "bot-keywords", "prompt_version": "bot1", "extracted_at": db.utc_now(),
            "input_tokens": 0, "output_tokens": 0, "est_cost_usd": 0.0})
        summary["saved"] += 1
    return summary
