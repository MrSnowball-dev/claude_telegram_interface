"""Convert Claude's Markdown output to Telegram-flavored HTML.

Telegram supports a small subset of HTML for ``parse_mode=HTML``:
``<b>``, ``<i>``, ``<u>``, ``<s>``, ``<code>``, ``<pre>``,
``<a href=…>``, ``<blockquote>``. Headings, lists, paragraphs, etc. aren't
supported — we keep them as plain text (lists already read fine that way) and
promote ``# heading`` lines to ``<b>heading</b>``.

We process fenced code blocks and inline code first (extracted as
placeholders), HTML-escape the rest, then transform the markdown features.
Incomplete markdown (``**bold`` with no closing ``**``) is left as raw text —
the regexes don't match, so it doesn't produce malformed HTML during streaming.
"""

from __future__ import annotations

import re
from html import escape as _esc

_FENCED_CODE_RE = re.compile(r"```([a-zA-Z0-9_+\-]*)\n(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(?=\S)([^*\n]+?)(?<=\S)\*\*")
_ITALIC_STAR_RE = re.compile(r"(?<![*\w])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![*\w])")
_ITALIC_UNDERSCORE_RE = re.compile(r"(?<!\w)_(?=\S)([^_\n]+?)(?<=\S)_(?!\w)")
_STRIKE_RE = re.compile(r"~~(?=\S)([^~\n]+?)(?<=\S)~~")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)

_PLACEHOLDER_RE = re.compile(r"\x00MD(\d+)\x01")


def md_to_html(text: str) -> str:
    """Convert Markdown to Telegram-safe HTML."""
    placeholders: list[str] = []

    def _stash(content: str) -> str:
        idx = len(placeholders)
        placeholders.append(content)
        return f"\x00MD{idx}\x01"

    def _fenced(m: re.Match[str]) -> str:
        lang, body = m.group(1), m.group(2)
        body_html = _esc(body, quote=False)
        if lang:
            return _stash(f'<pre><code class="language-{_esc(lang, quote=True)}">{body_html}</code></pre>')
        return _stash(f"<pre>{body_html}</pre>")

    text = _FENCED_CODE_RE.sub(_fenced, text)

    def _inline(m: re.Match[str]) -> str:
        return _stash(f"<code>{_esc(m.group(1), quote=False)}</code>")

    text = _INLINE_CODE_RE.sub(_inline, text)

    # Escape the rest in one pass — code spans are already stashed as placeholders.
    # quote=True so URLs that go on to live inside ``href="…"`` attributes
    # don't break out of the attribute.
    text = _esc(text, quote=True)

    text = _HEADER_RE.sub(lambda m: f"<b>{m.group(2)}</b>", text)
    text = _BOLD_RE.sub(r"<b>\1</b>", text)
    text = _ITALIC_STAR_RE.sub(r"<i>\1</i>", text)
    text = _ITALIC_UNDERSCORE_RE.sub(r"<i>\1</i>", text)
    text = _STRIKE_RE.sub(r"<s>\1</s>", text)
    # URL is already escaped by the bulk pass above; don't double-escape.
    text = _LINK_RE.sub(r'<a href="\2">\1</a>', text)

    text = _PLACEHOLDER_RE.sub(lambda m: placeholders[int(m.group(1))], text)
    return text
