#!/usr/bin/env python3
"""
AI News Curator Agents - the command line, and the composition root.

    ai-news                       # collect last 24h, write digest, email it
    ai-news --hours 48            # widen the window
    ai-news --no-email            # write files only
    ai-news --dry-run             # print to stdout, write nothing
    ai-news --trace               # log every event as it is dispatched
    ai-news check-feeds           # health-check every source and exit
    ai-news preview               # show the exact prompt the writer would send

Each stage on its own (what the GitHub Actions workflow runs, one per step).
Every one of these builds a single-stage bus and replays the previous stage's
JSON as the event it would have received in-process, so the handlers are
identical and only the transport differs:

    ai-news gather  --out raw.json
    ai-news analyze --in raw.json    --out ranked.json --hours 24 --max-items 35
    ai-news write   --in ranked.json --out-simple simple.md --out-deep deep.md
    ai-news deliver --in ranked.json --simple simple.md --deep deep.md

And, from a recorded run:

    ai-news replay runs/2026-09-05T071300Z.jsonl        # re-deliver, no email

This module builds objects and hands them to `register()` functions. It does
not implement any stage, and no stage names another: the whole run is one
publish, and everything after it happens because something subscribed. See
ainews/events.py for the cascade.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

try:  # optional: load a local .env when present
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional
    pass

from ainews.agents import analyst, gatherer, writer
from ainews.bus import EventBus
from ainews.config import Config, ConfigError, load_config
from ainews.delivery import (
    RunJournal,
    register_commit,
    register_email,
    register_files,
    register_journal,
    register_render,
    register_seen,
)
from ainews.events import (
    CommentaryWritten,
    DeliveryIncomplete,
    FeedCheckCompleted,
    FeedCheckRequested,
    FeedHealthAssessed,
    FeedOutcome,
    RunConfig,
    RunRequested,
    StoriesGathered,
    StoriesRanked,
    errors_of,
    utcnow,
)
from ainews.models import Item
from ainews.state import (
    FeedHealthStore,
    InMemoryFeedHealthStore,
    JsonSeenStore,
    SeenStore,
)

STAGE_FILE_EVENTS = {
    "StoriesGathered": StoriesGathered,
    "StoriesRanked": StoriesRanked,
}


# ---------------------------------------------------------------------------
# Stage files (the transport between CI steps)
# ---------------------------------------------------------------------------


def _dump_stage(path: str, event) -> None:
    payload = {
        "items": [i.to_json() for i in event.items],
        "outcomes": [dataclasses.asdict(o) for o in event.outcomes],
        # Derived, and only for a human reading the artifact.
        "errors": errors_of(event.outcomes),
    }
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Wrote {path}")


def _load_stage(path: str) -> tuple[list[Item], list[FeedOutcome]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    items = [Item.from_dict(d) for d in raw.get("items", [])]
    outcomes = [FeedOutcome(**d) for d in raw.get("outcomes", [])]
    if not outcomes and raw.get("errors"):
        # A file written before outcomes existed: keep the errors renderable.
        outcomes = [
            FeedOutcome(feed_id=name, name=name, error=err)
            for name, err in raw["errors"].items()
        ]
    return items, outcomes


def _read_optional(path: str) -> str | None:
    p = Path(path)
    if not p.exists():
        return None
    text = p.read_text(encoding="utf-8").strip()
    return text or None


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def build_bus(
    cfg: Config,
    *,
    trace: bool = False,
    seen_store: SeenStore | None = None,
    health: FeedHealthStore | None = None,
    journal: RunJournal | None = None,
    stages: str = "all",
):
    """Register the pipeline on a fresh bus. Returns (bus, finalize).

    `stages` picks how much of it to wire up: "all" for a full run, "delivery"
    for the CI deliver step and for replay. Nothing else in the codebase knows
    the assembly order - this is the only place that does.
    """
    bus = EventBus(trace=trace)
    seen_store = seen_store or JsonSeenStore()
    health = health if health is not None else FeedHealthStore()

    if journal is not None:
        register_journal(bus, journal)

    if stages == "all":
        gatherer.register(bus, cfg, health)
        analyst.register(bus, cfg, seen_store)
        writer.register(bus)

    # Commit first: it must see DigestRendered before the channels report back.
    finalize = register_commit(bus)
    register_render(bus, cfg, health)
    register_files(bus)
    register_email(bus, cfg, health)
    register_seen(bus, seen_store)
    return bus, finalize


def _exit_code(bus: EventBus) -> int:
    """Non-zero if anything went wrong - a crashed handler, a source list that
    has rotted, or a digest that never actually got delivered. A cron job that
    exits 0 while degrading is the failure mode worth being loud about."""
    code = 0

    for failure in bus.failures:
        print(
            f"[run] {failure.handler} failed on {failure.event_name}: {failure.error}",
            file=sys.stderr,
        )
        code = 1

    health = bus.last(FeedHealthAssessed)
    if health and health.fatal:
        print(f"[run] source health check failed: {health.reason}", file=sys.stderr)
        code = 1

    incomplete = bus.last(DeliveryIncomplete)
    if incomplete:
        print(f"[run] delivery incomplete: {incomplete.reason}", file=sys.stderr)
        code = 1

    return code


def _run_config(args: argparse.Namespace, **overrides) -> RunConfig:
    base = dict(
        hours=getattr(args, "hours", 24),
        max_items=getattr(args, "max_items", 35),
        include_seen=getattr(args, "include_seen", False),
        commentary=not getattr(args, "no_commentary", False),
        email=not getattr(args, "no_email", False),
        dry_run=getattr(args, "dry_run", False),
        run_at=utcnow(),
    )
    base.update(overrides)
    return RunConfig(**base)


def _journal_for(args: argparse.Namespace, config: RunConfig) -> RunJournal | None:
    if getattr(args, "no_journal", False) or config.dry_run:
        return None
    directory = getattr(args, "journal_dir", None)
    # Unset means "next to the rest of the data" - which is AINEWS_HOME when
    # that is set, not wherever the process happens to have been started.
    return RunJournal.for_run(config.run_at, Path(directory) if directory else None)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace, cfg: Config) -> int:
    """The whole pipeline, in one process. One publish is the entire run."""
    config = _run_config(args)
    health = InMemoryFeedHealthStore() if config.dry_run else FeedHealthStore()
    bus, finalize = build_bus(
        cfg, trace=args.trace, health=health, journal=_journal_for(args, config)
    )
    bus.publish(RunRequested(config=config))
    finalize()
    return _exit_code(bus)


def cmd_check_feeds(args: argparse.Namespace, cfg: Config) -> int:
    bus = EventBus(trace=args.trace)
    gatherer.register(bus, cfg)
    bus.publish(FeedCheckRequested())
    result = bus.last(FeedCheckCompleted)
    return 0 if result and result.healthy else 1


def cmd_gather(args: argparse.Namespace, cfg: Config) -> int:
    config = _run_config(args)
    bus = EventBus(trace=args.trace)
    gatherer.register(bus, cfg, FeedHealthStore())
    bus.subscribe(StoriesGathered, lambda e, b: _dump_stage(args.out, e))
    bus.publish(RunRequested(config=config))
    return _exit_code(bus)


def cmd_analyze(args: argparse.Namespace, cfg: Config) -> int:
    items, outcomes = _load_stage(args.input)
    bus = EventBus(trace=args.trace)
    analyst.register(bus, cfg, JsonSeenStore())
    bus.subscribe(StoriesRanked, lambda e, b: _dump_stage(args.out, e))
    bus.publish(
        StoriesGathered(items=items, outcomes=outcomes, config=_run_config(args))
    )
    return _exit_code(bus)


def cmd_write(args: argparse.Namespace, cfg: Config) -> int:
    items, outcomes = _load_stage(args.input)
    bus = EventBus(trace=args.trace)
    writer.register(bus)

    def dump(event: CommentaryWritten, bus: EventBus) -> None:
        Path(args.out_simple).write_text(event.simple or "", encoding="utf-8")
        Path(args.out_deep).write_text(event.deep or "", encoding="utf-8")
        print(f"Wrote {args.out_simple} and {args.out_deep}")

    bus.subscribe(CommentaryWritten, dump)
    bus.publish(
        StoriesRanked(items=items, outcomes=outcomes, config=_run_config(args))
    )
    return _exit_code(bus)


def cmd_deliver(args: argparse.Namespace, cfg: Config) -> int:
    items, outcomes = _load_stage(args.input)
    config = _run_config(args)
    bus, finalize = build_bus(
        cfg, trace=args.trace, journal=_journal_for(args, config), stages="delivery"
    )
    bus.publish(
        CommentaryWritten(
            items=items,
            outcomes=outcomes,
            simple=_read_optional(args.simple),
            deep=_read_optional(args.deep),
            config=config,
        )
    )
    finalize()
    return _exit_code(bus)


def cmd_replay(args: argparse.Namespace, cfg: Config) -> int:
    """Re-publish a recorded event into a fresh bus.

    Email is off unless you ask for it: a debugging tool that silently
    re-sends yesterday's digest to real people is not a debugging tool.
    """
    journal = RunJournal(Path(args.journal))
    recorded = journal.last_of(args.from_event)
    if recorded is None:
        print(
            f"[replay] no {args.from_event} in {args.journal}", file=sys.stderr
        )
        return 1

    config = dataclasses.replace(
        recorded.config, email=args.email, dry_run=args.dry_run
    )
    event = dataclasses.replace(recorded, config=config)
    print(f"[replay] {args.from_event} from {journal.path.name} "
          f"({len(getattr(event, 'items', []))} items, email={args.email})")

    bus, finalize = build_bus(cfg, trace=args.trace, stages="delivery")
    bus.publish(event)
    finalize()
    return _exit_code(bus)


def cmd_preview(args: argparse.Namespace, cfg: Config) -> int:
    writer.preview()
    return 0


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

SUBCOMMANDS = {
    "run": cmd_run,
    "gather": cmd_gather,
    "analyze": cmd_analyze,
    "write": cmd_write,
    "deliver": cmd_deliver,
    "check-feeds": cmd_check_feeds,
    "preview": cmd_preview,
    "replay": cmd_replay,
}


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="ai-news", description="Daily AI news digest from public feeds."
    )
    ap.add_argument("--trace", action="store_true", help="log every event to stderr")
    sub = ap.add_subparsers(dest="command")

    def add_trace(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--trace", action="store_true", help="log every event to stderr")

    def add_journal(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--journal-dir", default=None, help="where to write the run journal (default: runs/ beside your sources.yaml)")
        parser.add_argument("--no-journal", action="store_true", help="do not record this run")

    p_run = sub.add_parser("run", help="the whole pipeline in one process (the default)")
    p_run.add_argument("--hours", type=int, default=24, help="lookback window (default 24)")
    p_run.add_argument("--max-items", type=int, default=35, help="cap on digest length")
    p_run.add_argument("--no-email", action="store_true", help="write files but do not send")
    p_run.add_argument("--dry-run", action="store_true", help="print only, write nothing")
    p_run.add_argument("--include-seen", action="store_true", help="do not skip previously sent items")
    p_run.add_argument(
        "--no-commentary", action="store_true", help="skip the LLM voice layer even if a key is set"
    )
    add_journal(p_run)
    add_trace(p_run)

    p_gather = sub.add_parser("gather", help="stage 1: fetch every feed")
    p_gather.add_argument("--out", default="raw.json", help="where to write the raw items")
    add_trace(p_gather)

    p_analyze = sub.add_parser("analyze", help="stage 2: filter, score, dedupe")
    p_analyze.add_argument("--in", dest="input", default="raw.json", help="the gatherer's output")
    p_analyze.add_argument("--out", default="ranked.json", help="where to write the shortlist")
    p_analyze.add_argument("--hours", type=int, default=24, help="lookback window (default 24)")
    p_analyze.add_argument("--max-items", type=int, default=35, help="cap on digest length")
    p_analyze.add_argument("--include-seen", action="store_true", help="do not skip previously sent items")
    add_trace(p_analyze)

    p_write = sub.add_parser("write", help="stage 3: the two-level commentary")
    p_write.add_argument("--in", dest="input", default="ranked.json", help="the analyst's output")
    p_write.add_argument("--out-simple", default="simple.md", help="where to write the summary")
    p_write.add_argument("--out-deep", default="deep.md", help="where to write the deep dive")
    p_write.add_argument("--no-commentary", action="store_true", help="write empty files, call no LLM")
    add_trace(p_write)

    p_deliver = sub.add_parser("deliver", help="stage 4: render, write, email")
    p_deliver.add_argument("--in", dest="input", default="ranked.json", help="the analyst's output")
    p_deliver.add_argument("--simple", default="simple.md", help="the writer's summary")
    p_deliver.add_argument("--deep", default="deep.md", help="the writer's deep dive")
    p_deliver.add_argument("--hours", type=int, default=24, help="lookback window, for display only")
    p_deliver.add_argument("--no-email", action="store_true", help="write files but do not send")
    p_deliver.add_argument("--dry-run", action="store_true", help="print only, write nothing")
    add_journal(p_deliver)
    add_trace(p_deliver)

    p_check = sub.add_parser("check-feeds", help="health-check every source and exit")
    add_trace(p_check)

    p_preview = sub.add_parser("preview", help="print the writer's prompt and provider status")
    add_trace(p_preview)

    p_replay = sub.add_parser("replay", help="re-deliver from a recorded run journal")
    p_replay.add_argument("journal", help="path to a runs/*.jsonl file")
    p_replay.add_argument(
        "--from", dest="from_event", default="CommentaryWritten",
        help="which recorded event to replay (default CommentaryWritten)",
    )
    p_replay.add_argument("--email", action="store_true", help="actually send (off by default)")
    p_replay.add_argument("--dry-run", action="store_true", help="print only, write nothing")
    add_trace(p_replay)

    return ap


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # `ai-news --no-email` keeps working: anything that isn't a subcommand is
    # a `run` flag, which is what it always was.
    if not argv or (argv[0] not in SUBCOMMANDS and not argv[0] in ("-h", "--help")):
        argv.insert(0, "run")

    args = build_parser().parse_args(argv)

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"[config] {exc}", file=sys.stderr)
        return 2

    return SUBCOMMANDS[args.command](args, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
