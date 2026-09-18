"""Commands the page offers: copy, run, rerun, regenerate, reveal."""
from __future__ import annotations

import html
import shlex

from .actions import declare_action

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
CMD_RUN = "\u21BB"        # ↻

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
              'aria-label="Served by the review server \u2014 rerun and rebuild this page" '
              'data-tip="Served by the review server: commands run from this page and '
              'recordings play in it. Click to re-derive the evidence and rebuild the '
              'page: diagrams, complexity, the REST contract, the logging scan, the test '
              f'manifest. Not the findings, and not the film. Free.">'
              f'<span class="rr-ico">{CMD_RUN}</span></button>')

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
                 'data-tip="costs money: ~$5 on Sonnet. Rewrites the requirements↔tests '
                 'matrix and the per-test catalogue with a model, then re-derives the '
                 'evidence and rebuilds the page.">'
                 # The free one's mark, a plus, then the two things this one adds to it:
                 # a model, and money leaving. The `+` is the whole sentence — this chip is
                 # the one beside it *and* something more — and without it the three marks
                 # ran together as one picture nobody could take apart. No words, because
                 # the sentence that matters here is the price, and the price is in the
                 # hover and again in the dialog: a label reading `Rerun + AI` said neither,
                 # and cost the masthead two words to say `rerun` a second time.
                 f'<span class="rr-ico">{CMD_RUN}</span>'
                 '<span class="rr-plus">+</span>'
                 '\U0001F916\U0001F4B8</button>')

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
    '<p class="hrconfirm-b">Rerun&nbsp;+&nbsp;AI rewrites the requirements↔tests matrix '
    'and the per-test catalogue by asking a model — <b>about $5 on Sonnet</b> — and then '
    're-derives the evidence and rebuilds the page. The matrix you are looking at is '
    'replaced, not confirmed: a second pass over the same diff words and ranks it '
    'differently. The copy being replaced is kept in '
    '<code>.human-review/.model-prev/</code>.</p>'
    '<p class="hrconfirm-b hrconfirm-alt">Plain <b>Rerun</b> does everything except the '
    'model half, and costs nothing.</p>'
    '<div class="hrconfirm-row">'
    '<button type="button" class="hrconfirm-no" data-tip="Nothing is spent">Cancel</button>'
    '<button type="button" class="hrconfirm-yes" '
    'data-tip="Runs the model step, then rebuilds">Spend it, rerun with AI</button>'
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


def drawio_open_html(app_url: str, web_url: str = "") -> str:
    """The two ways to edit the drawing, as links.

    Under the picture and not inside it: a rendered diagram cannot show a cursor, so an
    invitation painted onto the map has to spell out in words that it is clickable — and
    then it is a sentence about tooling sitting on the drawing, re-read every time the
    reader looks at the boxes. In HTML it is just a link, and it can afford to be two.

    They are not the same offer, which is why both are named rather than one being "the"
    link. The **App** opens the file on disk, so an edit lands where the rerun command
    can pick it up. The **Web** editor opens a copy carried in the URL — nothing is
    uploaded, and nothing it saves reaches the repository either. It is the answer when
    draw.io is not installed on this machine, and the reader can tell which is which
    before clicking rather than after.
    """
    links = []
    if app_url:
        links.append(f'<a href="{html.escape(app_url, quote=True)}">App ↗</a>')
    if web_url:
        links.append(f'<a href="{html.escape(web_url, quote=True)}" '
                     'target="_blank" rel="noopener">Web ↗</a>')
    # The product is named once and the two editors are named after it — `draw.io App or
    # draw.io Web` said the brand twice in six words, which is the half of the phrase that
    # carries no information: the choice the reader is making is App or Web.
    return "draw.io " + " or ".join(links) if links else ""


# What a click-to-run offer says on a static page — where it stays visible and explains
# itself rather than being absent from one copy of the report and present in the other.
# `data-tip-served` is what the same button says where it actually works; the probe swaps
# them, so the two readings live next to each other here instead of in the script.
STATIC_RUN_TIP = ("This copy of the report is static, so nothing here can run: serve the "
                  "page — the static badge at the top copies the line that does — and "
                  "this button does the job.")


def reveal_html(reveal: dict | None, name: str) -> str:
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
    if not reveal or not reveal.get("command") or not name:
        return "this diagram"
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
            '>this diagram</button>'
            '<span class="plainword">this diagram</span></span>')


#: What a static click does, appended to the static tooltip of any offer that carries its
#: command. A sentence and not a word, because "copies it" leaves out the half a reader
#: needs — that the page cannot run it here, and that the copy is therefore the offer.
STATIC_COPIES = ("This copy of the report cannot run it, so clicking here copies the "
                 "command instead.")

