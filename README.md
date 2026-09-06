# AI News Curator Agents

**What this is for:** you don't have time to check 20+ AI news sources every
day yourself. This does it for you — a small team of AI agents that reads
everything so you don't have to, and hands you back a short, ranked,
duplicate-free write-up instead of a pile of links.

It's built as a small **event-driven agentic workflow** — three agents, each
with one job, each able to run and be improved on its own. None of them calls
another: each announces what it did, and whoever cares is listening.

- 🔎 **Gatherer** — fetches the raw posts from every source
- 📊 **Analyst** — ranks, dedupes, and drops what's already been covered
- ✍️ **Writer** *(optional)* — an LLM writes it up in plain English, at a
  quick-read level and a deeper level

No API keys needed for the core pipeline (gathering + ranking work for free,
forever). The writer agent is the only piece that costs anything, and it's
entirely opt-in. The result lands as a dated Markdown file in
**`see news/`**, and can be emailed to you automatically every morning.

---

## How each agent works

1. **Gatherer** (`ainews/agents/gatherer.py`) — downloads the latest posts from
   every feed in `sources.yaml`.
2. **Analyst** (`ainews/agents/analyst.py`) — scores each story (how recent it
   is, how much you trust the source, whether it matches AI keywords), throws
   out duplicates, and drops anything already sent in the last 30 days.
   Purely rule-based today, no LLM involved — this is the stage most likely
   to keep changing as the ranking gets smarter.
3. **Writer** (`ainews/agents/writer.py`, optional) — if you've set an API key,
   an LLM writes commentary at two levels: a short "what matters and why"
   summary, and a separate deeper story-by-story breakdown. Skipped
   entirely with no key configured.

The result is written to `see news/YYYY-MM-DD.md` — one file per day, named
after the date you ran it. Sending an email is optional and off by default;
this README doesn't cover it — see `.env.example` if you want it later.

---

## How they're wired together (the event bus)

The agents don't call each other. Each one subscribes to an event and
publishes a new one; the bus in `ainews/bus.py` does the routing:

```
RunRequested ──► Gatherer ──► StoriesGathered ──┬─► Analyst ──► StoriesRanked
                                                │                    │
                                     health check                    ▼
                                                                  Writer
                                                                     │
                                                                     ▼
                                                            CommentaryWritten
                                                                     │
                                                                     ▼
                                                              DigestRendered
                                                             ╱             ╲
                                                     write files        send email
                                                             ╲             ╱
                                                          (both reported back)
                                                                   │
                                                                   ▼
                                                            DigestDelivered
                                                                   │
                                                                   ▼
                                                            mark stories seen
```

Running a digest is a single `bus.publish(RunRequested(...))` — everything
after that happens because something was listening. Four things follow from
that, all of them the point of the design:

- **Adding a consumer is additive.** Want the digest posted to Slack too?
  Write a handler, subscribe it to `DigestRendered`, tell the commit
  coordinator to expect it. Nothing existing changes.
- **A broken stage doesn't take down the run.** A handler that raises becomes
  a `StageFailed` event carrying the event it choked on: the other subscribers
  of that event still run, and the run exits non-zero at the end.
- **Nothing is marked as "sent" until it was actually sent.** `DigestDelivered`
  only fires once every channel that was supposed to deliver reports success.
  If the email fails, nothing is marked seen and tomorrow's run picks the same
  stories up again — see `ainews/delivery/commit.py`.
- **Flags belong to whoever they concern.** `--no-email` isn't checked by the
  renderer; the email handler declines to run. Every event carries the run's
  `RunConfig` — including one shared clock, so a run at 23:59 can't file its
  digest under one date and its bookkeeping under the next.

To watch it happen:

```bash
ai-news --dry-run --trace     # logs every event as it's dispatched
```

---

## Run it with Docker (easiest — works on any machine)

