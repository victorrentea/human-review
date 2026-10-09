"""Commands the page offers: copy, run, rerun, regenerate, reveal."""
from __future__ import annotations

import html
import json
import re
from pathlib import Path
import shlex

from .actions import ACTIONS, declare_action

#: The one glyph a command wears — whichever of the two is true of *this* copy of the
#: report. Characters and not SVG: they are one text node each, they inherit the pill's
#: colour and size for free in both themes, and a page that carries eleven of these does
#: not want eleven inline documents in it.
#:
#: The run glyph is a circular arrow and not a ▶ because every command this page offers is
#: a *rerun*: it re-derives something the page is already showing and the page then catches
#: up with it. A play triangle promises a thing that starts and plays; this promises the
#: thing that comes round again, which is also what the masthead's badge and the spinner
#: mid-run are drawn from. One mark, learnt once, everywhere it can happen.
#:
#: `↻` and not the 🔃 emoji, for two reasons that both come down to it being *text*. It
#: takes the pill's colour — the served badge is green and the paid chip is amber, and an
#: emoji is a picture that stays its own colours inside both of them. And it is a stroke
#: rather than a two-tone glyph, so at .82rem it reads as a mark instead of a small
#: illustration. Picked over `⟳` (U+27F3) and `⥁` (U+2941) by measuring them: at this size
#: those come out a fifth to a third narrower, and over `⭮` (U+2BAE), which measured
#: exactly as wide as a private-use codepoint — i.e. it was tofu.
CMD_COPY = "\U0001F4CB"   # 📋
#: `↺`, not `↻`, since Victor gave each refresh mode one mark of its own: ↺ regenerates
#: the report (free), ↺⏳ re-runs the tests and the other slow steps first (free, minutes),
#: 🤖 re-evaluates with a model (paid). ↺⏳ is the one two-mark face, at Victor's asking:
#: the same regenerate, with the wait it costs drawn beside it.
CMD_RUN = "\u21BA"        # ↺

#: The two marks the Demo row wears instead of `↻`, because neither of its verbs is a
#: rerun. Start begins something that then keeps running, Stop ends it — and the circular
#: arrow, which this page teaches everywhere else as "the thing that comes round again",
#: would say the opposite of both. It is the one place a different glyph
#: is earned: every other command here re-derives something the page is already showing.
#:
#: Text presentation (`\uFE0E`) on the triangle for the same reason `↻` is not the 🔃
#: emoji — inside a pill the glyph has to take the pill's colour, and the play is green while
#: the square is red. `\u25A0` and not `⏹` (U+23F9), which is an emoji by default and whose
#: text form is tofu in more fonts than not: a filled square *is* the stop mark, and the red
#: it is drawn in here is the half of "stop" the shape alone does not carry.
CMD_PLAY = "\u25B6\uFE0E"  # ▶
CMD_STOP = "\u25A0"         # ■

#: The three rerun presses — masthead, tab strip, and the Tests tab's own — wear one face:
#: a round button, a ↻ ring drawn in the button's colour, and one emoji inside it saying
#: which kind of rerun it is. Victor's mapping (5 Oct 2026), and it is what each one does:
#: ⚙️ regenerates from scripts (free, seconds), ⏳ runs the slow steps first (free,
#: minutes), 🤖 asks a model (paid). One face, so a reader learns the ring once and reads
#: only the emoji. The ring is SVG rather than ↺ because a text arrow cannot hold a second
#: glyph inside it; it is `currentColor`, so it still takes the green or the amber. The
#: ring is the button's only edge: it runs at nearly the full diameter, with no pill border
#: or ground behind it, and its head is drawn large enough to read as an arrow at 19px.
RERUN_MARK_SCRIPT = "\u2699\uFE0F"   # ⚙️
RERUN_MARK_SLOW = "\u23F3"            # ⏳
RERUN_MARK_AI = "\U0001F916"          # 🤖
_RERUN_RING = ('<svg class="rr-ring" viewBox="0 0 24 24" aria-hidden="true">'
               '<path d="M18.5 5.5A9.2 9.2 0 1 1 5.5 5.5" fill="none" stroke="currentColor" '
               'stroke-width="2.4" stroke-linecap="round"/>'
               '<path d="M7.8 3.2L6.9 8.9L2.1 4.1Z" fill="currentColor" stroke="currentColor" '
               'stroke-width=".8" stroke-linejoin="round"/></svg>')


def rerun_face(mark: str) -> str:
    """The face of every rerun press: the ring, which is what turns while it runs, and the
    emoji, which holds still inside it."""
    return f'<span class="rr-ico">{_RERUN_RING}<span class="rr-mark">{mark}</span></span>'

# The masthead's Rerun — which is also the served badge, because they are one fact.
#
# Not to be confused with `rerun_html` further down, which is the offer under a *diagram*
# — one picture, re-rendered, from a command the build declared in the manifest. This one
# is the whole page, from a command the build never sees: it belongs to the server.
#
# It used to be a chip reading `Rerun` beside a chip reading `served`, and the two were
# saying the same thing twice. A page is served *exactly when* it can rerun itself: the
# badge announced the condition and the button beside it was the only thing that condition
# let you do. So the badge is the button. The glyph is the run mark every command on the
# page now wears, which makes the masthead the place a reader learns it — and its absence
# is what `static` means, in the one word that is left when nothing can run here.
#
# Emitted hidden and raised by the probe, like every other control here. A static copy has
# no process behind it, and a button that copied a shell line instead would be handing back
# the terminal round trip this exists to remove.
#
# The tooltip carries both halves, and names what the button will NOT do, because that is
# the part a reader cannot see and the part they are right to worry about: the findings on
# this page are a judgement bought once, and the film costs minutes and a running
# application.
RERUN_CHIP = ('<button type="button" class="chip chip-rerun chip-served" id="hr-rerun" '
              'hidden aria-disabled="true" data-rerun="__rerun__" '
              'aria-label="Regenerate this report" '
              # The same words as every tab's ⚙️ (`TAB_RERUN_TIP`): one glyph, one wording
              # (UX review, 7 Oct 2026). It used to be its own sentence — "Rebuild the page.
              # Free. Not the findings, not the film." — which made the reader wonder whether
              # the masthead ⚙️ and a tab's ⚙️ were two different presses.
              'data-tip="Regenerate (scripted, free)">'
              f'{rerun_face(RERUN_MARK_SCRIPT)}</button>')

