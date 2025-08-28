# CLAUDE.md - CHAT-GPT suggestion

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This NYT Scraper extracts New York Times articles **without an API key** using a robust, multi-layered approach. It now uses a **minimalistic AI strategy**:

* **Deterministic-first** parsing.
* **Micro‑AI gap fill** for *only missing/ambiguous* fields using tiny snippets + strict schemas.
* **Heavy AI / Headless** as a last resort, guarded by compliance checks.

## Development Commands

### Installation

```bash
pip install -r requirements.txt
```

### Testing

```bash
# Run all tests
python -m pytest tests/

# Run specific test categories
python -m pytest tests/test_discovery.py
python -m pytest tests/test_parsing.py
python -m pytest tests/test_fetching.py

# Run with coverage
python -m pytest --cov=nyt_scraper tests/
```

## Architecture

### Core Components

1. **Discovery Layer** – Finds article URLs via:

   * RSS feeds (`https://rss.nytimes.com/services/xml/rss/nyt/`)
   * Section sitemaps with `<lastmod>`
   * Search engine fallbacks (`site:nytimes.com`) under strict volume caps
   * Local SQLite + Whoosh index for offline queries

2. **Fetching Layer** – Resilient HTTP client with **polite crawling**:

   * `httpx` connection pooling + retries

   * Delta crawling via `If-Modified-Since` / `ETag`

   * **Challenge detector** (Cloudflare/Turnstile/paywall markers) → **abort + cooldown**

   * Exponential backoff on 429/403; per‑host concurrency limits

   * Wayback Machine fallback for dead URLs (if compliant)

   > Note: We **do not** attempt to bypass challenges. No proxy rotation to evade rate limits.

3. **Parsing Layer** – Cascaded extraction strategy:

   * JSON‑LD scripts
   * OpenGraph/Twitter meta tags
   * Framework blobs (`__NEXT_DATA__`)
   * Semantic HTML (Selectolax + Readability)
   * XPath/CSS site rules
   * **Micro‑AI gap fill (default AI):** tiny snippet (2–3 KB), schema‑only JSON, substring spans; `temperature=0`, ≤300 tokens, single call
   * **Heavy AI / Headless (rare):** only if fields remain missing *and* compliant to render JS

### Data Storage

* **SQLite** for article cache; indexes on date/section/tags
* **Whoosh** for full‑text search (FTS5 optional alternative)

### Key Dependencies

* `httpx`, `lxml`, `selectolax`, `readability-lxml`, `beautifulsoup4` (fallback)
* `python-dateutil`, `chardet`
* `jsonschema` or `pydantic` for AI output validation
* `whoosh` (or `sqlite-fts5`)

## Configuration

Environment variables (via `.env`):

* **Crawl:** `REQUEST_DELAY_MIN` / `REQUEST_DELAY_MAX`, `MAX_CONCURRENT_REQUESTS`, `CACHE_TTL_HOURS`, `SQLITE_DB_PATH`
* **AI minimal:**

  * `AI_MIN_ENABLED=true`
  * `AI_MIN_MAX_TOKENS=300`
  * `AI_MIN_TEMPERATURE=0`
  * `AI_MIN_SNIPPET_BYTES=3000`
* **AI heavy:**

  * `AI_HEAVY_ENABLED=true`
  * `AI_HEAVY_MAX_TOKENS=1200`
  * `AI_HEAVY_RATIO=0.005`  # portion of pages allowed to escalate
* **Budgets & metrics:** `AI_DAILY_TOKEN_LIMIT=10000`, `AI_ALERT_THRESHOLD=0.8`
* **Compliance:** `CHALLENGE_COOLDOWN_HOURS=24`, `KILL_SWITCH=false`, `DRY_RUN=false`, `ENABLE_HEADLESS=false`

## Implementation Notes

* Keep deterministic extractors first; run micro‑AI only for **gaps**.
* Micro‑AI must return **only substrings from snippet**, with **character spans**; reject otherwise.
* Heavy AI/headless only if: (a) still missing required fields, (b) no challenge/paywall, (c) within budgets.

### Micro‑AI Controller (pseudocode)

```python
def parse_with_micro_ai(html):
    fields = deterministic_extract(html)
    needed = [k for k in ("title","author","published_iso","body") if not valid(fields.get(k))]
    if not needed:
        return fields

    snippet = build_targeted_snippet(html, needed, budget=env.AI_MIN_SNIPPET_BYTES)
    payload = make_ai_prompt(needed, snippet, schema=SCHEMA)
    out = call_ai_min(payload, temperature=env.AI_MIN_TEMPERATURE, max_tokens=env.AI_MIN_MAX_TOKENS)

    assert schema_ok(out) and spans_reference_snippet(out, snippet)
    merged = merge(fields, out)

    if still_missing_required(merged) and env.AI_HEAVY_ENABLED:
        merged = maybe_escalate_heavy_ai(merged, html)
    return merged
```

