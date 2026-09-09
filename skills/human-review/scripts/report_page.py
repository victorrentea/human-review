"""Wrap a review fragment in the smallest document that renders it on its own.

Every generator next door emits an HTML *fragment* plus a `--css` stylesheet, and
`build-review-html.py` inlines the two into review.html. That contract is right for
anything the page shows — but it means the file on disk is unopenable: bare `<div>`s
against a white browser default, every `var(--line)` resolving to nothing, and in dark
mode grey text on grey. A link straight to such a file is worse than no link, because it
looks like the report failed rather than like it was never meant to be opened alone.

So a generator that wants its report to be *linkable* passes `--report`, and gets the
same body wrapped here: the page's own six design tokens, its typography, and a header
saying which review this fell out of. Nothing else — this is not a second renderer, and
anything that starts looking like page chrome belongs in build-review-html.py instead.

The tokens are copied from that file's `:root`, deliberately and not imported: pulling
them in would mean importing a 300KB module (and its Pygments dependency) to read seven
hex values. They are the contract between the fragments and the page; if they drift, the
fragment renders wrong *inside* review.html too, which is the louder failure.
"""
from __future__ import annotations

import html

# --- the review page's palette, verbatim from build-review-html.py's CSS -------------
TOKENS = """
:root {
  --bg:#fbfbfd; --fg:#1c1c22; --muted:#6b6b78; --line:#e2e2ea; --card:#ffffff;
  --code-bg:#f6f6fa; --link:#1a4fa0;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg:#15151a; --fg:#e8e8ef; --muted:#9a9aa8; --line:#2c2c36; --card:#1d1d24;
    --code-bg:#101015; --link:#8ab4f8;
  }
}
body { margin:0; background:var(--bg); color:var(--fg);
       font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
/* The same 1080px column the review reads in, so a report opened on its own keeps the
   line length its tables and prose were laid out for. */
.wrap { max-width:1080px; margin:0 auto; padding:1.4rem 1.25rem 5rem; }
/* One line of provenance, in the shape of the page's masthead: what this is, then what
   it was computed from. A reader arrives here from a link in a verdict band and has no
   other way to tell which two specs produced it. */
.rp-head { margin:0 0 .3rem; font-size:1.25rem; font-weight:700; }
.rp-sub { margin:0 0 1.4rem; color:var(--muted); font-size:.92rem; }
.rp-sub a { color:var(--link); }
"""


def wrap(title: str, css: str, body: str, subtitle: str = "") -> str:
    """A self-contained document around `body`, styled by `css` plus the page's tokens.

    `subtitle` is HTML, not text: callers pass their own provenance line, which already
    carries `<code>` around a ref and a link back to the tool that produced the numbers.
    `title` is text and is escaped, because it also becomes the browser tab's name."""
    safe = html.escape(title)
    sub = f'<p class="rp-sub">{subtitle}</p>' if subtitle else ""
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        f"<title>{safe}</title>\n"
        f"<style>{TOKENS}\n{css}</style>\n"
        f'</head><body><div class="wrap">\n'
        f'<h1 class="rp-head">{safe}</h1>\n{sub}\n{body}\n'
        "</div></body></html>\n"
    )
