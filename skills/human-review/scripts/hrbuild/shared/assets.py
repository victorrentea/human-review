"""The page's CSS and JavaScript, kept as real files under ``assets/``.

Every block below used to be a multi-thousand-line string literal in
``build-review-html.py``. They are the same bytes, moved to files an editor can
highlight and a diff can read: ``assets/*.css`` verbatim, ``assets/*.js`` without the
``<script>`` wrapper this module puts back. Nothing here is assembled or reordered --
the order the page emits them in lives in ``build-review-html.py``'s document template,
where it always did.
"""
from __future__ import annotations

from pathlib import Path

ASSETS = Path(__file__).resolve().parent.parent / "assets"


def _text(name: str) -> str:
    """One asset file, byte for byte."""
    return (ASSETS / name).read_text(encoding="utf-8")


def _script(name: str) -> str:
    """One asset file as the inline <script> block the page inlines.

    The wrapper is added here rather than stored in the file so that ``assets/*.js`` is
    JavaScript -- valid for an editor, a linter and a syntax highlighter -- instead of an
    HTML fragment wearing a .js extension."""
    return "<script>\n" + _text(name) + "</script>"


CSS = _text("page.css")


# The footer's own rule, in its own file rather than a line in `page.css`: see
# `assets/footer.css` for why. Emitted straight after `CSS`, ahead of a generator's
# `extra_css` and of `LATE_CSS`, so either can still outrank it the way they outrank the
# base sheet — nothing here needs to win a fight, it only needs to not lose the one
# `page.css`'s bare `a { color:var(--link); }` would otherwise hand it by default.
FOOTER_CSS = _text("footer.css")


# Emitted *after* every other stylesheet — the fragments' own CSS included — because these
# rules exist to outrank the base sheet's `button.tab { padding:0 .85rem }`. Anywhere
# earlier in the block and the cascade quietly reverts them, with no error and no visible
# clue beyond a tab strip that has silently wrapped onto two rows.
LATE_CSS = _text("late.css")


CAPTION_JS = _script("caption.js")


FOCUS_JS = _script("focus.js")

DGM_VIEWS_JS = _script("dgm-views.js")


SERVER_JS = _script("server.js")


RERUN_JS = _script("rerun.js")


APP_ENV_JS = _script("app-env.js")

TIP_JS = _script("tip.js")


FRAME_JS = _script("frame.js")


TRACE_JS = _script("trace.js")


SEQLINK_JS = _script("seqlink.js")


SEQFOLD_JS = _script("seqfold.js")

HSCROLL_JS = _script("hscroll.js")


TABS_JS = _script("tabs.js")


# Emitted in the <head>, which is the only place early enough: the first frame the browser
# paints is already past the first panel, and a class added at the foot of the body would
# be a class added after the flicker it exists to prevent. The hold is put on by script
# and not written into the markup, so a reader with no JavaScript -- and the page saved to
# disk and opened from a zip -- gets the document it always got, every panel on screen,
# rather than an invisible one nothing will ever reveal.
PAINT_HOLD_JS = _script("paint-hold.js")


# The release, and it is its own script rather than the last line of TABS_JS on purpose:
# TABS_JS returns early on a page built without a tab strip, and a throw anywhere inside
# it would strand the hold. A separate tag runs either way, and by the time it does the
# strip has hidden eleven of the twelve panels, so the first frame the reader ever sees is
# the one tab they asked for.
PAINT_RELEASE_JS = _script("paint-release.js")

XREF_CSS = _text("xref.css")


XREF_JS = _script("xref.js")


EDITOR_JS = _script("editor.js")

GENSEQ_JS = _script("genseq.js")