### Minimal Prompt (excerpt)

**System:**

```
You are an extractor. Output ONLY valid JSON matching the provided schema.
Use ONLY substrings present in the snippet. If unsure, return null.
Return character spans (start,end) into the provided snippet string.
```

**User:**

```
SCHEMA: {...}
NEEDED_FIELDS: ["author","published_iso"]
SNIPPET (utf-8): "<h1>…</h1><span class='byline'>By Jane</span><time datetime='2025-08-01T12:00:00Z'>…"
```

## Testing Strategy

* Unit tests for each deterministic extractor + micro‑AI merger
* Validate AI JSON against schema; check spans map to snippet offsets
* Drift tests to ensure escalation to micro‑AI when selectors change
* Compliance tests: challenge/paywall detection → abort
* Metrics tests: counters for `micro_ai_calls_total` vs `heavy_ai_calls_total`

---

# NYT Scraper — Production Implementation Plan (Updated)

## Project Overview

A **resilient, cost‑efficient** NYT scraper operating **without an API key**. It now employs a **three‑tier parsing strategy**:

1. Deterministic extractors → 2) **Micro‑AI gap fill** (tiny, always available) → 3) **Heavy AI / Headless** (rare).

## Architecture

### Three‑Layer Overview

```
DISCOVERY  →  FETCH (polite, challenge-aware)  →  PARSING CASCADE  →  STORAGE
```

### Parsing Cascade (updated)

```
JSON‑LD → Meta → AMP → __NEXT_DATA__ → HTML (Selectolax+Readability) → XPath
              ↓
        Micro‑AI gap fill (tiny snippet, spans+schema, ≤300 tok)
              ↓ (only if still missing & compliant)
        Heavy AI / Headless (strict budget; abort on challenges)
```

## Core Components (diff‑highlights)

* **Compliance:** Add *challenge detector* (Cloudflare/Turnstile/paywall). On detection → **record, abort, and cooldown**; never bypass.
* **Fetching:** Remove proxy‑evasion tactics; keep exponential backoff + per‑host concurrency.
* **Parsing:** Insert **Micro‑AI** between deterministic rules and heavy AI.
* **Monitoring:** Separate metrics for `micro_ai_*` vs `heavy_ai_*`.

## AI Strategy

### Micro‑AI (default)

* Trigger: any required field missing/ambiguous after deterministic.
* Input: 2–3 KB snippet around headline/byline/time/content.
* Output: JSON only; fields limited to substrings from snippet; include spans.
* Settings: `temperature=0`, `max_tokens≤300`, one call per article.
* Validation: JSON Schema + span checks + regex (ISO‑8601) + min body length rule.

### Heavy AI / Headless (rare)

* Trigger: still missing required fields **and** compliant to render JS.
* Budget: ≤0.5% of pages or governed by `AI_HEAVY_RATIO`.
* Safeguards: immediate abort on challenge/paywall; strict timeouts; resource blocking in headless.

## Monitoring & Alerts (additions)

* `micro_ai_calls_total`, `heavy_ai_calls_total`
* `micro_ai_tokens_total`, `heavy_ai_tokens_total`
* `ai_escalations_total{reason}`
* `challenge_events_total{type}` and `challenge_cooldown_active`

## Environment Configuration (additions)

```bash
AI_MIN_ENABLED=true
AI_MIN_MAX_TOKENS=300
AI_MIN_SNIPPET_BYTES=3000
AI_HEAVY_ENABLED=true
AI_HEAVY_MAX_TOKENS=1200
AI_HEAVY_RATIO=0.005
AI_DAILY_TOKEN_LIMIT=10000
AI_ALERT_THRESHOLD=0.8
CHALLENGE_COOLDOWN_HOURS=24
```

## Testing (additions)

* **Micro‑AI contract tests:** invalid JSON rejected; spans must map to snippet; dates must match ISO regex; body length validated.
* **Escalation tests:** simulate DOM drift → deterministic fails → micro‑AI fills; if still missing, escalate once; verify budgets enforced.
* **Compliance tests:** simulate Cloudflare/paywall markers → parser stops; cooldown engaged.

## Ops Runbook (changes)

* If `challenge_events_total` spikes, set `KILL_SWITCH=true` or increase cooldown; do **not** add proxies to bypass.
* Review weekly: top `ai_escalations_total{reason}`, adjust deterministic rules to push success back up.

## Config Files (new/updated)

* `config/ai_prompt.min.json` – schema + templates for micro‑AI
* `config/ai_prompt.heavy.json` – only used when escalation permitted
* `config/parsing_rules.yaml` – selectors + snippet builders for common patterns

## Success Metrics (clarified)

* **Deterministic success ≥90–95%**
* **Micro‑AI fills ≤10% of pages** with ≤300 tokens each
* **Heavy AI/headless <0.5%** of pages

## Compliance Note

Treat any challenge/paywall as a **hard boundary**. The system records the event, cools down, and does not attempt circumvention.