# The same button with the model's half in front of it, and the only control on this page
# that spends money.
#
# It is a second button rather than a modifier on the first because the difference between
# them is not a degree of thoroughness — it is that one of them is free and reproducible
# and the other buys a judgement. A single Rerun that sometimes called a model would make
# every press a question about what it was about to do; two buttons make the answer the
# label.
#
# Three things guard it, in this order, and none of them is a substitute for another:
# the price is in the hover before the click, the click opens a dialog that says the price
# again and defaults to nothing, and the server will not honour it at all unless
# `rerun-model.py` is really beside it. The tooltip leads with the money, in those words,
# because "costs money" is the part a reader cannot see and the part they are right to
# worry about — everything else about this button is legible from its label.
RERUN_AI_CHIP = ('<button type="button" class="chip chip-rerun chip-rerun-ai" '
                 'id="hr-rerun-ai" hidden aria-disabled="true" '
                 'data-rerun="__rerun_ai__" '
                 'aria-label="Rerun with AI \u2014 costs about $5" '
                 # `{price}` is filled by RERUN_JS out of the probe, which derives it
                 # from what this page's own paid runs have really cost. The rendered
                 # `data-tip` carries the range as its fallback, because the markup is
                 # built once and read by a static copy too, where nothing fills anything.
                 'data-tip-fmt="costs money: {price} on Sonnet. AI redoes the '
                 'requirements↔tests map." '
                 'data-tip="costs money: ~$5\u2013$10 on Sonnet. AI redoes the '
                 'requirements↔tests map.">'
                 # One mark: the model. It used to be the free one's arrow plus this,
                 # and before that a `+` and a banknote too; each refresh mode now has one
                 # mark of its own (↺ regenerate, ↺⏳ re-run tests, 🤖 re-evaluate). The `+` and the flying banknote that used to sit
                 # between and after them made a four-glyph rebus at .7rem, which Victor
                 # read as noise; the money is said where it can be said in words — first
                 # in the hover, then again in the dialog — and the amber edge is the
                 # at-a-glance "this one is different" (solid: Victor did not want it dashed). No words on the face, because
                 # a label reading `Rerun + AI` cost the masthead two words to say
                 # `rerun` a second time.
                 f'{rerun_face(RERUN_MARK_AI)}</button>')

#: The face of a run-the-tests press, masthead and Tests tab alike: the ring around the
#: hourglass — "regenerate, and wait for what takes long".
RUN_TESTS_FACE = rerun_face(RERUN_MARK_SLOW)


def rerun_tests_chip(info: dict | None) -> str:
    """The masthead's ↺⏳, beside the ↺: re-run the tests and every other slow producer,
    forced, then rebuild. Same machine as ↺ (`rerun.js`, `/__rerun__` with `mode:"tests"`,
    the shared lock and the reload hold), raised by the probe's `rerunTestsAll`. Empty
    without `info` (`actions.declare_rerun_tests_action`)."""
    if not info:
        return ""
    steps = html.escape(",".join(info.get("steps") or []), quote=True)
    return ('<button type="button" class="chip chip-rerun chip-served chip-rerun-tests" '
            'id="hr-rerun-tests" hidden aria-disabled="true" '
            f'data-rerun="{html.escape(info["id"], quote=True)}" data-steps="{steps}" '
            'aria-label="Re-run the tests and every other slow step, then regenerate" '
            f'data-tip="{html.escape(info["tip"], quote=True)}">{RUN_TESTS_FACE}</button>')


TAB_RERUN_TIP = "Regenerate (scripted, free)"


def tab_rerun_html(tab_id: str, label: str, info: dict | None) -> str:
    """The masthead's ↻, narrowed to one tab, at the end of that tab's own title.

    It used to sit on the tab strip, drawn into the selected pill — and a name with three
    rings after it ("Tests ⚙️⏳🤖") read as part of the name (Victor, 7 Oct 2026). The strip
    is navigation, so it now carries the labels and nothing else; the presses go where the
    tab says what it is (`place_tab_reruns`), the same 18px rings as the masthead's.

    Same two faces as the masthead, and the same machine behind both (`rerun.js`): the
    green ↻ re-runs this tab's producers and rebuilds the page, free; the amber 🤖 is
    there only on a tab with a model half (Tests, Review, Demo) and opens the same confirmation
    the masthead's paid chip does. Empty when the tab has no producer to re-run; and on a
    static copy every button stays `hidden`, so the span takes no room at all."""
    if not info:
        return ""
    tid = html.escape(tab_id, quote=True)
    name = html.escape(label, quote=True)
    steps = html.escape(",".join(info.get("steps") or []), quote=True)
    # The verb and the two facts that decide whether to press it: a script does it, not a
    # model, and it costs nothing. Which producers it runs is the command's business, one
    # hover away on the server's own log, not the tooltip's.
    tip = html.escape(info.get("tip") or TAB_RERUN_TIP, quote=True)
    out = ('<span class="tabre">'
           '<button type="button" class="chip chip-rerun chip-served tabrerun" hidden '
           f'aria-disabled="true" data-rerun="__rerun__" data-tab="{tid}" '
           f'data-steps="{steps}" aria-label="Rerun the {name} tab" '
           f'data-tip="{tip}">'
           f'{rerun_face(RERUN_MARK_SCRIPT)}</button>')
    # A tab's own further presses (the Tests tab's ↺⏳, which runs the suites first), drawn
    # by the tab that owns them and placed here, inside the span, so the group moves as one.
    # Right after the ↺, before the paid one: ↺ then ↺⏳ are the same free verb, the second
    # one slower, and they read as a pair only when nothing stands between them.
    out += info.get("extra") or ""
    if info.get("ai"):
        tip = html.escape(info.get("aiTip") or "", quote=True)
        confirm = html.escape(info.get("aiConfirm") or info.get("aiTip") or "", quote=True)
        out += ('<button type="button" class="chip chip-rerun chip-rerun-ai tabrerun" hidden '
                f'aria-disabled="true" data-rerun="__rerun_ai__" data-tab="{tid}" '
                f'data-steps="{steps}" aria-label="Rerun the {name} tab with AI \u2014 costs money" '
                f'data-confirm="{confirm}" '
                # The probe quotes each paid program out of its own ledger: the matrix's
                # (the Tests tab) and the film script's (the Demo tab). `data-price` names
                # which — absent means the matrix's, as it always did. The re-review is
                # priced in its sentence, so it wears no figure.
                + (f'data-tip-fmt="costs money: {{price}} on Sonnet. {tip}" '
                   if info.get("priced") else "")
                + (f'data-price="{html.escape(info["price"], quote=True)}" '
                   if info.get("priced") and info.get("price") not in (None, "model")
                   else "") +
                f'data-tip="costs money. {tip}">'
                f'{rerun_face(RERUN_MARK_AI)}</button>')
    return out + "</span>"


