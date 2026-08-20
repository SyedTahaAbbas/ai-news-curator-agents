# AI News Update

A script that checks ~22 AI news sources (OpenAI, DeepMind, TechCrunch, arXiv,
Hacker News, and more), picks out the stories that matter, and saves them as a
Markdown file in the **`see news/`** folder. No API keys required to run it.

---

## What it does, in plain terms

1. It reads a list of RSS feeds from `sources.yaml`.
2. It downloads the latest posts from each one.
3. It scores every story (how recent it is, how much you trust the source,
   whether it matches AI keywords) and throws out duplicates.
4. It writes the result to `see news/YYYY-MM-DD.md` — one file per day, named
   after the date you ran it.

That's the whole pipeline. Sending an email is optional and off by default;
this README doesn't cover it — see `.env.example` if you want it later.

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
docker compose run --rm ai-news --dry-run          # print only, save nothing
docker compose run --rm ai-news --hours 48          # widen the window
docker compose run --rm ai-news --check-feeds       # test every source
```

Editing `sources.yaml` or `PREFERENCES.md` takes effect immediately, no
rebuild needed — they're mounted from this folder into the container. You
only need to rebuild (`docker compose build`) after changing the Python code
itself.

If you'd rather not use Compose:

```bash
docker build -t ai-news-update .
docker run --rm \
  -v "$(pwd)/sources.yaml:/app/sources.yaml:ro" \
  -v "$(pwd)/PREFERENCES.md:/app/PREFERENCES.md:ro" \
  -v "$(pwd)/see news:/app/see news" \
  -v "$(pwd)/.seen.json:/app/.seen.json" \
  ai-news-update --no-email
```

If you want the emailed version, add `--env-file .env` (copy `.env.example`
to `.env` first) and drop `--no-email`.

---

## Running it locally instead (no Docker)

**1. Install `uv`** (a fast Python package manager), if you don't have it:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**2. Open this folder in your terminal:**

```bash
cd "AI News update"
```

**3. Create a virtual environment and install the dependencies:**

```bash
uv venv
uv pip install -r requirements.txt
```

**4. Run it:**

```bash
source .venv/bin/activate
python ai_news.py --no-email
```

**5. Look in the `see news/` folder.** You'll find a new file named after
today's date, like `see news/2026-08-20.md`.

---

## macOS certificate error? (local path only — Docker doesn't hit this)

If you see `SSL: CERTIFICATE_VERIFY_FAILED`, your Python install doesn't trust
any certificates yet (common with the python.org installer on macOS).
`certifi` is already in `requirements.txt` - just point Python at it:

```bash
export SSL_CERT_FILE="$(python -c 'import certifi; print(certifi.where())')"
```

Then re-run the script. (You'll need to re-export that variable in any new
terminal tab, or add it to your shell profile.)

---

## The commands you'll actually use

```bash
python ai_news.py --no-email         # collect news, write a file, done
python ai_news.py --dry-run          # just print to the screen, save nothing
python ai_news.py --hours 48         # look back 2 days instead of 1
python ai_news.py --check-feeds      # test every source and show which ones work
```

`--check-feeds` is the one to run first if a digest looks thin — it tells you
exactly which sources responded.

---

## The two files you'll want to edit

| File | What it's for |
|---|---|
| **`PREFERENCES.md`** | Your control panel. List sources you want added, and describe the writing style you want (see below). |
| `sources.yaml` | Where sources actually get added, in a format the script can read. |

**To add a source:** write it down in `PREFERENCES.md` under "To add", then
copy it into `sources.yaml` in this shape:

```yaml
  - name: Some AI Blog
    url: https://example.com/feed.xml
    weight: 1.5              # higher = shows up earlier
    category: Research       # Labs & Releases | Industry & Press | Research | Community
    always: true             # true = every post from this feed counts (it's already all-AI)
```

---

## Want the digest *written*, not just a list of links?

By default you get a plain, ranked list of headlines — free, no account
needed. If you set an API key (`ANTHROPIC_API_KEY` or `OPENAI_API_KEY`) in a
`.env` file (copy `.env.example` to start), the script will also have an LLM
write a short summary in the voice described in `PREFERENCES.md` — that file
is written to sound like how Aravind Srinivas explains AI news on his
podcast appearances. Costs about a cent a day. If it ever fails, the script
just falls back to the plain list — nothing breaks.

```bash
python summarizer.py     # preview exactly what gets sent to the model
```

---

## Running it every day automatically

There's a GitHub Actions workflow at `.github/workflows/daily-ai-news.yml`
that runs this on a schedule and commits the digest back to the repo. See the
comments inside that file for the secrets it needs. This is optional — running
it by hand with the commands above works fine too.

---

## If something looks wrong

- **A source is missing or the digest is thin:** run `python ai_news.py
  --check-feeds`. A source marked `DEAD` is either offline or has changed its
  feed URL — remove it from `sources.yaml` or find the new URL.
- **Reddit sources show up as dead sometimes:** that's normal — Reddit rate
  limits automated requests. It'll work again on the next run.
- **One bad source never breaks the whole run.** Every feed is fetched
  independently; a failure is just noted at the bottom of the digest.