No Python setup, no dependency issues, no certificate problems. Requires
[Docker Desktop](https://www.docker.com/products/docker-desktop/) installed
and running.

```bash
cd "AI News update"
docker compose run --rm ai-news --no-email
```

Look in the `see news/` folder afterwards — same as the local path below.
Other commands work the same way, just prefixed:

```bash
docker compose run --rm ai-news --dry-run           # print only, save nothing
docker compose run --rm ai-news --hours 48          # widen the window
docker compose run --rm ai-news check-feeds         # test every source
```

Editing `sources.yaml` or `PREFERENCES.md` takes effect immediately, no
rebuild needed — they're mounted from this folder into the container. You
only need to rebuild (`docker compose build`) after changing the Python code
itself.

---

## Running it locally instead (no Docker)

**1. Install `uv`** (a fast Python package manager), if you don't have it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**2. Open this folder in your terminal, and install it:**

```bash
cd "AI News update"
uv venv
uv pip install -e ".[dev]"
```

**3. Run it:**

```bash
source .venv/bin/activate
ai-news --no-email
```

**4. Look in the `see news/` folder.** You'll find a new file named after
today's date, like `see news/2026-09-06.md`.

`ai-news` reads `sources.yaml` and writes its output relative to the directory
you run it from, so run it from this folder (or set `AINEWS_HOME` to point
here).

---

## macOS certificate error? (local path only — Docker doesn't hit this)

If you see `SSL: CERTIFICATE_VERIFY_FAILED`, your Python install doesn't trust
any certificates yet (common with the python.org installer on macOS).
`certifi` is already a dependency — just point Python at it:

```bash
export SSL_CERT_FILE="$(python -c 'import certifi; print(certifi.where())')"
```

Then re-run. (You'll need to re-export that variable in any new terminal tab,
or add it to your shell profile.)

---

## The commands you'll actually use

```bash
ai-news --no-email         # collect news, write a file, done
ai-news --dry-run          # just print to the screen, save nothing
ai-news --hours 48         # look back 2 days instead of 1
ai-news check-feeds        # test every source and show which ones work
ai-news preview            # show exactly what would be sent to the model
```

`check-feeds` is the one to run first if a digest looks thin — it tells you
exactly which sources responded.

---

## The two files you'll want to edit

| File | What it's for |
|---|---|
| **`PREFERENCES.md`** | Your control panel. List sources you want added, and describe the writing style you want (see below). |
| `sources.yaml` | Where sources actually get added, plus the ranking weights and health thresholds. |

**To add a source:** write it down in `PREFERENCES.md` under "To add", then
copy it into `sources.yaml` in this shape:

```yaml
  - name: Some AI Blog
    url: https://example.com/feed.xml
    weight: 1.5              # higher = shows up earlier
    category: Research       # Labs & Releases | Industry & Press | Research | Community
    always: true             # true = every post from this feed counts (it's already all-AI)
```

The file is validated when it loads, so a typo tells you exactly where it is
(`sources.yaml: feeds[7] ('Wired AI'): weight must be a number, got '1.5x'`)
rather than failing halfway through tomorrow morning's run.

**To change the ranking**, edit the `scoring:` block at the bottom of
`sources.yaml` — no code change needed. Every digest's `.json` sibling records
the score breakdown per story, so you can see which part put something where
before you touch anything.

**To change when a broken source list fails the run**, edit `health:` in the
same file. By default a run fails if nothing was gathered at all or more than
half the feeds are down, and a feed that's been dead 3+ days gets flagged in
the digest. One flaky source (Reddit rate-limits most mornings) is just a
footnote.

---

## Want the digest *written*, not just a list of links?

By default you get a plain, ranked list of headlines — free, no account
needed. If you set an API key (`ANTHROPIC_API_KEY` or `OPENAI_API_KEY`) in a
`.env` file (copy `.env.example` to start), the writer stage has an LLM
produce two pieces of commentary in the voice described in `PREFERENCES.md`
— written to sound like how Aravind Srinivas explains AI news on his
podcast appearances:

- a short **top-level summary** — the one thing worth knowing today, in 2-4
  sentences
- a separate **deep dive** — the same stories again, but story-by-story with
  more technical detail

Costs about two cents a day. The two calls run concurrently and retry on rate
limits; if either still fails, that piece is skipped — nothing breaks, and the
other one can still land.

```bash
ai-news preview     # print exactly what gets sent to the model
```

---

## Running the stages separately (what CI does)

`ai-news` runs all three stages on one in-process bus. The GitHub Actions
workflow instead runs each stage as its own step, passing a JSON file between
them — useful for inspecting exactly what the analyst kept or dropped without
re-running the gatherer. Each of those processes runs its own single-stage bus
and replays the previous stage's output as the event it would have received
in-process, so the handlers are identical; only the transport changes:

```bash
ai-news gather  --out raw.json
ai-news analyze --in raw.json    --out ranked.json --hours 24 --max-items 35
ai-news write   --in ranked.json --out-simple simple.md --out-deep deep.md
ai-news deliver --in ranked.json --simple simple.md --deep deep.md --hours 24
```

Each command is self-contained and safe to re-run on its own.

---

## Re-running a day without re-fetching it

Every run writes its full event log to `runs/<timestamp>.jsonl` — what the
gatherer saw, what the analyst kept, what the writer produced, in order. You
can re-deliver from that recording instead of hitting the network or paying
for another model call:

```bash
ai-news replay runs/2026-09-06T071300Z.jsonl            # re-render, no email
ai-news replay runs/2026-09-06T071300Z.jsonl --email    # actually send it
```

Email is off unless you ask for it — a debugging tool that silently re-sends
yesterday's digest to real people isn't a debugging tool.

---

## Running it every day automatically

There's a GitHub Actions workflow at `.github/workflows/daily-ai-news.yml`
that runs the tests, then the four commands above on a schedule (one per step,
each with its output uploaded as an artifact) and commits the digest back to
the repo. See the comments inside that file for the secrets it needs. This is
optional — running it by hand with the commands above works fine too.

---

## Tests

```bash
pytest              # the whole suite, no network needed
pytest tests/test_delivery.py -v
```

Nothing in the suite touches the network, your real `.seen.json`, or your
`see news/` folder — the seen store and feed-health store are interfaces with
in-memory implementations, and the run clock is injected.

---

## A note on untrusted input

Everything in a feed — titles, summaries, even the text of a parser error — is
written by someone else, and ends up in a Markdown file committed to this repo,
an HTML email, and a prompt sent to a model. All three are escaped at the
boundary: Markdown link syntax is neutralised so a title can't break out of its
own link, HTML is escaped so a summary can't become a tag, only `http(s)` URLs
are ever emitted as clickable, and the model is told explicitly to treat feed
text as data rather than instructions. `tests/test_security.py` pins all of it
down.

---

## If something looks wrong

- **A source is missing or the digest is thin:** run `ai-news check-feeds`.
  A source marked `DEAD` is either offline or has changed its feed URL —
  remove it from `sources.yaml` or find the new URL. A feed that's been dead
  for days also gets flagged at the bottom of the digest itself.
- **Reddit sources show up as dead sometimes:** that's normal — Reddit rate
  limits automated requests. Fetches are retried, and it'll work again on the
  next run.
- **One bad source never breaks the whole run.** Every feed is fetched
  independently; a failure is just noted at the bottom of the digest.
- **You stopped getting emails:** the run now fails loudly instead of quietly
  marking stories as sent — check the workflow's most recent run, and note
  that nothing was marked seen, so nothing was lost.
