"""The retry layer. A daily unattended job gets one shot; transient failures
used to cost a whole source, or the whole voice layer, for the day."""

import urllib.error

import pytest

from ainews.http import is_transient, with_retries


def make_http_error(code, headers=None):
    return urllib.error.HTTPError("https://x", code, "boom", headers or {}, None)


@pytest.mark.parametrize("code, expected", [(429, True), (500, True), (503, True),
                                            (408, True), (401, False), (404, False)])
def test_only_transient_statuses_are_retried(code, expected):
    assert is_transient(make_http_error(code)) is expected


def test_url_and_timeout_errors_are_transient():
    assert is_transient(urllib.error.URLError("no route"))
    assert is_transient(TimeoutError())


def test_it_retries_then_succeeds():
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise make_http_error(503)
        return "ok"

    assert with_retries(flaky, attempts=3, base_delay=0, sleep=lambda _: None) == "ok"
    assert len(attempts) == 3


def test_it_gives_up_and_raises_the_last_error():
    def always_fails():
        raise make_http_error(500)

    with pytest.raises(urllib.error.HTTPError):
        with_retries(always_fails, attempts=2, base_delay=0, sleep=lambda _: None)


def test_a_permanent_error_is_not_retried():
    attempts = []

    def unauthorised():
        attempts.append(1)
        raise make_http_error(401)

    with pytest.raises(urllib.error.HTTPError):
        with_retries(unauthorised, attempts=5, base_delay=0, sleep=lambda _: None)
    assert len(attempts) == 1  # retrying a 401 only wastes the run's time


def test_retry_after_header_is_honoured():
    delays = []

    def rate_limited():
        raise make_http_error(429, {"Retry-After": "7"})

    with pytest.raises(urllib.error.HTTPError):
        with_retries(rate_limited, attempts=2, base_delay=1, sleep=delays.append)
    assert delays == [7.0]


def test_backoff_grows_and_is_jittered():
    delays = []

    def always_503():
        raise make_http_error(503)

    with pytest.raises(urllib.error.HTTPError):
        with_retries(always_503, attempts=4, base_delay=1.0, sleep=delays.append)

    assert len(delays) == 3
    # jitter is 50-150% of the exponential curve: 1, 2, 4
    assert 0.5 <= delays[0] <= 1.5
    assert 1.0 <= delays[1] <= 3.0
    assert 2.0 <= delays[2] <= 6.0
