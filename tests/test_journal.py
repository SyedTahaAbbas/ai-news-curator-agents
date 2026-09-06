"""
The run journal and replay.

The bus already holds a whole run; writing it down is what makes a run
re-runnable without refetching 2,000 items or paying for another model call.
"""

from ainews.bus import EventBus
from ainews.delivery.journal import RunJournal, register_journal
from ainews.events import (
    CommentaryWritten,
    DigestEmailed,
    RunRequested,
    StageFailed,
    StoriesRanked,
    event_from_payload,
    event_to_payload,
)


def test_every_event_type_round_trips(items, outcomes, run_config):
    for event in (
        RunRequested(config=run_config),
        StoriesRanked(items=items, outcomes=outcomes, config=run_config),
        CommentaryWritten(items=items, outcomes=outcomes, simple="top", config=run_config),
        DigestEmailed(sent=True),
    ):
        restored = event_from_payload(event_to_payload(event))
        assert type(restored) is type(event)
        if hasattr(event, "items"):
            assert [i.uid for i in restored.items] == [i.uid for i in event.items]
            assert restored.config.run_at == event.config.run_at
            assert restored.errors == event.errors


def test_a_failure_round_trips_with_its_payload(items, run_config):
    failure = StageFailed(
        handler="send_email",
        event=CommentaryWritten(items=items, config=run_config),
        error="RuntimeError: boom",
    )
    restored = event_from_payload(event_to_payload(failure))
    assert restored.event_name == "CommentaryWritten"
    assert len(restored.event.items) == len(items)


def test_the_journal_records_a_whole_run(tmp_path, items, run_config):
    journal = RunJournal(tmp_path / "run.jsonl")
    bus = EventBus()
    register_journal(bus, journal)
    bus.subscribe(RunRequested, lambda e, b: b.publish(StoriesRanked(items=items, config=run_config)))
    bus.publish(RunRequested(config=run_config))

    recorded = journal.read()
    assert [type(e).__name__ for e in recorded] == ["RunRequested", "StoriesRanked"]
    assert len(recorded[1].items) == len(items)


def test_last_of_finds_the_event_to_replay(tmp_path, items, run_config):
    journal = RunJournal(tmp_path / "run.jsonl")
    journal.record(CommentaryWritten(items=items[:2], config=run_config))
    journal.record(CommentaryWritten(items=items, simple="the summary", config=run_config))

    replayed = journal.last_of("CommentaryWritten")
    assert replayed.simple == "the summary"
    assert len(replayed.items) == len(items)
    assert journal.last_of("DigestDelivered") is None


def test_an_unreadable_line_is_skipped_not_fatal(tmp_path, run_config):
    path = tmp_path / "run.jsonl"
    journal = RunJournal(path)
    journal.record(RunRequested(config=run_config))
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{ not json\n")
        fh.write('{"type": "SomethingRetired", "data": {}}\n')
    journal.record(DigestEmailed(sent=True))

    # An old journal stays readable after the vocabulary changes.
    assert [type(e).__name__ for e in journal.read()] == ["RunRequested", "DigestEmailed"]


def test_the_journal_file_is_named_for_the_run(tmp_path, now):
    journal = RunJournal.for_run(now, tmp_path)
    assert journal.path.name == f"{now:%Y-%m-%dT%H%M%SZ}.jsonl"