#: The copy glyph's hover. Says what the click does and then the line it will put on the
#: clipboard, which is the only place a command appears on this page in full.
COPY_TIP = "Copy command to paste in terminal"


def command_html(cmd: str, action_id: str | None = None, *, tip: str = "",
                 running: str = "") -> str:
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
    """
    quoted = html.escape(cmd, quote=True)
    # The command in the hover, on its own line after the sentence. This is the only place
    # it appears in full, so it is not truncated: a half-copied command in a tooltip is
    # worse than none, because the reader cannot tell which half they are looking at.
    copy_tip = html.escape(f"{COPY_TIP}:\n{cmd}", quote=True)
    out = [f'<span class="cmd">'
           f'<button type="button" class="copycmd cmd-copy" data-copy="{quoted}" '
           f'data-tip="{copy_tip}" aria-label="{html.escape(COPY_TIP, quote=True)}">'
           f'{CMD_COPY}</button>']
    if action_id:
        # `runhere` because the page's existing handler runs a `runhere` with a
        # `data-action` through the action server — spinner, log tail and reload included.
        # `hidden` from the start and raised by the probe, like every other control here.
        play_tip = html.escape(
            ((tip.rstrip(".") + ". ") if tip else "")
            + f"Runs it through the server serving this page:\n{cmd}", quote=True)
        out.append('<button type="button" class="runhere cmd-run" hidden '
                   f'data-action="{html.escape(action_id, quote=True)}" '
                   f'data-tip="{play_tip}"'
                   + (f' data-run-say="{html.escape(running, quote=True)}"' if running else "")
                   + f' aria-label="Run this command">{CMD_RUN}</button>')
    out.append('</span>')
    return "".join(out)


def offer_words_html(label: str, action_id: str, static_tip: str, served_tip: str,
                     running: str = "", cmd: str = "", pill: bool = False) -> str:
    """The words of an offer — one control, in both copies of the report.

    This replaces the pair `_run_or_read` used to render. That function put *two* controls
    in the markup and let CSS choose: `click here` served, `run this` off disk, the second
    one opening a fold with the command in it. The whole arrangement existed to answer "the
    command is only useful in one of the two copies", and it answered it by making the same
    control read differently in each — so a reader could not learn the page, and "what does
    this button run" had no answer in the copy where the button worked.

    One control, and the *click* differs rather than the words:

      * **served**, it runs the command through the review server;
      * **off disk**, it copies it, with a `copied` toast. That is the only thing that copy
        of the report can do with the command, so it is what the click does. A control
        whose whole answer is a sentence explaining why it did nothing is a control readers
        learn to stop pressing.

    The copy glyph beside it (`command_html`) is what keeps that from being a magic trick:
    it is the signal that a click here copies something, and its hover carries the line.

    `pill` dresses it as a button rather than as an underlined word. For the offers in the
    aftermath band, which are the page's answer to "somebody changed the code after the
    review was written" — a fact in a red band, with two things to do about it. Inline
    links inside that sentence were the same weight as the prose around them and were read
    as part of it; the two things to do are the point of the band.
    """
    tip = f"{static_tip} {STATIC_COPIES}" if cmd else static_tip
    cls = "runhere offer-words" + (" offer-pill" if pill else "")
    return (f'<button type="button" class="{cls}" '
            f'data-action="{html.escape(action_id, quote=True)}" '
            + (f'data-copy="{html.escape(cmd, quote=True)}" ' if cmd else "")
            + f'data-tip="{html.escape(tip, quote=True)}" '
            f'data-tip-served="{html.escape(served_tip, quote=True)}"'
            + (f' data-run-say="{html.escape(running, quote=True)}"' if running else "")
            + f'>{html.escape(label)}</button>')


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

    So the one that runs the generator stays, and it is named after what it produces rather
    than after the gesture that gets you there: *Regenerate the diagram*, not *start over*,
    which is a direction and not a destination.

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
    base = redraw.get("base") or "the base branch"
    tip = (f"Puts automation's own drawing back: restores the file to {base} and runs the "
           "repository's script over it, which draws what the code has and the map lacks "
           "— in red, as a to-do. Your layout is banked with `git stash`, not binned.")
    served = ("Runs it here and reloads, with automation's drawing back. Your layout goes "
              "to the git stash.")
    return (offer_words_html("Regenerate the diagram", aid or "", tip, served,
                             "Putting automation's drawing back…", cmd=line, pill=True)
            + command_html(line, aid, tip=served,
                           running="Putting automation's drawing back…"), aid)


def rerun_html(rerun: dict | None, rebuild: str, name: str = "",
               app_url: str = "", web_url: str = "", redraw: dict | None = None,
               revert: dict | None = None, reveal: dict | None = None) -> str:
    """Under the drawing: where to edit it, and the two things to do about it afterwards.

    The command is not a convenience. The picture above is inlined into the HTML, and it
    has to be: the boxes are links into the classes they name and the to-do note is a link
    into draw.io, and an SVG loaded through `<img src>` renders those as decoration — the
    reader can see them and cannot click them. So the file on disk and the picture in the
    page are two artefacts, and reloading the browser only ever refreshes the second one.
    That is a thing the page owes the reader an answer to, at the moment they need it, in
    the form of something they can press.

    **A sentence, then two buttons.** It was one sentence with everything inside it, and
    by the time the offers had grown their glyphs it carried seven underlined runs of text
    — `this diagram`, `App`, `Web`, `click here`, `undo your edits`, `start over` — which
    read as a wall of links with no rank. Underlining is for the actions now and the
    actions are pills; the places to go (the file, the two editors) are plain links that
    underline on hover. One kind of emphasis, one meaning.

    **`revert` is accepted and ignored**, so a verdict written by an older `drawio-diff.py`
    still builds. See `regenerate_html` for why there is one way back rather than two.

    `rerun` is what `drawio-diff.py` recorded about its own invocation; `rebuild` is how
    this build was started. Neither is reconstructed here — a guessed command that does
    not work is worse than no command, because it is tried first.
    """
    edit = drawio_open_html(app_url, web_url)
    it = reveal_html(reveal, name)
    if not rerun or not rerun.get("command"):
        return f'<p class="dgm-open">Edit {it} in {edit}</p>' if edit else ""
    line = f'cd {shlex.quote(rerun["cwd"])} \\\n  && {rerun["command"]} \\\n  && {rebuild}'
    # Per diagram, because a page can carry several and each one reruns its own. The id
    # is the diagram's name for the same reason every other handle on this page is: so a
    # button that has been on screen since the last build cannot end up running the
    # command belonging to a different picture.
    #
    # `reload`, because the last stage of this line rewrites the very file the browser is
    # displaying. Leaving the reader on the old bytes with a green tick beside them would
    # be the worst possible outcome: the page would look like it had picked the edit up.
    aid = None
    if name:
        aid = declare_action(f"drawio:{name}", line, reload=True,
                             label=f"Re-render {name} and rebuild this page")
    served = "Runs it here, then reloads with the new picture"
    again, _ = regenerate_html(redraw, rerun, rebuild, name)
    # The status line, under the buttons and empty until something is running. This is the
    # whole of the answer to the complaint that produced it: the command behind *Update the
    # report* re-renders a diagram and rebuilds the page, which is seconds of nothing
    # whatever, in front of a control that gave no sign it had been pressed. A reader with
    # no feedback does not wait patiently — they press it again, and then they stop
    # believing the page. What goes in it is the command's own last line, polled from
    # `/__run_status__`, so it says what is actually happening rather than a guess.
    #
    # In the markup of both copies of the report. Off disk nothing fills it, which costs
    # a hidden empty paragraph.
    # The sentence, when there is somewhere to send them. With no editor link declared
    # there is no "in draw.io App ↗" to write, and `Edit this diagram in .` is worse than
    # silence — but the file itself is still worth naming if the verdict recorded how to
    # reveal it.
    where = (f'<p class="dgm-open">Edit {it} in {edit}.</p>' if edit
             else (f'<p class="dgm-open">Edit {it}.</p>' if reveal else ""))
    return ('<div class="rerun">' + where
            + '<div class="rerun-acts">'
            + offer_words_html("Update the report", aid or "", STATIC_RUN_TIP, served,
                               "Re-rendering the diagram…", cmd=line, pill=True)
            + command_html(line, aid, tip=served, running="Re-rendering the diagram…")
            + again
            + '</div>'
            + '<p class="runstatus" hidden role="status" aria-live="polite">'
              '<b class="rs-phase"></b><span class="rs-tail"></span></p>'
            + '</div>')


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


def runtime_html(rt) -> str:
    """The app the walkthrough was filmed against: start it, open it, stop it, reset it.

    This page is a file on disk that outlives the branch it describes, so it cannot hold a
    live URL: by the time anyone opens it the environment is long gone, and the next one
    will come up on a different port. What it *can* hold is a way to bring the environment
    back \u2014 and there are two of those, which is the whole shape of this bar.

    Served, the row is verbs, and it keeps its own state: nothing answering, so `Start`;
    something answering, so the address it answers at \u2014 a link, into a new tab, port and
    all \u2014 then `Stop` and `Reset DB`. The page asks its own server to run the commands
    the build declared and scrapes the URL out of what `Start` printed, so there is nothing
    for the reader to paste anywhere.

    Off disk none of that can happen: no process here runs a command, and Reset has
    nothing to reset until one does. So the row carries no verbs at all and the terminal
    command takes their place. Both are in the markup either way \u2014 the same file is
    opened both ways and only the script knows which \u2014 and CSS hides the half that is
    lying. One offer, in the register that copy of the report can actually honour, rather
    than the same offer twice: greyed buttons above the command that replaces them.

    Every control except the command is opt-in on something the environment actually
    provides \u2014 `stop`, `reset` \u2014 because a button that always fails is worse than no
    button, and none of these can be derived from `command` by string surgery without
    working for the one host this was written against and failing silently on the next.
    """
    if not rt:
        return ""
    cmd = rt.get("command", "")
    fallback = rt.get("base", "")

    def btn(cls: str, face: str, tip: str) -> str:
        # `hidden` from the start, and raised by the script once the probe has answered:
        # a verb drawn live that turns out not to apply has already been clicked by then.
        # aria-disabled rides along so the guard in the script survives a stylesheet that
        # never loaded, where `hidden` alone would have left a live button behind.
        return (f'<button type="button" class="{cls}" hidden aria-disabled="true"'
                f' data-tip="{html.escape(tip, quote=True)}">{face}</button>')

    # The state first, because it is the subject of everything after it: "Offline", and
    # then the one verb that changes that — or the address, and then the two verbs that
    # act on what is answering there. Exactly one of the pill and the link is ever shown.
    at = ('<span class="appenv-at">'
          '<span class="appenv-state" data-state="unknown">checking\u2026</span>'
          '<a class="appenv-url" target="_blank" rel="noopener" hidden></a></span>')

    controls = btn("appenv-start", "Start",
                   "Start the app and fill the address in from what it prints") if cmd else ""
    if rt.get("stop"):
        controls += btn("appenv-stop", "Stop", "Stop the app and free its port")
    if rt.get("reset"):
        controls += btn("appenv-reset", "Reset DB", "Put the demo data back to its seed")

    if cmd:
        declare_action("demo-env", cmd, scrape="url",
                       label="Start the environment the walkthrough was filmed against")
    if rt.get("stop"):
        declare_action("demo-env-stop", rt["stop"],
                       label="Stop the environment the walkthrough was filmed against")
    # Optional and never guessed. Turning `… up --ref abc` into `… url --ref abc` by
    # string surgery would work for the one host this was written against and fail
    # silently on the next, at probe time, where nobody would see it fail. Declared or
    # absent — and absent costs only the re-discovery of a base the browser had already
    # remembered in localStorage.
    if rt.get("urlCommand"):
        declare_action("demo-env-url", rt["urlCommand"], scrape="url",
                       label="Ask the host where the environment is already answering")
    if rt.get("drive"):
        declare_action("cue-drive", rt["drive"], params={"n": "int", "base": "url"},
                       label="Drive the app to one caption of the walkthrough")

    # The commands this row is made of, as a clipboard each — and, served, a play each.
    #
    # In *both* copies of the report, which is the change. The stylesheet used to print the
    # `up` line off disk and hide it the moment the probe answered, so "what does this
    # button actually run" had no answer in the copy where the button worked. Now the
    # affordance is there either way, and the line itself is in the glyph's hover rather
    # than in the row: a `cd … && ./start-docker.sh up --ref abc123` printed in a bar of
    # four controls was the widest thing in the Demo tab and was read once.
    #
    # All three, not just `up`. `stop` and `url` were declared for the buttons and never
    # offered to anybody, which meant the one reader who needed to know how the host is
    # asked where the stack is answering had to go and read the manifest.
    rows = []
    if cmd:
        rows.append(("start", command_html(
            cmd, "demo-env", tip="Starts the app and fills the address in from what it "
            "prints", running="Starting the app…")))
    if rt.get("stop"):
        rows.append(("stop", command_html(rt["stop"], "demo-env-stop",
                                          tip="Stops the app and frees its port",
                                          running="Stopping…")))
    if rt.get("urlCommand"):
        rows.append(("where", command_html(
            rt["urlCommand"], "demo-env-url",
            tip="Asks the host where the app is already answering",
            running="Asking the host…")))
    manual = ('<p class="appenv-manual">'
              + "".join(f'<span class="appenv-cmd">'
                        f'<span class="appenv-verb">{face}</span>{box}</span>'
                        for face, box in rows)
              + '</p>') if rows else ""

    return (f'<div class="appenv" data-fallback="{html.escape(fallback)}"'
            f'{f' data-reset="{html.escape(rt["reset"])}"' if rt.get("reset") else ""}'
            f'{f' data-drive="{html.escape(rt["drive"])}"' if rt.get("drive") else ""}>'
            '<div class="appenv-run"><span class="appenv-title">Deployed app</span>'
            + at + controls + '</div>' + manual + '</div>')
