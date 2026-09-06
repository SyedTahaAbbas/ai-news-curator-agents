import pytest

from ainews.config import ConfigError, Health, Scoring, load_config, slug

VALID = """
feeds:
  - name: OpenAI
    url: https://openai.com/rss
    weight: 2.0
    category: Labs & Releases
    always: true
keywords: [ai, llm]
boost_terms: [release*]
mute_terms: [sponsored]
"""


def write(tmp_path, text):
    path = tmp_path / "sources.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_a_valid_file(tmp_path):
    cfg = load_config(write(tmp_path, VALID))
    assert len(cfg.feeds) == 1
    feed = cfg.feeds[0]
    assert (feed.id, feed.name, feed.weight, feed.always) == ("openai", "OpenAI", 2.0, True)
    assert cfg.keywords == ["ai", "llm"]


def test_defaults_apply_when_sections_are_absent(tmp_path):
    cfg = load_config(write(tmp_path, "feeds: []"))
    assert cfg.scoring == Scoring()
    assert cfg.health == Health()
    assert cfg.keywords == []


def test_scoring_can_be_tuned_from_the_file(tmp_path):
    cfg = load_config(write(tmp_path, VALID + "\nscoring:\n  recency: 9.0\n  mute: 1.0\n"))
    assert cfg.scoring.recency == 9.0
    assert cfg.scoring.mute == 1.0
    assert cfg.scoring.feed_weight == Scoring().feed_weight  # untouched keys keep defaults


@pytest.mark.parametrize(
    "text, expected",
    [
        ("feeds:\n  - url: https://x.com/rss\n", "name is required"),
        ("feeds:\n  - name: X\n", "url is required"),
        ("feeds:\n  - name: X\n    url: ftp://x.com\n", "must be http(s)"),
        ("feeds:\n  - name: X\n    url: https://x.com\n    weight: 1.5x\n", "weight must be a number"),
        ("feeds:\n  - name: X\n    url: https://x.com\n    always: yes please\n", "must be true or false"),
        ("feeds: not-a-list\n", "feeds must be a list"),
        ("keywords: 5\n", "keywords must be a list"),
    ],
)
def test_bad_config_names_the_problem(tmp_path, text, expected):
    with pytest.raises(ConfigError) as exc:
        load_config(write(tmp_path, text))
    assert expected in str(exc.value)


def test_duplicate_feed_ids_are_rejected(tmp_path):
    """Two feeds sharing a name used to silently share one weight."""
    text = """
feeds:
  - name: The Verge AI
    url: https://a.com/rss
    weight: 2.0
  - name: The Verge AI
    url: https://b.com/rss
    weight: 0.5
"""
    with pytest.raises(ConfigError) as exc:
        load_config(write(tmp_path, text))
    assert "same id" in str(exc.value)


def test_explicit_id_lets_two_feeds_share_a_display_name(tmp_path):
    text = """
feeds:
  - name: arXiv
    id: arxiv-ai
    url: https://a.com/rss
  - name: arXiv
    id: arxiv-lg
    url: https://b.com/rss
"""
    cfg = load_config(write(tmp_path, text))
    assert [f.id for f in cfg.feeds] == ["arxiv-ai", "arxiv-lg"]


def test_unknown_scoring_key_is_rejected(tmp_path):
    """Silently ignoring a typo is how you get convinced you tuned something."""
    with pytest.raises(ConfigError) as exc:
        load_config(write(tmp_path, VALID + "\nscoring:\n  recencey: 9.0\n"))
    assert "unknown key" in str(exc.value)


def test_dead_ratio_must_be_a_fraction(tmp_path):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, VALID + "\nhealth:\n  fail_above_dead_ratio: 5\n"))


def test_missing_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml")


def test_the_real_sources_file_is_valid():
    """The shipped sources.yaml must always load - this is the one test that
    reads it, so editing the feed list can only break this."""
    cfg = load_config()
    assert len(cfg.feeds) > 10
    assert cfg.keywords and cfg.boost_terms and cfg.mute_terms


def test_slug():
    assert slug("Hacker News (AI, 100+ pts)") == "hacker-news-ai-100-pts"
