from ainews.bus import EventBus
from ainews.events import (
    DigestRendered,
    Event,
    RunRequested,
    StageFailed,
    StoriesGathered,
    StoriesRanked,
)


def boom(event, bus):
    raise RuntimeError("stage exploded")


def test_every_subscriber_runs_in_registration_order(bus):
    order = []
    bus.subscribe(RunRequested, lambda e, b: order.append("first"))
    bus.subscribe(RunRequested, lambda e, b: order.append("second"))
    bus.publish(RunRequested())
    assert order == ["first", "second"]


def test_history_and_lookup_helpers(bus):
    bus.publish(RunRequested())
    assert len(bus.all_of(RunRequested)) == 1
    assert isinstance(bus.last(RunRequested), RunRequested)
    assert bus.last(StoriesRanked) is None


def test_a_follow_on_event_is_queued_not_dispatched_mid_handler(bus):
    order = []
    bus.subscribe(RunRequested, lambda e, b: (order.append("run"), b.publish(StoriesGathered(items=[]))))
    bus.subscribe(RunRequested, lambda e, b: order.append("run-sibling"))
    bus.subscribe(StoriesGathered, lambda e, b: order.append("gathered"))
    bus.publish(RunRequested())
    assert order == ["run", "run-sibling", "gathered"]


def test_subscribing_to_the_base_event_matches_everything(bus):
    """How the run journal records a whole run with one subscriber."""
    seen = []
    bus.subscribe(Event, lambda e, b: seen.append(type(e).__name__))
    bus.publish(RunRequested())
    bus.publish(StoriesGathered(items=[]))
    assert seen == ["RunRequested", "StoriesGathered"]


def test_a_raising_handler_does_not_stop_its_siblings(bus):
    survivors = []
    bus.subscribe(DigestRendered, boom)
    bus.subscribe(DigestRendered, lambda e, b: survivors.append("still ran"))
    bus.publish(DigestRendered(items=[], markdown=""))
    assert survivors == ["still ran"]
    assert len(bus.failures) == 1


def test_stage_failed_carries_the_original_event(bus):
    """Strings would make retry or dead-lettering impossible; the payload has
    to travel with the failure."""
    original = DigestRendered(items=[], markdown="the digest")
    bus.subscribe(DigestRendered, boom)
    bus.publish(original)

    failure = bus.failures[0]
    assert isinstance(failure, StageFailed)
    assert failure.event is original
    assert failure.event_name == "DigestRendered"
    assert failure.event.markdown == "the digest"
    assert "stage exploded" in failure.error


def test_a_failing_failure_handler_does_not_loop(bus):
    bus.subscribe(StageFailed, boom)
    bus.subscribe(DigestRendered, boom)
    bus.publish(DigestRendered(items=[], markdown=""))
    assert len(bus.failures) == 1


def test_trace_writes_to_stderr(capsys):
    EventBus(trace=True).publish(RunRequested())
    assert "RunRequested" in capsys.readouterr().err
