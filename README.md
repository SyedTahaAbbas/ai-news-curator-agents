# AI News Update

A daily AI news digest. Collects from ~26 public RSS/Atom feeds, filters and
ranks them, writes a dated Markdown digest into the repo, and emails you a
formatted HTML summary — automatically, every morning, via GitHub Actions.

No API keys. No paid tiers. No accounts.

---

## Why this doesn't scrape Twitter/X

You asked for Twitter scraping, and it's worth being straight about why the
script doesn't do that:

- **X requires a login for search.** Since 2023 the public search endpoints are
  gated. An anonymous scraper gets a login wall, not tweets.
- **The old tools are dead.** `snscrape`, `twint` and the Nitter mirrors all
  broke when that gate came down. Anything still claiming to work is either
  paid or fragile.
- **The official API is expensive.** X API v2 tweet *search* starts at the Basic
  tier (~$200/month). The free tier can post, but cannot read search results.
- **Automated scraping violates X's ToS**, which matters if this lives in a
  public GitHub repo attached to your name.

The feeds in `sources.yaml` cover the same ground the AI side of X does —
frontier lab announcements, press, arXiv, Hacker News, and Reddit — with no
cost and nothing that breaks next month.

**If you do want X later**, the collector is structured so a source is just a
function returning `Item` objects. Add a `fetch_x()` alongside `fetch_feed()`
and it drops into the same pipeline.

---

## What you get each morning

An email like this, to the address in `MAIL_TO`:

```
AI News Update — 18 Aug 2026 (23 stories)

LABS & RELEASES
  OpenAI ships new reasoning model
  OpenAI · 18 Aug 06:12 UTC
  ...

INDUSTRY & PRESS
  ...

RESEARCH
  ...

COMMUNITY
  ...
```

Plus, committed to the repo:

- `digests/YYYY-MM-DD.md` — the day's digest in Markdown
- `digests/YYYY-MM-DD.json` — the same data, structured
- `latest.md` — always the most recent digest

---

## Files

| File | Purpose |
|---|---|
| **`PREFERENCES.md`** | **Your control panel — sources to add, and the voice the digest is written in. Start here.** |
| `sources.yaml` | The machine-readable feed list, keywords, boost/mute terms |
| `ai_news.py` | Collector, filter, ranker, renderer, entry point |
| `summarizer.py` | Optional voice layer — reads `PREFERENCES.md`, calls an LLM |
| `emailer.py` | SMTP delivery (also runnable standalone to test email) |
| `requirements.txt` | Three dependencies |
| `.env.example` | Template for local credentials |
| `.github/workflows/daily-ai-news.yml` | The daily schedule |
| `tests/test_pipeline.py` | 42 offline tests — no network needed |

---

## The two modes

**Without an LLM API key** (default) you get a ranked, deduplicated list of
links grouped by category. Free, deterministic, runs forever.

**With an API key** you additionally get the digest *written* — a "Top" section
arguing what actually mattered today, then the stories with their second-order
implications, then a "Watch" list. The writing style is defined in prose in
`PREFERENCES.md` and passed to the model as its system prompt. See
`sample-digest.md` for what this looks like.

Set either `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` — the provider is
auto-detected. Cost is roughly a cent a day at current mid-tier pricing, since
it's one call over ~35 headlines. `python ai_news.py --no-commentary` skips it
for a run; `LLM_PROVIDER=none` disables it permanently.

If the API call fails for any reason, the run falls back to the plain link
digest and still emails you. The voice layer can never break the delivery.

---

## Setup

### 1. Put it in a repo

This folder is designed to be **its own repository**, because
`.github/workflows/` only runs when it sits at the repo root.

```bash
cd "AI News update"
git init
git add .
git commit -m "AI news daily digest"
gh repo create ai-news-update --private --source=. --push
```

> If you'd rather nest it inside an existing repo, move
> `.github/workflows/daily-ai-news.yml` up to that repo's root `.github/workflows/`
> and add a `working-directory: "AI News update"` line to the run steps.

### 2. Get a Gmail app password

The script sends over SMTP. Gmail needs an **app password**, not your normal
password:

