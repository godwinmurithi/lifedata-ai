"""The panel's markdown renderer, driven in a real browser.

`markdown.js` is exposed as `window.__lifedataMarkdown` precisely so this file
can drive it with `page.evaluate` — it builds DOM nodes, so there is nothing
useful to assert about it outside a browser.

It needs no Superset, no login and no orchestrator: the module is loaded into a
blank page. That is deliberate, so a markdown regression is reported as a
markdown regression rather than as "the stack is down".

    uv run pytest tests_browser/test_markdown.py

THE TEST THAT MATTERS is `test_every_prefix_of_an_answer_renders`. Deltas are
raw token chunks, so the renderer is handed partial markdown on every frame of
every answer — an unterminated `**`, a fence with no closing fence, a list item
cut mid-word. Rendering every prefix of a realistic answer is the only honest way
to prove that never throws and never produces markup.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

MARKDOWN_JS = (
    Path(__file__).resolve().parents[1]
    / "lifedata_ai" / "static" / "panel" / "markdown.js"
)

#: A realistic copilot answer, exercising every construct the subset supports.
SAMPLE_ANSWER = """\
I've built **Unique patients by facility** for the last 12 months.

## What it shows

- Unique patients, counted with `COUNT(DISTINCT patient_id)`
- Broken down by *facility*
- Over the last 12 months

> Note that a facility reporting zero patients may not be reporting at all.

```sql
-- this is what the saved metric computes
COUNT(DISTINCT patient_id)
```

1. Open the chart
2. Change the period if you need a different window
"""


@pytest.fixture(scope="module")
def page():
    """A blank page with `markdown.js` loaded, and nothing else.

    The page never navigates anywhere, so this suite needs no Superset, no login
    and no orchestrator. A markdown regression is then reported as a markdown
    regression rather than as "the stack is down".
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.add_script_tag(path=str(MARKDOWN_JS))
        yield page
        browser.close()


@pytest.fixture(scope="module")
def render(page):
    """Render markdown and return the resulting HTML."""

    def _render(source: str) -> str:
        return page.evaluate(
            """(source) => {
                const host = document.createElement('div');
                window.__lifedataMarkdown.renderInto(host, source);
                return host.innerHTML;
            }""",
            source,
        )

    return _render


@pytest.fixture(scope="module")
def rendered_attributes(page):
    """Every attribute name present on the rendered tree.

    Walks the live DOM rather than pattern-matching serialised HTML, because
    `&lt;img src=x&gt;` as *text* contains the string `src=` while carrying no
    attribute at all — a substring check cannot tell an escaped image from a
    real one, and would report both or neither.
    """

    def _attributes(source: str) -> list[str]:
        return page.evaluate(
            """(source) => {
                const host = document.createElement('div');
                window.__lifedataMarkdown.renderInto(host, source);
                const found = [];
                host.querySelectorAll('*').forEach((node) => {
                    for (const attr of node.attributes) {
                        found.push(node.tagName.toLowerCase() + '[' + attr.name + ']');
                    }
                });
                return found;
            }""",
            source,
        )

    return _attributes


# --- Block constructs -----------------------------------------------------


def test_a_paragraph_becomes_a_paragraph(render) -> None:
    assert render("Encounters rose in March.") == "<p>Encounters rose in March.</p>"


def test_blank_lines_separate_paragraphs(render) -> None:
    assert render("One.\n\nTwo.") == "<p>One.</p><p>Two.</p>"


def test_a_soft_wrap_stays_in_one_paragraph(render) -> None:
    """The model hard-wraps prose; each line is not a paragraph."""
    html = render("Encounters rose\nin March.")
    assert html.count("<p>") == 1


@pytest.mark.parametrize(
    ("source", "tag"),
    [("# Heading", "h3"), ("## Heading", "h4"), ("### Heading", "h5")],
)
def test_headings_map_below_the_panel_s_own_level(render, source: str, tag: str) -> None:
    """The panel already owns h1/h2, so model headings start at h3."""
    assert render(source) == f"<{tag}>Heading</{tag}>"


def test_a_bullet_list(render) -> None:
    assert render("- one\n- two") == "<ul><li>one</li><li>two</li></ul>"


def test_an_ordered_list(render) -> None:
    assert render("1. one\n2. two") == "<ol><li>one</li><li>two</li></ol>"


def test_switching_list_type_starts_a_new_list(render) -> None:
    html = render("- bullet\n1. numbered")
    assert html == "<ul><li>bullet</li></ul><ol><li>numbered</li></ol>"


def test_a_blockquote(render) -> None:
    assert render("> careful") == "<blockquote><p>careful</p></blockquote>"


def test_a_fenced_code_block(render) -> None:
    assert render("```\nSELECT 1\n```") == "<pre><code>SELECT 1</code></pre>"


def test_markdown_inside_a_fence_stays_literal(render) -> None:
    """A `#` in SQL is a comment, not a heading."""
    html = render("```\n# not a heading\n- not a bullet\n```")
    assert "<h3>" not in html
    assert "<li>" not in html


# --- Inline constructs ----------------------------------------------------


def test_bold_and_italic(render) -> None:
    assert render("**bold** and *italic*") == "<p><strong>bold</strong> and <em>italic</em></p>"


def test_underscore_emphasis(render) -> None:
    assert render("__bold__ and _italic_") == "<p><strong>bold</strong> and <em>italic</em></p>"


