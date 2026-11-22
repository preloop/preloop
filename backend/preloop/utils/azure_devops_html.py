"""HTML <-> markdown for Azure Boards rich text fields.

Azure Boards stores ``System.Description`` and comment text as HTML, while
the rest of Preloop (agents, MCP tools, flow prompts) works in markdown. The
Jira client solves the same problem for Atlassian Document Format; this
module is the Azure counterpart and, like it, keeps to the stdlib.

Supported both ways: paragraphs and line breaks, headings, bold, italic,
strikethrough, inline code, fenced code blocks, links, bullet and numbered
lists (nested by indentation), block quotes and horizontal rules.

Lossy, by design:

* HTML to markdown drops styling (colours, fonts, alignment), tables (cells
  become text separated by `` | ``), images (kept as ``![alt](src)``), and
  ``@mention`` markup (kept as its display text).
* Markdown to HTML does not render tables, task lists or HTML embedded in
  the markdown: such text is escaped and shown literally.

A round trip of markdown through HTML and back reproduces the supported
constructs; whitespace inside paragraphs is normalised.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import List, Optional, Tuple

_BLOCK_TAGS = {
    "p",
    "div",
    "section",
    "article",
    "header",
    "footer",
    "table",
    "tr",
    "blockquote",
    "ul",
    "ol",
    "pre",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
}
_EMPHASIS = {
    "strong": "**",
    "b": "**",
    "em": "_",
    "i": "_",
    "s": "~~",
    "strike": "~~",
    "del": "~~",
}
_SKIP_CONTENT = {"script", "style", "head", "title"}


class _MarkdownWriter(HTMLParser):
    """Stream HTML events into markdown text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: List[str] = []
        self.line: List[str] = []
        self.lists: List[Tuple[str, int]] = []
        self.quote = 0
        self.pre = False
        self.pre_lang = ""
        self.code_inline = False
        self.skip = 0
        self.links: List[Optional[str]] = []
        self.link_start: List[int] = []
        self.heading: Optional[int] = None
        self.cell_index = 0
        self.item_prefix = ""

    # -- output helpers -------------------------------------------------

    def _prefix(self) -> str:
        return "> " * self.quote

    def _flush(self) -> None:
        text = "".join(self.line)
        self.line = []
        if self.pre:
            return
        text = re.sub(r"[ \t\r\n\xa0]+", " ", text)
        text = re.sub(r" ?\x01 ?", "\x01", text).strip(" \x01")
        prefix, self.item_prefix = self.item_prefix, ""
        if not text:
            return
        text = text.replace("\x01", "\n" + self._prefix() + " " * len(prefix))
        text = prefix + text
        if self.heading:
            text = f"{'#' * self.heading} {text}"
        self.blocks.append(self._prefix() + text)

    def _emit_block(self, text: str) -> None:
        self._flush()
        self.blocks.append(text)

    # -- parser events ----------------------------------------------------

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        attr = dict(attrs)
        if tag in _SKIP_CONTENT:
            self.skip += 1
            return
        if self.pre:
            if tag == "br":
                self.line.append("\n")
            elif tag == "code":
                match = re.search(r"language-([\w+-]+)", attr.get("class") or "")
                self.pre_lang = match.group(1) if match else self.pre_lang
            return
        if tag in _BLOCK_TAGS or tag == "li":
            self._flush()
            self.item_prefix = ""
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.heading = int(tag[1])
        elif tag == "br":
            self.line.append("\x01")
        elif tag == "hr":
            self.blocks.append(self._prefix() + "---")
        elif tag == "blockquote":
            self.quote += 1
        elif tag in {"ul", "ol"}:
            self.lists.append((tag, 0))
        elif tag == "li":
            depth = max(len(self.lists) - 1, 0)
            kind, count = self.lists[-1] if self.lists else ("ul", 0)
            if self.lists:
                self.lists[-1] = (kind, count + 1)
            marker = f"{count + 1}." if kind == "ol" else "-"
            self.item_prefix = "   " * depth + marker + " "
        elif tag == "pre":
            self.pre = True
            self.pre_lang = ""
        elif tag == "code":
            self.code_inline = True
            self.line.append("`")
        elif tag in _EMPHASIS:
            self.line.append(_EMPHASIS[tag])
        elif tag == "a":
            href = attr.get("href")
            self.links.append(href)
            self.link_start.append(len(self.line))
            if href:
                self.line.append("[")
        elif tag == "img":
            src = attr.get("src") or ""
            self.line.append(f"![{attr.get('alt') or ''}]({src})")
        elif tag in {"td", "th"}:
            if self.cell_index:
                self.line.append(" | ")
            self.cell_index += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_CONTENT:
            self.skip = max(self.skip - 1, 0)
            return
        if tag == "pre":
            code = "".join(self.line).strip("\n")
            self.line = []
            self.pre = False
            fence = "```" + self.pre_lang
            self.blocks.append(f"{fence}\n{code}\n```")
            return
        if self.pre:
            return
        if tag == "code":
            if self.code_inline:
                self.line.append("`")
                self.code_inline = False
        elif tag in _EMPHASIS:
            self.line.append(_EMPHASIS[tag])
        elif tag == "a":
            href = self.links.pop() if self.links else None
            start = self.link_start.pop() if self.link_start else len(self.line)
            if href:
                text = "".join(self.line[start + 1 :])
                if text.strip() == href:
                    # A bare URL stays bare.
                    self.line[start:] = [href]
                else:
                    self.line.append(f"]({href})")
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._flush()
            self.heading = None
        elif tag == "blockquote":
            self._flush()
            self.quote = max(self.quote - 1, 0)
        elif tag in {"ul", "ol"}:
            self._flush()
            if self.lists:
                self.lists.pop()
        elif tag == "tr":
            self._flush()
            self.cell_index = 0
        elif tag in _BLOCK_TAGS or tag == "li":
            self._flush()

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        self.line.append(data)

    def result(self) -> str:
        self._flush()
        return _join_blocks(self.blocks)