#: A tab that opens on no title of its own gets this one, so its presses have somewhere to
#: stand: Structure was a stack of diagram cards with nothing above the first (Victor,
#: 7 Oct 2026). Any other untitled tab with presses is given its own label.
TAB_TITLES = {"packages": "Structure diagrams"}

#: Tabs whose header is each of their cards: Data is three pictures of one model (the
#: domain, the database, the concept), each in its own card, and each card carries the
#: tab's presses at the end of its own title.
CARD_HEADED_TABS = frozenset({"data"})

_TABTITLE_OPEN = re.compile(r'<h2 class="tabtitle\b[^>]*>')
_CARD_TITLE = re.compile(r'(<div class="diagram\b[^"]*"[^>]*><div class="head"><b>.*?</b>)', re.S)


def tab_title_row(title: str) -> str:
    """The title row an untitled tab is given: the `h2.tabtitle` every titled tab opens on."""
    return f'<div class="adopthead"><h2 class="tabtitle">{html.escape(title)}</h2></div>'


def place_tab_reruns(tab_id: str, label: str, body: str, presses: str) -> str:
    """Put a tab's presses (`tab_rerun_html`) at the end of that tab's own title.

    Read off the panel's markup, not off a list per tab, because five of the tabs are a
    producer's fragment pasted whole (CLAUDE.md): wherever the tab says what it is, in this
    order — the Review tab's pile line (its sticky header), a `h2.tabtitle` (Tests,
    Sequence, City, UX, Complexity, Logging, CODEOWNERS, the Demo's film, Structure's),
    the API verdict band (after its last word), and a title row made for a tab that has none.
    A card-headed tab (Data) gets one set per card. `presses` empty: only the title."""
    if tab_id in TAB_TITLES and not _TABTITLE_OPEN.search(body):
        body = tab_title_row(TAB_TITLES[tab_id]) + body
    if not presses:
        return body
    if tab_id in CARD_HEADED_TABS and _CARD_TITLE.search(body):
        # A card that came with a ring of its own keeps it, and only it: the draw.io card's
        # redraws that one picture (`diagrams.card_rerun_html`), which is what a press on
        # that card means, and a second ring beside it would re-run the whole tab.
        return _CARD_TITLE.sub(lambda m: m.group(1) + (
            "" if body.startswith('<span class="tabre">', m.end()) else presses), body)
    lede = body.find('<p class="sub counts pilelede"')
    if lede >= 0:
        end = body.find("</p>", lede)
        if end >= 0:
            return body[:end] + presses + body[end:]
    title = _TABTITLE_OPEN.search(body)
    if title:
        end = body.find("</h2>", title.end())
        if end >= 0:
            return body[:end] + presses + body[end:]
    band = re.search(r'<div class="apiverdict\b[^>]*>', body)
    end = body.find("</div>", band.end()) if band else -1
    if end >= 0:            # the band holds spans only: its own `</div>` is the first one
        return body[:end] + presses + body[end:]
    return (f'<div class="adopthead"><h2 class="tabtitle">{html.escape(label)}{presses}'
            '</h2></div>' + body)


# The confirmation, in the page rather than in the browser.
#
# `window.confirm` was the first version of this and it is the wrong control for the job in
# three ways at once: it cannot say the price in the page's own voice, it cannot make the
# safe answer the default one, and it is the dialog every abusive site on the internet has
# trained readers to dismiss without reading. A reader who reflexively clicks OK on a
# native confirm has spent five dollars; the same reflex here lands on Cancel, because
# Cancel is what has focus when the panel opens and what Escape and a click on the backdrop
# both mean.
#
# In the markup of every copy of the page, hidden, like every other control here — the
# button that opens it is what the probe raises, so a static copy never reaches this.
RERUN_AI_CONFIRM = (
    '<div class="hrconfirm" id="hr-ai-confirm" hidden role="dialog" aria-modal="true"'
    ' aria-labelledby="hr-ai-confirm-t">'
    '<div class="hrconfirm-box">'
    '<p class="hrconfirm-t" id="hr-ai-confirm-t"><b>This one costs money.</b></p>'
    # A run already going is the first thing this panel says, before the price: a reader
    # about to spend needs to know that pressing may not even start anything of theirs.
    # Filled and raised by RERUN_JS off `/__run_status__`; absent from every static copy.
    '<p class="hrconfirm-busy" hidden></p>'
    '<p class="hrconfirm-b">AI redoes the requirements↔tests map — '
    '<b class="hrconfirm-price">about $5–$10 on Sonnet</b>. The current map is replaced.</p>'
    # The last real invoice, where the decision is made. An average is what a reader
    # budgets with; the figure that makes them believe it is what the last press actually
    # cost, and a tooltip they may never open is the wrong place for it.
    '<p class="hrconfirm-b hrconfirm-last" hidden></p>'
    '<p class="hrconfirm-b hrconfirm-alt">The plain ↺ is free.</p>'
    '<div class="hrconfirm-row">'
    '<button type="button" class="hrconfirm-no">Cancel</button>'
    '<button type="button" class="hrconfirm-yes">Spend it, rerun with AI</button>'
    '</div></div></div>')