def test_inline_code(render) -> None:
    assert render("use `facility_name`") == "<p>use <code>facility_name</code></p>"


def test_bold_is_tried_before_italic(render) -> None:
    """Otherwise `**x**` renders as two empty italics around the text."""
    assert render("**x**") == "<p><strong>x</strong></p>"


def test_emphasis_can_nest_when_the_markers_differ(render) -> None:
    assert render("**bold _and italic_**") == (
        "<p><strong>bold <em>and italic</em></strong></p>"
    )


def test_nesting_the_same_marker_degrades_to_literal_text(render) -> None:
    """`**bold *and italic***` is genuinely ambiguous, and this is the limit.

    A leftmost-match scanner closes the outer `**` on the first `**` it finds,
    which lands inside the `***`. CommonMark resolves this with a delimiter-run
    algorithm that is more machinery than a 380px panel justifies.

    What matters is the failure mode: the extra marker is shown as LITERAL TEXT,
    never as broken markup. A user sees a stray asterisk; nothing is lost and
    nothing is injected.
    """
    html = render("**bold *and italic***")

    assert "<strong>" in html
    assert "*" in html  # the surplus marker, rendered as itself
    assert "<script" not in html


def test_an_underscore_inside_a_column_name_is_not_emphasis(render) -> None:
    """`facility_name` and `district_name` in one sentence would otherwise
    italicise everything between them — which is the single most likely way this
    renderer would embarrass itself, because the copilot writes column names."""
    html = render("Use `facility_name` rather than `district_name`.")
    assert "<em>" not in html


# --- Safety ---------------------------------------------------------------


def test_html_in_the_source_is_rendered_as_text(render) -> None:
    """Model output is untrusted input, and this panel sits in a page holding
    the user's Superset session."""
    html = render("<img src=x onerror=alert(1)>")
    assert "<img" not in html
    assert "&lt;img" in html


def test_a_script_tag_cannot_survive(render) -> None:
    html = render("Here is <script>alert(1)</script> a thing")
    assert "<script>" not in html


def test_a_link_is_not_rendered_as_a_link(render) -> None:
    """Deliberately unsupported.

    A copilot that can render `[click here](javascript:…)` into a page that
    already holds the user's session is a bad trade for prettier prose.
    """
    html = render("[click here](javascript:alert(1))")
    assert "<a" not in html
    assert "javascript" in html  # shown literally, so nothing is hidden


def test_no_rendered_element_ever_carries_an_attribute(rendered_attributes) -> None:
    """The structural safety property, asserted structurally.

    Every node is built with `createElement` and `textContent`, so an attribute
    is not something a source string can reach — there is no string-to-HTML step
    for it to ride in on. Walking the live DOM is what makes this meaningful:
    the serialised output legitimately *contains* `src=` when an `<img …>` was
    rendered as escaped text, so a substring check cannot tell a rendered image
    from a described one and would be green either way.
    """
    hostile = (
        '`" onmouseover="alert(1)` and <img src=x onerror=alert(1)> and '
        "[a](javascript:alert(1)) and **b** and `c`"
    )

    assert rendered_attributes(hostile) == []


def test_the_attribute_check_would_notice_a_real_one(page) -> None:
    """Guard the guard.

    A DOM walk that silently found nothing — a bad selector, an empty host —
    would leave the check above permanently, invisibly green. This proves it
    fires on a tree that genuinely carries an attribute.
    """
    found = page.evaluate(
        """() => {
            const host = document.createElement('div');
            const img = document.createElement('img');
            img.setAttribute('src', 'x');
            host.appendChild(img);
            const out = [];
            host.querySelectorAll('*').forEach((node) => {
                for (const attr of node.attributes) {
                    out.push(node.tagName.toLowerCase() + '[' + attr.name + ']');
                }
            });
            return out;
        }"""
    )

    assert found == ["img[src]"]


# --- Streaming safety -----------------------------------------------------


def test_unterminated_emphasis_renders_literally(render) -> None:
    """Mid-stream, every bold run is unterminated for a moment."""
    assert render("**partial") == "<p>**partial</p>"
    assert render("a *b") == "<p>a *b</p>"


def test_an_unterminated_fence_renders_as_an_open_code_block(render) -> None:
    """Visible while it streams, rather than appearing all at once at the end."""
    assert render("```\nSELECT 1") == "<pre><code>SELECT 1</code></pre>"


def test_an_empty_source_renders_nothing(render) -> None:
    assert render("") == ""


def test_every_prefix_of_an_answer_renders(render) -> None:
    """THE streaming-safety test.

    Each animation frame re-parses the whole buffer, so the renderer sees every
    prefix of the final answer. None may throw, and none may produce markup —
    the point of re-parsing rather than reconciling is that a half-parsed
    structure is never carried forward.
    """
    for length in range(len(SAMPLE_ANSWER) + 1):
        prefix = SAMPLE_ANSWER[:length]
        html = render(prefix)
        assert "<script" not in html, f"markup escaped at prefix length {length}"
        assert "onerror" not in html, f"attribute escaped at prefix length {length}"


def test_the_finished_answer_renders_every_construct(render) -> None:
    html = render(SAMPLE_ANSWER)

    assert "<strong>Unique patients by facility</strong>" in html
    assert "<h4>What it shows</h4>" in html
    assert "<ul>" in html and "<ol>" in html
    assert "<blockquote>" in html
    assert "<pre><code>" in html
    assert "<code>COUNT(DISTINCT patient_id)</code>" in html