def _join_blocks(blocks: List[str]) -> str:
    """Join blocks: list items and quote lines stay adjacent."""
    out: List[str] = []
    for block in blocks:
        if out and _is_list_item(block) and _is_list_item(out[-1]):
            out.append("\n" + block)
        elif out and block.startswith(">") and out[-1].startswith(">"):
            out.append("\n" + block)
        elif out:
            out.append("\n\n" + block)
        else:
            out.append(block)
    return "".join(out).strip()


_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s")


def _is_list_item(text: str) -> bool:
    return bool(_LIST_ITEM_RE.match(text))


def html_to_markdown(value: Optional[str]) -> str:
    """Convert an Azure Boards HTML field to markdown.

    Args:
        value: HTML from ``System.Description`` or a comment ``text``.

    Returns:
        Markdown text; empty for an empty field.
    """
    if not value:
        return ""
    writer = _MarkdownWriter()
    writer.feed(value)
    writer.close()
    return writer.result()


# ----------------------------------------------------------------------
# Markdown -> HTML
# ----------------------------------------------------------------------

_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+|mailto:[^\s)]+)\)")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\((https?://[^\s)]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_ITALIC_RE = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?!\w)|(?<!\w)_(.+?)_(?!\w)")
_STRIKE_RE = re.compile(r"~~(.+?)~~")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_ITEM_RE = re.compile(r"^(\s*)([-*+]|\d+\.)\s+(.*)$")
_URL_RE = re.compile(r"(?<![\"'=(>])\bhttps?://[^\s<]+")


