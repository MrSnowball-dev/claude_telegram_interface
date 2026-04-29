from bot.markdown import md_to_html


def test_plain_text_passthrough() -> None:
    assert md_to_html("hello world") == "hello world"


def test_bold() -> None:
    assert md_to_html("This is **bold** text.") == "This is <b>bold</b> text."


def test_italic_star_and_underscore() -> None:
    assert md_to_html("This is *italic* text.") == "This is <i>italic</i> text."
    assert md_to_html("This is _italic_ text.") == "This is <i>italic</i> text."


def test_strikethrough() -> None:
    assert md_to_html("This is ~~gone~~ now.") == "This is <s>gone</s> now."


def test_inline_code_escapes_html() -> None:
    out = md_to_html("Run `echo <hi>` here.")
    assert "<code>echo &lt;hi&gt;</code>" in out


def test_fenced_code_block_with_lang() -> None:
    md = "Use this:\n```python\nprint('hi')\n```"
    out = md_to_html(md)
    assert '<pre><code class="language-python">' in out
    assert "print(&#x27;hi&#x27;)" in out or "print('hi')" in out
    assert "</code></pre>" in out


def test_fenced_code_block_no_lang() -> None:
    md = "```\nplain code\n```"
    out = md_to_html(md)
    assert "<pre>plain code\n</pre>" in out


def test_link() -> None:
    out = md_to_html("See [the docs](https://example.com/api?a=1&b=2).")
    assert '<a href="https://example.com/api?a=1&amp;b=2">the docs</a>' in out


def test_header_becomes_bold() -> None:
    out = md_to_html("# Title\n\nbody")
    assert "<b>Title</b>" in out
    assert "body" in out


def test_html_in_plain_text_is_escaped() -> None:
    out = md_to_html("a < b and c > d, & here")
    assert "<" not in out.replace("&lt;", "")
    assert out == "a &lt; b and c &gt; d, &amp; here"


def test_incomplete_bold_left_as_text() -> None:
    """Mid-stream chunk with unclosed formatting must not produce malformed HTML."""
    out = md_to_html("Hello **wo")
    # No <b> tag; the literal ** survives.
    assert "<b>" not in out
    assert "**wo" in out


def test_incomplete_inline_code_left_as_text() -> None:
    out = md_to_html("Run `incomplete")
    assert "<code>" not in out
    assert "`incomplete" in out


def test_code_block_protects_inner_markdown() -> None:
    md = "```\n**not bold inside code**\n```"
    out = md_to_html(md)
    assert "<b>" not in out
    assert "**not bold inside code**" in out


def test_multiple_features_together() -> None:
    md = "**Bold** and `code` and [link](http://x.com)"
    out = md_to_html(md)
    assert "<b>Bold</b>" in out
    assert "<code>code</code>" in out
    assert '<a href="http://x.com">link</a>' in out