# Under the masthead rather than inside it: the header is a block that never scrolls, and
# a log tail pinned to the top of the viewport for the rest of the read is a worse artifact
# than the failure it reports. Hidden until there is something to report, so the normal
# read never pays for it, and dismissible, because the reader decides when it is read.
RERUN_FAIL = ('<div class="rerunfail" id="hr-rerun-fail" hidden role="alert">'
              '<div class="rerunfail-head"><b>Rerun failed</b>'
              '<span class="rerunfail-why"></span>'
              '<button type="button" class="rerunfail-x" '
              'aria-label="Dismiss this report" data-tip="Dismiss">✕</button></div>'
              '<pre class="rerunfail-log"></pre></div>')

# And the other outcome, in the same row of the masthead, because a rerun had only ever
# been able to report the bad one.
#
# `run-steps.py` prints `N step(s) re-run, M unchanged and skipped — about N s saved` and
# says in its own comment that the line is "phrased for the status band on the served page
# rather than for this terminal". There was no such band: the page reloaded, the tab and
# the scroll came back exactly where they were, and the six seconds the press took read as
# a button that did nothing. On a page whose whole argument is that a fast rerun should be
# legible as a fast rerun, the one artifact saying so never left stdout.
#
# `role="status"`, not `alert`: nothing here needs interrupting, and a screen reader should
# hear it when it finishes what it is saying. It takes itself away after a few seconds —
# it is news about a press, and a press is over — which is the other difference from the
# failure beside it, where the reader decides when it is read.
RERUN_DONE = ('<div class="rerundone" id="hr-rerun-done" hidden role="status">'
              '<span class="rerundone-ico" aria-hidden="true">↺</span>'
              '<span class="rerundone-say"></span></div>')


# The run itself, while it runs. A press used to turn one glyph and put the producer's
# last line in a hover: on a fifty-second rebuild that is a page that looks exactly as it
# did, with a reader wondering whether to press again, and a refresh of the tab lost even
# the turning glyph. This band is the run's own progress: a bar, the step it is on, how
# many are left and roughly how long — and it comes back on a reload, because the server
# still knows which run is going and the page asks it on every load (`rerun.js`).
#
# The expectations are the last run's own timings, read out of `.steps-cache.json` at
# build time and carried on the band as data: `run-steps.py` prints `* <step> -> <tabs>`
# as it starts each one, so the page can tell which step it is on, and what it cannot
# tell from the log — how long the ones ahead will take — it estimates from what they
# took last time. A step with no history counts as a few seconds; the page build after
# the steps counts as `build`. Only static steps are listed: the paid run's model half is
# a wait the page names as such rather than pretends to measure.
PROGRESS_BUILD_SECONDS = 15.0
PROGRESS_STEP_DEFAULT = 5.0


def step_expectations(out_dir) -> dict:
    """`{step: seconds}` from the last run's cache, in the order the steps ran."""
    try:
        doc = json.loads((Path(out_dir) / ".steps-cache.json").read_text(encoding="utf-8"))
        steps = doc.get("steps") or {}
    except (OSError, ValueError, AttributeError):
        return {}
    out = {}
    for name, rec in steps.items():
        try:
            out[name] = round(float(rec.get("seconds") or 0), 1)
        except (TypeError, ValueError, AttributeError):
            out[name] = 0.0
    return out


def rerun_progress_html(expected: dict) -> str:
    data = {"steps": expected, "build": PROGRESS_BUILD_SECONDS,
            "default": PROGRESS_STEP_DEFAULT}
    return ('<div class="rerunprog" id="hr-rerun-progress" hidden role="status" '
            'aria-live="polite" data-expect="'
            + html.escape(json.dumps(data, separators=(",", ":")), quote=True) + '">'
            '<div class="rerunprog-row"><span class="rerunprog-ico" aria-hidden="true">↺</span>'
            '<span class="rerunprog-say">Rebuilding this page…</span>'
            '<span class="rerunprog-eta"></span></div>'
            '<div class="rerunprog-bar" aria-hidden="true"><i class="rerunprog-fill"></i></div>'
            '</div>')


def drawio_open_html(app_url: str, web_url: str = "") -> str:
    """The two ways to edit the drawing, as buttons of the same look as the commands.

    Under the picture and not inside it: a rendered diagram cannot show a cursor, so an
    invitation painted onto the map has to spell out in words that it is clickable.

    They are not the same offer, which is why both are named. **Edit on desktop** opens the
    file on disk, so an edit lands where the rerun command can pick it up. **Edit on web**
    opens a copy carried in the URL — nothing is uploaded, and nothing it saves reaches the
    repository either. It is the answer when draw.io is not installed on this machine.

    They are links (`a.dgm-edit`), wearing the look of a labelled command, and deliberately
    **not** `.cmd-copy` / `.cmd-run` inside a `.cmd`: the run script disables those inside
    `.rerun` while a command is going and pairs them by `.cmd`, and a link that only opens
    an editor is neither a copy nor a run.
    """
    def btn(href: str, word: str, tip: str, extra: str = "") -> str:
        return (f'<a class="dgm-edit" href="{html.escape(href, quote=True)}" '
                f'data-tip="{html.escape(tip, quote=True)}"{extra}>'
                f'<span class="cmd-lead">\u270f\ufe0f</span><span class="cmd-word">{word}</span></a>')

    out = []
    if app_url:
        out.append(btn(app_url, "Edit on desktop", "Open in the draw.io desktop app"))
    if web_url:
        out.append(btn(web_url, "Edit on web", "Open in draw.io on the web",
                       ' target="_blank" rel="noopener"'))
    return "".join(out)


# What a click-to-run offer says on a static page — where it stays visible and explains
# itself rather than being absent from one copy of the report and present in the other.
# `data-tip-served` is what the same button says where it actually works; the probe swaps
# them, so the two readings live next to each other here instead of in the script.
STATIC_RUN_TIP = "Static copy. Click 📋 Serve at the top to enable."