def _inline(text: str) -> str:
    """Render inline markdown; everything else is escaped."""
    stash: List[str] = []

    def keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    text = _INLINE_CODE_RE.sub(
        lambda m: keep(f"<code>{html.escape(m.group(1), quote=False)}</code>"), text
    )
    text = _IMAGE_RE.sub(
        lambda m: keep(
            f'<img src="{html.escape(m.group(2))}" alt="{html.escape(m.group(1))}">'
        ),
        text,
    )
    text = _LINK_RE.sub(
        lambda m: keep(
            f'<a href="{html.escape(m.group(2))}">'
            f"{html.escape(m.group(1), quote=False)}</a>"
        ),
        text,
    )
    text = _URL_RE.sub(
        lambda m: keep(
            f'<a href="{html.escape(m.group(0))}">'
            f"{html.escape(m.group(0), quote=False)}</a>"
        ),
        text,
    )
    text = html.escape(text, quote=False)
    text = _BOLD_RE.sub(lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", text)
    text = _ITALIC_RE.sub(lambda m: f"<em>{m.group(1) or m.group(2)}</em>", text)
    text = _STRIKE_RE.sub(lambda m: f"<s>{m.group(1)}</s>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)


def _render_list(items: List[Tuple[int, str, str]]) -> str:
    """Render ``(indent, marker, text)`` items as nested lists."""
    out: List[str] = []
    stack: List[Tuple[int, str]] = []
    for indent, marker, text in items:
        tag = "ol" if marker[0].isdigit() else "ul"
        while stack and indent < stack[-1][0]:
            out.append(f"</li></{stack.pop()[1]}>")
        if stack and indent == stack[-1][0]:
            if stack[-1][1] != tag:
                out.append(f"</li></{stack.pop()[1]}><{tag}><li>")
                stack.append((indent, tag))
            else:
                out.append("</li><li>")
        else:
            out.append(f"<{tag}><li>")
            stack.append((indent, tag))
        out.append(_inline(text))
    while stack:
        out.append(f"</li></{stack.pop()[1]}>")
    return "".join(out)


def markdown_to_html(value: Optional[str]) -> str:
    """Convert Preloop markdown to HTML for an Azure Boards field.

    Args:
        value: Markdown (or plain) text.

    Returns:
        HTML; empty for empty input.
    """
    if not value or not value.strip():
        return ""
    lines = value.replace("\r\n", "\n").split("\n")
    out: List[str] = []
    paragraph: List[str] = []
    items: List[Tuple[int, str, str]] = []
    quote: List[str] = []

    def close_paragraph() -> None:
        if paragraph:
            out.append("<p>" + "<br>".join(_inline(p) for p in paragraph) + "</p>")
            paragraph.clear()

    def close_list() -> None:
        if items:
            out.append(_render_list(items))
            items.clear()

    def close_quote() -> None:
        if quote:
            out.append(
                f"<blockquote>{markdown_to_html(chr(10).join(quote))}</blockquote>"
            )
            quote.clear()

    def close_all() -> None:
        close_paragraph()
        close_list()
        close_quote()

    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped.startswith("```"):
            close_all()
            lang = stripped[3:].strip()
            code: List[str] = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code.append(lines[index])
                index += 1
            cls = f' class="language-{html.escape(lang)}"' if lang else ""
            body = html.escape("\n".join(code), quote=False)
            out.append(f"<pre><code{cls}>{body}</code></pre>")
            index += 1
            continue
        if stripped.startswith(">"):
            close_paragraph()
            close_list()
            quote.append(re.sub(r"^\s*>\s?", "", line))
            index += 1
            continue
        close_quote()
        heading = _HEADING_RE.match(stripped)
        item = _ITEM_RE.match(line)
        if not stripped:
            close_paragraph()
            close_list()
        elif stripped in {"---", "***", "___"}:
            close_all()
            out.append("<hr>")
        elif heading:
            close_all()
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2).strip())}</h{level}>")
        elif item:
            close_paragraph()
            items.append((len(item.group(1)), item.group(2), item.group(3)))
        elif items and line.startswith(" "):
            indent, marker, text = items[-1]
            items[-1] = (indent, marker, f"{text} {stripped}")
        else:
            close_list()
            paragraph.append(stripped)
        index += 1
    close_all()
    return "".join(out)