1. Turn on 2-Step Verification: <https://myaccount.google.com/security>
2. Generate an app password: <https://myaccount.google.com/apppasswords>
3. Copy the 16-character code.

### 3. Add repository secrets

In GitHub: **Settings → Secrets and variables → Actions → New repository secret**

| Secret | Value |
|---|---|
| `SMTP_USER` | the Gmail address you send *from* |
| `SMTP_PASSWORD` | the 16-character app password |
| `MAIL_FROM` | same as `SMTP_USER` |
| `MAIL_TO` | `Taha.mmtlmu@gmail.com` |
| `ANTHROPIC_API_KEY` *or* `OPENAI_API_KEY` | optional — enables the written digest |

`SMTP_HOST` and `SMTP_PORT` are optional — they default to `smtp.gmail.com:587`.

### 4. Test it

**Actions → Daily AI News Digest → Run workflow.** It runs immediately instead
of waiting for tomorrow. Check the log, then check your inbox.

After that it runs on its own at **07:13 UTC** daily. Change the `cron:` line in
the workflow to move it.

---

## Running locally

```bash
pip install -r requirements.txt
cp .env.example .env      # then fill in your credentials

python ai_news.py                # collect 24h, write digest, email it
python ai_news.py --no-email     # write files only
python ai_news.py --dry-run      # print to terminal, write nothing
python ai_news.py --hours 48     # widen the window
python ai_news.py --check-feeds  # health-check every source
python ai_news.py --no-commentary # skip the LLM layer for this run
python emailer.py                # send a test email and exit
python summarizer.py             # print the exact prompt built from PREFERENCES.md
python tests/test_pipeline.py    # offline tests
```

`--check-feeds` is the one to reach for when the digest looks thin — it tells
you which sources answered and which didn't.

---

## Tuning it

**How it reads** → edit section 2 of `PREFERENCES.md`. It's prose; the model
reads it directly. Run `python summarizer.py` to see exactly what's being sent.

**What it collects** → `sources.yaml`.

**Add a source:**

```yaml
  - name: Some AI Blog
    url: https://example.com/feed.xml
    weight: 1.5              # >1 surfaces earlier, <1 pushes down
    category: Research       # Labs & Releases | Industry & Press | Research | Community
    always: true             # true = skip the keyword filter (feed is already all-AI)
```

**Keywords** are matched on word boundaries, so `ai` hits "AI", "AI's" and
"AI-powered" but never "said" or "chain". A trailing `*` means prefix match:
`fine-tun*` covers tune/tuned/tuning. Spaces also match hyphens, so
`open source` catches "open-source".

**`boost_terms`** raise an item's rank, **`mute_terms`** bury it.

**Digest too long?** `python ai_news.py --max-items 20`, or edit the default in
the workflow.

---

## How ranking works

Each item scores:

```
  source weight × 2.0        how much you trust the outlet
+ recency      × 3.0         1.0 at publication, decaying to 0 at 24h
+ keyword hits × 0.3         capped at 5
+ boost terms  × 0.6         capped at 3
- mute terms   × 3.0         effectively removes the item
```

Then duplicates collapse in two passes: exact (same URL after stripping
`utm_*` tracking params) and near-duplicate (75% word overlap between
headlines, so the same story from five outlets appears once — the
highest-scoring copy wins).

`.seen.json` remembers the last 30 days of sent items so a story that lingers
on a feed for three days is only emailed once.

---

## When something breaks

**No email arrived.** Check the Actions log. A missing secret prints
`Skipping send - missing env var(s)`. An auth failure means Gmail wants an app
password, not your account password.

**The digest is empty.** Run `--check-feeds`. If most sources are DEAD you're
likely rate-limited or offline; if one is, remove it from `sources.yaml`.

**A single dead feed never fails the run** — errors are caught per-source and
listed at the bottom of the digest.

**Reddit feeds are flaky** from datacenter IPs and may intermittently 429. They
are low-weight on purpose; the digest is fine without them.

**GitHub disables scheduled workflows** in repos with no activity for 60 days.
Since this one commits a digest daily, that won't trigger — but if you ever
pause it, re-enable it under Actions.