def reveal_html(reveal: dict | None, name: str, capital: bool = False) -> str:
    """"this diagram" as a handle on the file, rather than as a noun.

    The sentence already says *edit* it and *re-render* it; the one thing it says nothing
    about is where the thing actually is. And the two words that name it were sitting right
    there, unclickable, at the front of the line. So the subject of the sentence became the
    control: press it and the file is selected on disk, in the window the reader would have
    gone looking for it in.

    Not folded, and not paired with a command to read: revealing a file changes nothing and
    costs nothing to press, which is the one offer under this picture that needs no second
    click and no `reload`. Where it cannot run — a static copy, or a verdict written before
    the command was recorded — the two words are two words again, and the sentence reads
    exactly as it did before any of this.
    """
    word = "This diagram" if capital else "this diagram"
    if not reveal or not reveal.get("command") or not name:
        return word
    aid = declare_action(f"drawio-reveal:{name}", reveal["command"],
                         label=f"Show {name} on disk")
    where = reveal.get("in") or "the file manager"
    # Two words and a plain copy of them, and the same rule as every other offer on this
    # line picks: where nothing can run, the subject of the sentence is a noun again rather
    # than a control that explains why it does not work. There is no `run this` fallback
    # here because there is nothing to fall back to — `open -R` is not a step in anyone's
    # workflow, it is a shortcut for one, and a reader without a server has their own.
    return ('<span class="offer">'
            f'<button type="button" class="runhere" '
            f'data-action="{html.escape(aid, quote=True)}" '
            f'data-tip="{html.escape(STATIC_RUN_TIP, quote=True)}" '
            f'data-tip-served="Selects the file on disk, in {html.escape(where, quote=True)}"'
            f'>{word}</button>'
            f'<span class="plainword">{word}</span></span>')


#: The copy glyph's hover. Says what the click does and then the line it will put on the
#: clipboard, which is the only place a command appears on this page in full.
COPY_TIP = "Copy command to paste in terminal"


def command_html(cmd: str, action_id: str | None = None, *, tip: str = "",
                 running: str = "", label: str = "", run_face: str = "",
                 icon: str = "", play: bool = True) -> str:
    """The affordances of one shell command, beside the control that describes it.

    **The command itself is not printed.** It used to be, in a parenthesis, and it was the
    right instinct and the wrong artifact: a review page is prose and pictures, and a
    forty-to-two-hundred-character absolute path in the middle of a sentence is a wall the
    eye has to climb over on every read. The information was for the one reader in ten who
    wanted to paste it, charged to all ten, forever.

    So what is on screen is the *offer*, not its implementation: one glyph, and it is the
    one that is true here. Off disk, out of the zip and on GitHub Pages that is the
    clipboard, and the command lives in its hover — the only place the line appears in
    full on this page. Served, the probe raises the run glyph and takes the clipboard
    away, because the command has somewhere to go.

    Both are in the markup and only one is ever on screen, which is not the same as
    rendering both. They were both visible for a while and the pair asked the reader a
    question the page already knew the answer to — press this and it runs, press that and
    you get a line to run somewhere else, and neither glyph said which was which. A
    control whose whole job is to say *what this copy of the report can do* must not need
    a click to say it.

    `action_id` is what the server will be asked for: a manifest id, or one of the two
    server-owned verbs (`__rerun__`, `__rerun_ai__`, which `window.HR.can` answers off the
    probe rather than out of the manifest). No id means no run glyph and the clipboard
    stays, which is the honest rendering of a command the build did not declare — the line
    is real, and nothing here can run it.

    `label` is the action in words, and it is on the button rather than beside it. It used
    to be beside it: a grey pill reading *Update the report* and, after it, a separate
    glyph — two elements for one action, so a reader who pressed one had no way to know
    the other did the same thing, and the row under every diagram carried four controls
    for two offers. The words and the mark are one target now, which is also the answer to
    "what does this glyph belong to" without a hover.

    `run_face` replaces the play on the run half, for the verbs in the Demo tab's
    **Running app** row: Start begins something that then keeps running, Stop ends it,
    and a play triangle on both would say the same thing about two different things. Everywhere else the mark is the play, because everywhere else
    the offer is *do this here*.

    `play=False` drops the run half's mark altogether, for a labelled button whose lead
    glyph already says what it does — the draw.io card's *Revert changes*: "buttons do run
    stuff, so a play icon is irrelevant" (Victor, 7 Oct 2026). Mid-run the lead spins
    instead (`.cmd-run.has-word.running .cmd-lead`).

    Not `↻`. The circular arrow is the masthead's badge, where it means "this page can
    rebuild itself", and it is green because that is what `served` is coloured. On a row of
    actions it said *rerun* over verbs that are not reruns, and its green read as a passing
    check. The play is drawn in the page's action accent — the colour everything pressable
    here already wears.

    **One string, two surfaces.** When `action_id` names something the build declared, the
    command rendered is the register's, not the caller's: `data-copy`, `data-cmd` and the
    clipboard's hover all carry the bytes `serve-review.py` will hand to `sh -c`. The
    caller's `cmd` is a fallback for the undeclared case and a cross-check for the declared
    one. This is not tidiness — the aftermath band and the server used to compose the same
    refresh command separately and had drifted by an interpreter and a flag, so the line a
    reader pasted did something other than the button they could have pressed.
    """
    # The register is the source, and the caller's string is the fallback. Reading it here
    # rather than trusting what was passed is what makes "the line you copy is the line the
    # server runs" a property of the code instead of a habit: there is one author of that
    # string, and it is `declare_action`.
    entry = ACTIONS.get(action_id) if action_id else None
    if entry and entry.get("command"):
        cmd = entry["command"]
    quoted = html.escape(cmd, quote=True)
    # The command in the hover, on its own line after the sentence. This is the only place
    # it appears in full, so it is not truncated: a half-copied command in a tooltip is
    # worse than none, because the reader cannot tell which half they are looking at.
    copy_tip = html.escape(f"{COPY_TIP}:\n{cmd}", quote=True)
    # The word first and the mark after it, in that order on both faces. The label is what
    # the reader is looking for and the mark is the footnote saying what a press will do —
    # and the row this replaced already read that way, so nothing about where to look
    # changed when the second control went away.
    lead = f'<span class="cmd-lead">{icon}</span>' if icon and label else ""
    word = f'{lead}<span class="cmd-word">{html.escape(label)}</span>' if label else ""
    wordy = " has-word" if label else ""

    def face(glyph: str) -> str:
        if label and not glyph:
            return word
        return f'{word}<span class="cmd-ico">{glyph}</span>' if label else glyph

    # With a word on the button the word is the name; `aria-label` would replace it and
    # leave a screen reader saying "Copy command to paste in terminal" three times in a
    # row with nothing to tell the three apart.
    copy_aria = (f"{label} \u2014 {COPY_TIP.lower()}" if label else COPY_TIP)
    # `data-cmd` on both faces, and it is not for the browser: it is the handle the
    # guardrail reads. A test that walks a built page can ask every control what line it
    # stands for and compare it with the register the server runs out of, which is the only
    # way the two surfaces can be held equal after the fact rather than by inspection.
    out = [f'<span class="cmd">'
           f'<button type="button" class="copycmd cmd-copy{wordy}" data-copy="{quoted}" '
           f'data-cmd="{quoted}" '
           f'data-tip="{copy_tip}" aria-label="{html.escape(copy_aria, quote=True)}">'
           f'{face(CMD_COPY)}</button>']
    if action_id:
        # `runhere` because the page's existing handler runs a `runhere` with a
        # `data-action` through the action server — spinner, log tail and reload included.
        # `hidden` from the start and raised by the probe, like every other control here.
        #
        # One short sentence, and the command is not in it. It used to end with the whole
        # line, which put a two-hundred-character absolute path in a hover over a button
        # whose label already says what it does — and it is the *clipboard* whose hover a
        # reader opens to read a command, because that is the face that hands them one.
        # A labelled button needs no hover: its words are under the pointer (copy pass,
        # 3 Oct 2026 — "runs on the server serving this page" was mechanics).
        play_tip = html.escape(tip or ("" if label else "Run it"), quote=True)
        run_aria = f"{label} \u2014 run this command" if label else "Run this command"
        out.append(f'<button type="button" class="runhere cmd-run{wordy}" hidden '
                   f'data-action="{html.escape(action_id, quote=True)}" '
                   f'data-cmd="{quoted}" '
                   f'data-tip="{play_tip}"'
                   + (f' data-run-say="{html.escape(running, quote=True)}"' if running else "")
                   + f' aria-label="{html.escape(run_aria, quote=True)}">'
                   + f'{face(run_face or (CMD_PLAY if play or not label else ""))}</button>')
    out.append('</span>')
    return "".join(out)


