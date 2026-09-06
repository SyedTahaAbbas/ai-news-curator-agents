from ainews.render import markdown_to_html, render_html, render_markdown
from tests.conftest import make_item


def test_markdown_has_a_title_and_links_every_story(items, now):
    md = render_markdown(items, 24, {"Dead Feed": "HTTP 404"}, run_at=now)
    assert md.startswith("# AI News Curator")
    assert "](https://" in md
    assert "Dead Feed" in md
    assert "## Labs & Releases" in md


def test_markdown_uses_the_run_clock(items, now):
    assert f"{now:%Y-%m-%d}" in render_markdown(items, 24, {}, run_at=now)


def test_health_warnings_are_surfaced(items, now):
    md = render_markdown(
        items, 24, {}, run_at=now,
        health_warnings=["r/artificial has been unreachable for 9 days"],
    )
    assert "unreachable for 9 days" in md


def test_html_is_a_full_document_and_escapes_content(items, now):
    html = render_html(items, 24, {}, run_at=now)
    assert html.startswith("<!DOCTYPE html>") and html.endswith("</html>")
    assert 'href="https://' in html

    hostile = render_html([make_item("<script>alert(1)</script>")], 24, {}, run_at=now)
    assert "<script>" not in hostile


def test_non_http_urls_are_defused(now):
    item = make_item("A story", url="javascript:alert(1)")
    html = render_html([item], 24, {}, run_at=now)
    assert "javascript:" not in html
    assert 'href="#"' in html


def test_empty_digest_renders_without_crashing(now):
    assert "Nothing crossed" in render_markdown([], 24, {}, run_at=now)
    assert "Nothing crossed" in render_html([], 24, {}, run_at=now)


def test_commentary_lands_in_both_renderings(items, now):
    md = render_markdown(items, 24, {}, simple_commentary="## Top\nQuiet day.", run_at=now)
    assert "Quiet day." in md and "## All stories" in md

    html = render_html(items, 24, {}, deep_commentary="## Then\nThe detail.", run_at=now)
    assert "The detail." in html

    assert "All stories" not in render_html(items, 24, {}, run_at=now)


def test_markdown_to_html_converts_the_basics():
    converted = markdown_to_html(
        "## Top\nThis **matters** because *inference* costs fell.\n"
        "- A [link](https://example.com) here\n- `code` here\n\n1. numbered\n"
    )
    assert "<h3" in converted
    assert "<strong>matters</strong>" in converted
    assert "<em>inference</em>" in converted
    assert 'href="https://example.com"' in converted
    assert "<code" in converted
    assert converted.count("<li") == 3


def test_model_output_cannot_inject_html():
    assert "<script>" not in markdown_to_html("<script>alert(1)</script>")
    assert markdown_to_html("") == ""


def test_model_links_are_restricted_to_http():
    converted = markdown_to_html("[click](javascript:alert(1))")
    assert "javascript:" not in converted