def regenerate_html(redraw: dict | None, rerun: dict, rebuild: str,
                    name: str) -> tuple[str, str | None]:
    """The one way back: put the machine's own drawing there, and rebuild around it.

    There used to be two of these and they were the same offer to the reader. *Undo your
    edits* walked back to the newest committed drawing; *start over* restored the base and
    re-ran the repository's patch script, which draws what the code has and the map lacks.
    Two commands, two tooltips, two paragraphs of this docstring explaining that they land
    in different places — and every reader who pressed either was asking one question:
    **give me back the diagram the machine makes.** Only the second answers it. The first
    hands back a human's layout from an earlier commit, which is a different drawing and
    is not "generated" in any sense the reader meant.

    So the one that runs the generator stays, and it is named after what it gives back
    rather than after the gesture that gets you there: *Revert the diagram*, not *start
    over*, which is a direction and not a destination. It was *Regenerate the diagram* for
    a while, and the word was the problem: on a row whose other button reads *Update the
    report*, and beside an aftermath band whose button reads *Regenerate the report*, a
    third *Regenerate* made three controls sound like three doses of one thing. *Revert*
    is what a reader is actually asking for here — put back what was there before they
    drew on it — and it is the one word on this row that admits the layout goes away.

    It is destructive — the hand-drawn layout goes — so it banks the work first. The
    command that went away was the survivable one (`git stash push` before the checkout),
    and losing that property along with it would be a bad trade for a simpler line, so the
    stash comes across. `git stash push -- <path>` exits 0 with "No local changes to save"
    when there is nothing to bank, so it costs a clean tree nothing.

    Returns `(offer, action_id)`; `("", None)` where the repository declared no patch
    script. That script is the reviewed project's, not this tool's — guessing it from a
    naming convention and running it on a reader's click is not a trade worth making.
    """
    if not redraw or not redraw.get("command"):
        return "", None
    # Five stages, and the middle three are the reason this is one offer and not three:
    # banking the layout, restoring the base drawing and redrawing it all change the file
    # on disk, and the picture in this page is an inlined SVG that only `drawio-diff.py`
    # rewrites. Stopping before the last two would leave the reader looking at their own
    # layout with a green tick beside it.
    stash = ""
    if redraw.get("diagram"):
        stash = (f"git stash push -m {shlex.quote(f'human-review: layout of {name}')} -- "
                 f"{shlex.quote(redraw['diagram'])} && ")
    line = (f'cd {shlex.quote(redraw["cwd"])} \\\n  && {stash}{redraw["command"]} \\\n'
            f'  && {rerun["command"]} \\\n  && {rebuild}')
    aid = None
    if name:
        aid = declare_action(f"drawio-redraw:{name}", line, reload=True,
                             label=f"Regenerate {name} from the repository's own script")
    # One short sentence on the play, and it spends its second half on the one thing a
    # label cannot carry: that this throws the reader's layout away, and where it goes. The
    # rest of what the old three-line tooltip said — which file, which base, what the
    # script draws — is what the *command* says, and the command is one hover away on the
    # clipboard face of the same control.
    served = "Your layout is saved in git stash"
    # ↺, not ⇤: it is the one way back on the card now that *Load changes* is gone, and the
    # card's own green ring already carries the forward half (Victor, 7 Oct 2026).
    return (command_html(line, aid, label="Revert changes", icon="\u21ba", tip=served,
                         running="Putting automation's drawing back…", play=False), aid)


def rerun_html(rerun: dict | None, rebuild: str, name: str = "",
               app_url: str = "", web_url: str = "", redraw: dict | None = None,
               revert: dict | None = None, reveal: dict | None = None,
               tested_against: str = "", tested_against_path: str = "") -> tuple[str, str]:
    """The draw.io card's actions, and the line under its header: `(actions, under)`.

    The picture is inlined into the HTML, and it has to be: the boxes are links into the
    classes they name and the to-do note is a link into draw.io, and an SVG loaded through
    `<img src>` renders those as decoration. So the file on disk and the picture in the page
    are two artefacts, and reloading the browser only ever refreshes the second one.

    **`actions` go in the card's header**, right-aligned before the file name — Edit on
    desktop, Edit on web, Revert changes. They are action buttons, and the header is where
    the card's other press already sits (Victor, 7 Oct 2026). **`under`** is what stays
    under the header: the guardrail sentence, and the status line a running Revert writes.

    **No *Load changes*.** It ran `drawio-diff.py` and rebuilt the page, which is exactly
    what the green ring at the top of the card does — the Data tab's rerun is
    `refresh-report.py --steps diagrams`, the step that runs the same `drawio-diff.py` from
    the same config, and it is fingerprinted on the dirty `*.drawio.png` so an edit is never
    a cache hit. Two presses for one thing on one card asked the reader which to trust
    ("why does it exist when I can press the green rerun?", Victor, 7 Oct 2026).

    **`revert` is accepted and ignored**, so a verdict written by an older `drawio-diff.py`
    still builds. See `regenerate_html` for why there is one way back rather than two.

    `rerun` is what `drawio-diff.py` recorded about its own invocation; `rebuild` is how
    this build was started. Neither is reconstructed here — a guessed command that does
    not work is worse than no command, because it is tried first.
    """
    edit = drawio_open_html(app_url, web_url)
    # One fact, and only when the project named a guardrail: the file's own name is the
    # card's title right above, so the sentence does not repeat it ("This diagram is…").
    # With `tested_against_path` the label is the way into the scanned package in VS Code.
    sentence = ""
    if tested_against:
        label = html.escape(tested_against)
        if tested_against_path:
            label = (f'<a href="vscode://file/{html.escape(tested_against_path, quote=True)}">'
                     f'{label}</a>')
        sentence = f"Unit-tested against the {label}."
    where = f'<p class="dgm-open">{sentence}</p>' if sentence else ""
    again = ""
    if rerun and rerun.get("command"):
        again, _ = regenerate_html(redraw, rerun, rebuild, name)
    acts = f'<div class="rerun-acts">{edit}{again}</div>' if (edit or again) else ""
    # The status line, empty until Revert is running: that command re-renders a diagram
    # and rebuilds the page, which is seconds of nothing in front of a control that gave
    # no sign it had been pressed. What goes in it is the command's own last line, polled
    # from `/__run_status__`. Only where there is something to run.
    status = ('<p class="runstatus" hidden role="status" aria-live="polite">'
              '<b class="rs-phase"></b><span class="rs-tail"></span></p>') if again else ""
    under = f'<div class="rerun">{where}{status}</div>' if (where or status) else ""
    return acts, under


def _app_anchor(href: str) -> str:
    """The opening tag for a link into the running app.

    A root-relative href is a *path into whatever instance is up right now*, and the port
    that instance got is not knowable when this page is built — the host picks it, so that
    several branches can be running at once. So the path is kept verbatim in `data-app` and
    the `href` is only ever a best guess, rewritten by the script once a base URL is known.
    An absolute href is left exactly as written: it names a specific server on purpose."""
    if not href.startswith("/"):
        return f'<a href="{html.escape(href)}">'
    return f'<a data-app="{html.escape(href)}" href="{html.escape(href)}">'


#: What a Seed button says while nothing answers. It is on screen, greyed, and this is why.
SEED_OFFLINE_TIP = "Start the app first"


def fixtures_row_html(fixtures: list | None, can_reset: bool) -> str:
    """The "DB Fixture:" row under the Running app one: every fixture the project has, each
    as its colour dot, its name, a 👁 on its data (put there by `dataset-view.js`, from
    rows computed at build time, so it works with the app down) and a **Seed** button that
    resets the database to it.

    The list is the build's — `demo_fixtures`, read off `db/fixtures/*.sql` — and the row
    is on screen whatever the app is doing (Victor, 8 Oct 2026). It used to be buttons the
    *running* environment listed, inside the Running app row: down, they all left and the
    seed's 👁 hung alone after Start; and an instance started from an older image listed
    only Default while green.sql sat in the repo. So the names never move now; only the
    Seed buttons do, from greyed ("Start the app first") to live, as APP_ENV_JS hears the
    app answer. Seed is opt-in on the environment's `reset` endpoint, like every verb.

    `fixtures` is `[(name, colour), …]` with the seed as `""`; None means "the seed only",
    which is all a page knows that was rendered without a project to read."""
    items = list(fixtures) if fixtures else [("", "#8b929c")]
    if len(items) < 2 and not can_reset:
        # One Default with nothing to do to it is a row that says nothing.
        return ""

    def item(name: str, colour: str) -> str:
        label = name or "Default"
        seed = (f'<button type="button" class="appenv-reset" data-fixture="{html.escape(name)}"'
                f' aria-disabled="true" aria-label="Seed the DB with {html.escape(label)}"'
                f' data-tip="{SEED_OFFLINE_TIP}">Seed</button>') if can_reset else ""
        return (f'<span class="appenv-fx" data-fixture="{html.escape(name)}">'
                f'<span class="fx-dot" aria-hidden="true" style="--fx:{html.escape(colour)}">'
                f'</span><span class="appenv-fx-name">{html.escape(label)}</span>{seed}</span>')

    return ('<div class="appenv-fixtures" role="group" aria-label="DB fixtures">'
            '<span class="appenv-fixtures-to" data-tip="The datasets the demo DB can be '
            'reset to: the seed, or the seed plus a fixture">DB Fixture:</span>'
            + "".join(item(n, c) for n, c in items) + '</div>')


def runtime_html(rt, tail: str = "", fixtures: list | None = None) -> str:
    """The app the walkthrough was filmed against: start it, open it, stop it, reset it.

    This page is a file on disk that outlives the branch it describes, so it cannot hold a
    live URL: by the time anyone opens it the environment is long gone, and the next one
    will come up on a different port. What it *can* hold is a way to bring the environment
    back \u2014 and there are two of those, which is the whole shape of this row.

    **One line, and the click is what differs.** It was two: a row of word buttons that
    only did anything on a served page, and under it a second row \u2014 `START \u21bb`,
    `STOP \u21bb`, `WHERE \u21bb` \u2014 carrying the same three commands as clipboards. Six
    controls for three offers, and the lower row wore the *rerun* glyph, so a page that was
    being served still looked like it was handing out lines to paste somewhere else. Worse,
    the two rows disagreed about which copy of the report the reader was holding: the verbs
    vanished off disk and the commands stayed, which read as the row breaking rather than
    as it telling the truth.

    Now each verb is one control (`command_html` with a word on it), and it is the one
    control the page's whole command vocabulary is built on:

      * **served**, it wears its own mark \u2014 a green `\u25b6` on Start, a red `\u25a0` on Stop,
        \u2014 and a click runs the command through the review server;
      * **off disk**, both wear the clipboard, hover `Copy command to paste in
        terminal` with the line under it, and a click copies. No play glyph anywhere,
        because nothing here can play.

    Neither is `\u21bb`: the marks this page teaches everywhere else mean *this
    comes round again*, and starting an app is not a rerun of anything.

    Which verbs are on screen is the row's own state and lives in APP_ENV_JS: served, it
    shows Start while nothing answers, and Stop once something does. Off disk it shows
    both, because the reader is going to paste one of them into a terminal
    and which one they need is their business.

    The state leads the row \u2014 `Offline`, or the address as a link, port and all \u2014
    because it is the subject of every verb after it.

    Every control except the clipboards is opt-in on something the environment actually
    provides \u2014 `stop`, `urlCommand`, `reset` \u2014 because a button that always fails is
    worse than no button, and none of these can be derived from `command` by string
    surgery without working for the one host this was written against and failing silently
    on the next.
    """
    if not rt:
        return ""
    # `tail` is what the section adds at the far end of the row (the Demo tab's voice switch):
    # one row of controls over the player, not a second one under it.
    cmd = rt.get("command", "")
    fallback = rt.get("base", "")

    # The state first, because it is the subject of everything after it: "Offline", and
    # then the one verb that changes that \u2014 or the address, and then the two verbs that
    # act on what is answering there. Exactly one of the pill and the link is ever shown.
    at = ('<span class="appenv-at">'
          '<span class="appenv-state" data-state="unknown">checking\u2026</span>'
          '<a class="appenv-url" target="_blank" rel="noopener" hidden></a></span>')

    if cmd:
        declare_action("demo-env", cmd, scrape="url",
                       label="Start the environment the walkthrough was filmed against")
    if rt.get("stop"):
        declare_action("demo-env-stop", rt["stop"],
                       label="Stop the environment the walkthrough was filmed against")
    # Optional and never guessed — run by APP_ENV_JS, never offered as a button. Turning `\u2026 up --ref abc` into `\u2026 url --ref abc` by
    # string surgery would work for the one host this was written against and fail
    # silently on the next, at probe time, where nobody would see it fail.
    if rt.get("urlCommand"):
        declare_action("demo-env-url", rt["urlCommand"], scrape="url",
                       label="Ask the host where the environment is already answering")
    if rt.get("drive"):
        declare_action("cue-drive", rt["drive"], params={"n": "int", "base": "url"},
                       label="Drive the app to one caption of the walkthrough")

    # One control per verb, wrapped in a span that carries the verb's name. The wrapper is
    # what APP_ENV_JS hides when the verb does not apply, and it has to be a *wrapper*:
    # the two buttons inside it are already owned by SERVER_JS, which raises one and hides
    # the other per action. Two owners of `hidden` on the same element is how a control
    # ends up flickering between two truths.
    #
    # `hidden` from the start, like everything else in this row \u2014 a verb drawn live that
    # turns out not to apply has already been clicked by the time the probe corrects it.
    # APP_ENV_JS raises the three it wants in its first synchronous pass, before the probe
    # has answered anything, so a static page shows all three with no flicker.
    verbs = []
    if cmd:
        verbs.append(("start", command_html(
            cmd, "demo-env", label="Start App in Docker", run_face=CMD_PLAY,
            running="Starting the app\u2026")))
    if rt.get("stop"):
        verbs.append(("stop", command_html(
            rt["stop"], "demo-env-stop", label="Stop", run_face=CMD_STOP,
            running="Stopping\u2026")))
    # `urlCommand` has no control of its own. It was a Where button, and a reader could not
    # tell what it would do; served, APP_ENV_JS now runs it by itself when nothing answers
    # at the remembered address, and off disk nobody needs a line to paste to find out.
    controls = "".join(f'<span class="appenv-act appenv-{verb}" hidden>{box}</span>'
                       for verb, box in verbs)

    # The fixtures get a row of their own under this one (`fixtures_row_html`).
    row = fixtures_row_html(fixtures, bool(rt.get("reset")))

    return (f'<div class="appenv" data-fallback="{html.escape(fallback)}"'
            f'{f' data-reset="{html.escape(rt["reset"])}"' if rt.get("reset") else ""}'
            f'{f' data-drive="{html.escape(rt["drive"])}"' if rt.get("drive") else ""}>'
            '<div class="appenv-run"><span class="appenv-title">Running app</span>'
            + at + controls + tail + '</div>' + row + '</div>')
