#!/usr/bin/env python3
"""The Review tab's *Publish on GitHub*, in a headless browser, against a fake server.

PR #51's press showed the dry run's `gh api` calls "done on GitHub in my name", then an
alert() quoting a command line, and left the button on "Posting…". What the reader gets
now: one question ("Post 2 comments on GitHub PR #51 as @victorrentea?"), the comments a
click away, and the outcome in words beside the button, with Retry when it stopped
half-way. `window.HR` is stubbed: nothing here runs a command or reaches GitHub.

Run with:  python3 -m pytest test_pr_push_dialog.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("hr_build_pp", HERE / "build-review-html.py")
build = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = build
_spec.loader.exec_module(build)
_ppc_spec = importlib.util.spec_from_file_location("hr_ppc_dialog", HERE / "push-pr-comments.py")
ppc = importlib.util.module_from_spec(_ppc_spec)
sys.modules[_ppc_spec.name] = ppc
_ppc_spec.loader.exec_module(ppc)

PREVIEW = {"pr": 51, "repo": "o/r", "url": "u", "user": "victorrentea", "total": 43,
           "already": 41, "calls": 1, "items": [
               {"id": "I:a", "fate": "new", "where": "src/A.java:12", "pile": "ignored",
                "title": "A", "text": "🔴 **Open issue** — keep the vet"},
               {"id": "A:b", "fate": "update", "where": "src/B.java:3-5", "pile": "assumption",
                "title": "B", "text": "💭 **Assumption** — none means none"}]}
PARTIAL = {"status": "partial", "expected": 43, "present": 30,
           "message": "GitHub timed out — 30 of 43 posted; click Retry to post the remaining 13."}


TITLES = ["Keep the <code>vet</code>", "Don&#x27;t &quot;guess&quot; the owner", "Not posted"]
PAYLOAD_TEXT = '{"comments": [{"title": "Keep the vet"}]}\n'


def page_html(answers: dict, editor: bool = False) -> str:
    spec = {"_prPush": {"count": 43, "posted": 0, "pushedAt": None,
                        "counts": {"fixed": 25, "ignored": 14, "assumption": 4}}}
    stub = """<script>
      window.__runs = []; window.__alerts = 0; window.__opened = [];
      window.alert = function () { __alerts++; };
      window.open = function (u) { __opened.push(u); };
      window.__answers = %s;
      window.HR = {onready: function (f) { f({token: 't', openPr: true}); },
        can: function () { return true; },
        keepPlace: function () {},
        run: function (id) { __runs.push(id); return Promise.resolve(__answers[id]); }};
    </script>""" % json.dumps(answers)
    items = "".join(f'<li><span class="f-title">{t}</span><p>body</p></li>' for t in TITLES)
    return ('<!doctype html><html data-hr-root="/r/petclinic"><head>' + stub + "</head><body>"
            '<p class="sub counts pilelede">3 fixed' + build.push_pr_button(spec) + "</p>"
            + build.push_pr_dialog(spec) + f"<ol>{items}</ol>"
            + (build.EDITOR_JS if editor else "") + "</body></html>")


def out(tag: str, obj: dict) -> str:
    return "https://github.com/o/r/pull/51 @ d01c3776\nsome notes\n" + tag + " " + json.dumps(obj)


def record(status="complete", present=2, sha=None) -> dict:
    slug = build.pr_comment_slug
    return {"status": status, "expected": 3, "present": present, "pr": 51,
            "url": "https://github.com/o/r/pull/51", "pushed_at": "2026-10-07T15:28:54+02:00",
            "review_url": "https://github.com/o/r/pull/51#pullrequestreview-9",
            "message": PARTIAL["message"] if status == "partial" else "All 3 comments are on GitHub PR #51.",
            **({"payload_sha256": sha} if sha else {}),
            "comments": {
                f"I:{slug(TITLES[0])}": {"mode": "line", "path": "src/A.java", "line": 12,
                                         "html_url": "https://github.com/o/r/pull/51#discussion_r1"},
                f"A:{slug(TITLES[1])}": {"mode": "body", "path": "src/B.java",
                                         "html_url": "https://github.com/o/r/pull/51#pullrequestreview-9"},
                f"F:{slug(TITLES[2])}": {"mode": "line", "html_url": None}}}


@pytest.fixture(scope="module")
def browser():
    """Its own, as `test_dataset_view.py` has: the `browser` it used to borrow was
    pytest-playwright's, a plugin a laptop happened to have and `requirements-dev.txt` (so
    CI) does not install, where the fixture simply did not exist."""
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:                       # no chromium installed on this box
            pytest.skip(f"chromium unavailable: {e}")
        yield b
        b.close()


def open_page(browser, answers: dict, files: dict, pick: str | None = None):
    """The page at http://127.0.0.1:9/review.html, its two JSON files served out of `files`
    (mutable: a test changes what the server holds between two presses). `pick`: the page
    with editor.js on it too, and that IDE picked in the masthead."""
    page = browser.new_page()
    if pick:
        page.add_init_script(f"localStorage.setItem('hr-ide', '{pick}')")

    def serve(route):
        name = route.request.url.rsplit("/", 1)[-1]
        if name == "review.html":
            return route.fulfill(body=page_html(answers, editor=bool(pick)), content_type="text/html")
        if files.get(name) is None:
            return route.fulfill(status=404, body="")
        body = files[name] if isinstance(files[name], str) else json.dumps(files[name])
        return route.fulfill(body=body, content_type="application/json")
    page.route("http://127.0.0.1:9/**", serve)
    page.goto("http://127.0.0.1:9/review.html")
    return page


def test_the_page_reads_the_same_tags_the_script_prints():
    assert build.PR_PREVIEW_TAG == ppc.PREVIEW_TAG
    assert build.PR_RESULT_TAG == ppc.RESULT_TAG


def test_post_asks_one_question_and_shows_comments_not_calls(browser):
    files = {}
    page = open_page(browser, {
        build.PUSH_PR_DRY_ACTION: {"exit": 0, "output": "# create a review\ngh api -X POST x\n"
                                   + out(build.PR_PREVIEW_TAG, PREVIEW)},
        build.PUSH_PR_ACTION: {"exit": 4, "output": out(build.PR_RESULT_TAG, PARTIAL)}}, files)
    page.click(".pr-push")
    dlg = page.locator("#pr-push-dlg")
    dlg.wait_for(state="visible")
    assert page.locator(".pp-q").inner_text() == \
        "Post 2 comments on GitHub PR #51 as @victorrentea?"
    assert "41 comments already there" in page.locator(".pp-sub").inner_text()
    assert "gh api" not in dlg.inner_text()
    assert not page.locator(".pp-list").is_visible(), "collapsed until asked"
    page.click(".pp-what summary")
    items = page.locator(".pp-list li")
    assert items.count() == 2 and "src/A.java:12" in items.nth(0).inner_text()
    assert "keep the vet" in items.nth(0).inner_text()
    files["pr-comments.posted.json"] = record("partial", present=2)   # what the push wrote
    page.click("#pr-push-dlg button[value=post]")
    page.wait_for_function("document.querySelector('.pr-push').textContent === 'Retry'")
    msg = page.locator(".pr-push-msg")
    page.wait_for_function("document.querySelector('.pr-push-msg').classList.contains('pp-err')")
    assert msg.inner_text() == PARTIAL["message"]
    assert not page.locator(".pr-push").is_disabled()
    assert page.evaluate("__alerts") == 0
    assert page.evaluate("__runs") == [build.PUSH_PR_DRY_ACTION, build.PUSH_PR_ACTION]
    # What did reach GitHub has its ↗ already — no rebuild, no reload.
    links = page.locator("a.f-gh")
    assert links.count() == 2
    assert links.nth(0).get_attribute("href").endswith("#discussion_r1")
    assert links.nth(0).get_attribute("data-pr-line") == "12"
    assert "summary" in links.nth(1).get_attribute("data-tip")
    # "in VS Code": the commented file at its line, in this checkout — editor.js opens it
    # in the checkout's window and brings the thread up (the ↗ beside it names it).
    vsc = page.locator("a.f-vsc")
    assert vsc.count() == 2
    assert vsc.nth(0).get_attribute("href") == "vscode://file//r/petclinic/src/A.java:12:1"
    assert vsc.nth(0).locator("svg.vsc-ico").count() == 1


def test_cancel_posts_nothing_and_gives_the_button_back(browser):
    page = open_page(browser, {build.PUSH_PR_DRY_ACTION: {
        "exit": 0, "output": out(build.PR_PREVIEW_TAG, PREVIEW)}}, {})
    page.click(".pr-push")
    page.locator("#pr-push-dlg").wait_for(state="visible")
    page.click("#pr-push-dlg button[value=cancel]")
    page.wait_for_function("!document.querySelector('.pr-push').disabled")
    assert page.locator(".pr-push").inner_text() == "Publish on GitHub"
    assert page.evaluate("__runs") == [build.PUSH_PR_DRY_ACTION]


def test_nothing_to_post_says_so_without_a_dialog(browser):
    page = open_page(browser, {build.PUSH_PR_DRY_ACTION: {
        "exit": 0, "output": out(build.PR_PREVIEW_TAG,
                                 {**PREVIEW, "already": 43, "items": []})}}, {})
    page.click(".pr-push")
    page.wait_for_function("document.querySelector('.pr-push-msg').textContent !== ''")
    assert page.locator(".pr-push-msg").inner_text() == \
        "All 43 comments are already on GitHub PR #51."
    assert not page.locator("#pr-push-dlg").is_visible()


def test_a_failed_check_is_said_in_words(browser):
    page = open_page(browser, {build.PUSH_PR_DRY_ACTION: {
        "exit": 2, "output": "cannot find the PR: gh: HTTP 401"}}, {})
    page.click(".pr-push")
    page.wait_for_function("document.querySelector('.pr-push-msg').textContent !== ''")
    text = page.locator(".pr-push-msg").inner_text()
    assert text.startswith("Could not read the pull request") and "gh:" not in text
    assert page.evaluate("__alerts") == 0


def _sha(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()


def test_everything_posted_reads_published_as_grey_text_that_does_nothing(browser):
    """Victor, 19 Oct 2026: not a button — grey "✓ Published", the where and when in the
    hover, and a click opens nothing and runs nothing."""
    page = open_page(browser, {}, {"pr-comments.json": PAYLOAD_TEXT,
                                   "pr-comments.posted.json": record(sha=_sha(PAYLOAD_TEXT))})
    page.wait_for_function("document.querySelector('.pr-push').dataset.state === 'done'")
    b = page.locator(".pr-push")
    assert b.inner_text() == "Published on GitHub"
    assert b.get_attribute("aria-disabled") == "true"
    assert b.get_attribute("data-tip") == "Published as a review on PR #51 \u2014 2026-10-07 15:28"
    assert not page.locator(".pr-repub").is_visible(), "unchanged: nothing to re-publish"
    assert page.locator("a.f-gh").count() == 2
    page.click(".pr-push", force=True)  # aria-disabled: Playwright would wait for enabled
    assert page.evaluate("__opened") == []
    assert page.evaluate("__runs") == []
    assert page.locator("a.pr-vsc").is_visible()
    assert page.locator("a.pr-vsc").get_attribute("href").endswith("uri=https%3A%2F%2Fgithub.com%2Fo%2Fr%2Fpull%2F51")


@pytest.mark.parametrize("pick", ["vscode", "idea"])
def test_in_the_picked_ide_opens_the_pr_there(browser, pick):
    """Served, through editor.js — the one script that hands the browser an editor URL.
    VS Code: POST /__editor_open__ first (the window on this checkout comes forward), then
    the extension's URI. IntelliJ: the link says so, and POST /__open_pr__ with the PR asks
    the plugin to show it in IntelliJ's Pull Requests tool window; nothing navigates."""
    page = open_page(browser, {}, {"pr-comments.json": PAYLOAD_TEXT,
                                   "pr-comments.posted.json": record(sha=_sha(PAYLOAD_TEXT))},
                     pick=pick)
    went = []
    cdp = page.context.new_cdp_session(page)
    cdp.on("Page.frameRequestedNavigation",
           lambda e: went.append(e["url"]) if e["url"].startswith(("vscode:", "jetbrains:")) else None)
    cdp.send("Page.enable")
    asked = []

    def answer(said):
        def fulfil(r):
            asked.append((r.request.url.split("127.0.0.1:9", 1)[1], r.request.post_data))
            r.fulfill(body=json.dumps(said), content_type="application/json")
        return fulfil
    page.route("http://127.0.0.1:9/__editor_open__", answer({"how": "focused", "window": "petclinic"}))
    page.route("http://127.0.0.1:9/__open_pr__",
               answer({"how": "opened", "window": "petclinic", "number": 51}))
    page.wait_for_selector("a.pr-vsc:not([hidden])")
    a = page.locator("a.pr-vsc")
    assert a.locator(".ide-word").text_content() == ("IntelliJ" if pick == "idea" else "VS Code")
    assert a.get_attribute("data-hr-pr").endswith("uri=https%3A%2F%2Fgithub.com%2Fo%2Fr%2Fpull%2F51")
    assert a.get_attribute("href") == (
        "jetbrains://idea/human-review/open-pr?url=https%3A%2F%2Fgithub.com%2Fo%2Fr%2Fpull%2F51"
        "&root=%2Fr%2Fpetclinic" if pick == "idea" else a.get_attribute("data-hr-pr"))
    assert "GitHub Pull Requests view" in a.get_attribute("data-tip")
    page.click("a.pr-vsc")
    page.wait_for_timeout(500)
    if pick == "idea":
        assert asked == [("/__open_pr__", '{"url":"https://github.com/o/r/pull/51"}')], asked
        assert went == []
    else:
        assert asked == [("/__editor_open__", "{}")], "the checkout's window first"
        assert went == [a.get_attribute("data-hr-pr")], went
    page.close()


def test_the_build_puts_in_vs_code_beside_every_github_link():
    link = build.gh_comment_link({"_ghUrl": "https://github.com/o/r/pull/51#discussion_r1",
                                  "_ghFile": "vscode://file//r/src/A.java:12:1",
                                  "_ghWhere": "src/A.java:12"})
    assert 'class="f-vsc" href="vscode://file//r/src/A.java:12:1"' in link
    assert "svg" in link and "Open src/A.java:12 in VS Code" in link
    assert build.vscode_pr_uri("https://github.com/o/r/pull/51#pullrequestreview-9") == (
        "vscode://github.vscode-pull-request-github/open-pull-request-webview?uri="
        "https%3A%2F%2Fgithub.com%2Fo%2Fr%2Fpull%2F51")
    assert build.vscode_pr_uri("https://example.com/x") is None


def test_editor_js_sends_the_item_link_with_its_thread(tmp_path):
    """The item's *in VS Code* is a plain file reference to editor.js: served, it asks
    /__open__ for the file at the line with `comment=1`, because the ↗ in the same card
    names the thread on that line."""
    out_dir = tmp_path / ".human-review"
    out_dir.mkdir()
    (out_dir / build.PR_COMMENTS_JSON).write_text(json.dumps({"comments": [
        {"pile": "ignored", "title": "Keep the vet", "path": "src/A.java", "line": 12, "body": "b"}]}))
    (out_dir / build.PR_POSTED_JSON).write_text(json.dumps({"comments": {
        "I:keep-the-vet": {"mode": "line", "path": "src/A.java", "line": 12,
                           "html_url": "https://github.com/o/r/pull/51#discussion_r1"}}}))
    spec = {"pr": {"number": 51}, "findings": [{"title": "Keep the vet"}]}
    build.prepare_pr_push(spec, out_dir, tmp_path, HERE)
    item = spec["findings"][0]
    assert item["_ghFile"] == f"vscode://file/{tmp_path.resolve()}/src/A.java:12:1"


def test_a_changed_payload_offers_re_publish(browser):
    page = open_page(browser, {build.PUSH_PR_DRY_ACTION: {
        "exit": 0, "output": out(build.PR_PREVIEW_TAG, PREVIEW)}},
        {"pr-comments.json": PAYLOAD_TEXT + " ",
         "pr-comments.posted.json": record(sha=_sha(PAYLOAD_TEXT))})
    page.wait_for_function("document.querySelector('.pr-push').dataset.state === 'done'")
    assert page.locator(".pr-repub").is_visible()
    page.click(".pr-repub")
    page.locator("#pr-push-dlg").wait_for(state="visible")
    assert page.evaluate("__runs") == [build.PUSH_PR_DRY_ACTION]


def test_a_partial_record_renders_retry_and_its_sentence():
    base = {"count": 43, "posted": 30, "pushedAt": "2026-10-07T15:28:00",
            "counts": {"fixed": 25, "ignored": 14, "assumption": 4},
            "reviewUrl": "https://github.com/o/r/pull/51#pullrequestreview-9"}
    face = build.push_pr_button({"_prPush": {**base, "status": "partial",
                                             "message": PARTIAL["message"]}})
    assert ">Retry</button>" in face and 'data-face="Publish on GitHub"' in face
    assert PARTIAL["message"] in face and "pp-err" in face
    face = build.push_pr_button({"_prPush": {**base, "status": "complete", "message": "x"}})
    assert ">Published on GitHub</button>" in face and 'data-state="done"' in face
    assert 'aria-disabled="true"' in face and "↗" not in face.split("</button>")[0]
    assert 'data-tip="Published as a review on the pull request — 2026-10-07 15:28"' in face
    assert 'class="pr-repub" hidden' in face
    face = build.push_pr_button({"_prPush": {**base, "status": "complete", "changed": True}})
    assert 'class="pr-repub" data-tip' in face


def test_the_push_action_is_the_push_alone(tmp_path):
    """No rebuild after it: the page decorates itself from the record."""
    out_dir = tmp_path / ".human-review"
    out_dir.mkdir()
    (out_dir / build.PR_COMMENTS_JSON).write_text(json.dumps(
        {"comments": [{"pile": "fixed", "title": "t", "path": "a", "line": 1, "body": "b"}]}))
    build.ACTIONS.clear()
    build.prepare_pr_push({"pr": {"number": 7}}, out_dir, tmp_path, HERE)
    push = build.ACTIONS[build.PUSH_PR_ACTION]
    assert "refresh-report" not in push["command"] and not push["reload"]
    assert build.ACTIONS[build.PUSH_PR_DRY_ACTION]["command"].endswith("--dry-run")


def test_changed_means_the_payload_is_not_the_file_it_was_pushed_from(tmp_path):
    (tmp_path / build.PR_COMMENTS_JSON).write_text(PAYLOAD_TEXT)
    assert not build._payload_changed(tmp_path, {"payload_sha256": _sha(PAYLOAD_TEXT)})
    assert build._payload_changed(tmp_path, {"payload_sha256": _sha("other")})
    assert not build._payload_changed(tmp_path, {}), "an old record cannot say"


# ------------------------------------------------- auto-fixes: their fix commit, not the PR
# Victor, 30 Oct 2026: an auto-fixed item is fixed by a commit already. Its card's buttons
# open that commit — on GitHub, and as the commit's diff in the IDE — never a PR comment;
# the push puts its note on the commit.

FIX = "be762955d5f477feeca0f7db55e32fa1c2fb8db5"
PARENT = "1bcf74d4f14a165cabaaa7e9cd1cc5faaa3311b1"


def fixed_item(**kw) -> dict:
    return {"title": "Unknown vetId untested", "_fixSha": FIX, "_fixForge": "GitHub",
            "_fixUrl": f"https://github.com/o/r/commit/{FIX}",
            "_fixOpen": {"path": "src/VisitTest.java", "line": 380, "base": PARENT,
                         "abs": "/r/petclinic/src/VisitTest.java"}, **kw}


def test_a_fixed_card_opens_its_fix_commit_on_github_and_in_the_ide(monkeypatch):
    monkeypatch.setenv("HUMAN_REVIEW_DIFF_URI_HANDLER", "victorrentea.human-review")
    links = build.fix_commit_links(fixed_item())
    assert f'class="f-gh f-commit" href="https://github.com/o/r/commit/{FIX}"' in links
    assert 'data-tip="Open the fix commit be762955 on GitHub"' in links
    assert "PR comment" not in links and "discussion_r" not in links
    # The IDE's: the commit's own diff, parent to commit, landing on the item's line.
    assert 'href="vscode://file//r/petclinic/src/VisitTest.java:380:1"' in links
    assert f'data-diff-base="{PARENT}" data-diff-head="{FIX}"' in links
    assert 'data-diff-path="src/VisitTest.java"' in links
    assert "Open the fix commit be762955 in VS Code — its diff, at VisitTest.java:380" in links
    assert f"head={FIX}" in links and "vscode://victorrentea.human-review/diff?" in links
    # Once pushed, GitHub's mark lands on the note on that commit.
    note = f"https://github.com/o/r/commit/{FIX}#commitcomment-9"
    links = build.fix_commit_links(fixed_item(_fixNoteUrl=note))
    assert f'href="{note}"' in links and "the review&#x27;s note on this fix" in links
    assert build.fix_commit_links({"title": "no commit"}) == ""


def test_only_a_comment_on_a_commit_is_an_auto_fixs_note():
    from hrbuild.shared.forge import is_commit_comment_url
    assert is_commit_comment_url(f"https://github.com/o/r/commit/{FIX}#commitcomment-9")
    assert not is_commit_comment_url("https://github.com/o/r/pull/51#discussion_r7")
    assert not is_commit_comment_url(f"https://github.com/o/r/commit/{FIX}")
    assert not is_commit_comment_url(f"https://github.com/o/r/commit/{FIX}#commitcomment-9 x")
    assert not is_commit_comment_url(None)


def test_an_auto_fix_is_never_linked_to_its_old_pr_comment(tmp_path):
    out_dir = tmp_path / ".human-review"
    out_dir.mkdir()
    (out_dir / build.PR_COMMENTS_JSON).write_text(json.dumps({"comments": [
        {"pile": "fixed", "title": "Return the vet", "path": "src/A.java", "line": 3, "body": "b"},
        {"pile": "ignored", "title": "Keep the vet", "path": "src/A.java", "line": 12, "body": "b"}]}))
    old = {"mode": "line", "path": "src/A.java", "line": 3,
           "html_url": "https://github.com/o/r/pull/51#discussion_r7"}
    (out_dir / build.PR_POSTED_JSON).write_text(json.dumps(
        {"status": "complete", "comments": {"F:return-the-vet": old}}))
    spec = {"pr": {"number": 51}, "autofixes": [{"title": "Return the vet"}]}
    build.prepare_pr_push(spec, out_dir, tmp_path, HERE)
    item = spec["autofixes"][0]
    assert "_ghUrl" not in item and "_fixNoteUrl" not in item
    assert spec["_prPush"]["changed"], "it would go on the commit now: re-publish is offered"
    assert spec["_prPush"]["counts"] == {"fixed": 1, "ignored": 1, "assumption": 0}
    note = {"mode": "commit", "commit_id": FIX,
            "html_url": f"https://github.com/o/r/commit/{FIX}#commitcomment-9"}
    (out_dir / build.PR_POSTED_JSON).write_text(json.dumps(
        {"status": "complete", "comments": {"F:return-the-vet": note}}))
    spec = {"pr": {"number": 51}, "autofixes": [{"title": "Return the vet"}]}
    build.prepare_pr_push(spec, out_dir, tmp_path, HERE)
    assert spec["autofixes"][0]["_fixNoteUrl"] == note["html_url"]
    assert not spec["_prPush"]["changed"]
    face = build.push_pr_button({"_prPush": {**spec["_prPush"], "status": None}})
    assert "1 inline PR comment, and 1 note on the fix commit for what was auto-fixed" in face


def test_the_dialog_says_where_auto_fix_notes_go(browser):
    preview = json.loads(json.dumps(PREVIEW))
    preview["items"].append({"id": "F:c", "fate": "new", "pile": "fixed", "title": "C",
                             "target": "commit", "commit": FIX,
                             "where": f"fix commit {FIX[:8]} · src/C.java:9",
                             "text": "🤖⚔️🤖✅ **Auto-fixed** — C"})
    page = open_page(browser, {build.PUSH_PR_DRY_ACTION: {
        "exit": 0, "output": out(build.PR_PREVIEW_TAG, preview)}}, {})
    page.click(".pr-push")
    page.locator("#pr-push-dlg").wait_for(state="visible")
    assert page.locator(".pp-q").inner_text() == (
        "Post 2 comments on GitHub PR #51 and 1 note on fix commit be762955 as @victorrentea?")
    page.click(".pp-what summary")
    assert "fix commit be762955 · src/C.java:9" in page.locator(".pp-list li").nth(2).inner_text()


def test_the_dialog_says_the_merge_risk_goes_into_the_pr_description(browser):
    line = "**Merge risk**\n- Two-way door: ✅\n- Blast radius: ◔ **visits API**"
    page = open_page(browser, {build.PUSH_PR_DRY_ACTION: {"exit": 0, "output": out(
        build.PR_PREVIEW_TAG, {**PREVIEW, "description": {"fate": "new", "text": line}})}}, {})
    page.click(".pr-push")
    page.locator("#pr-push-dlg").wait_for(state="visible")
    assert page.locator(".pp-q").inner_text() == (
        "Post 2 comments on GitHub PR #51 and the merge risk in its description as @victorrentea?")
    page.click(".pp-what summary")
    assert "PR description" in page.locator(".pp-list li").nth(2).inner_text()


def test_every_comment_already_there_still_offers_a_changed_merge_risk(browser):
    page = open_page(browser, {build.PUSH_PR_DRY_ACTION: {"exit": 0, "output": out(
        build.PR_PREVIEW_TAG, {**PREVIEW, "already": 43, "items": [],
                               "description": {"fate": "update", "text": "x"}})}}, {})
    page.click(".pr-push")
    page.locator("#pr-push-dlg").wait_for(state="visible")
    assert page.locator(".pp-q").inner_text() == (
        "Post the merge risk in GitHub PR #51\u2019s description as @victorrentea?")
    same = open_page(browser, {build.PUSH_PR_DRY_ACTION: {"exit": 0, "output": out(
        build.PR_PREVIEW_TAG, {**PREVIEW, "already": 43, "items": [],
                               "description": {"fate": "same", "text": "x"}})}}, {})
    same.click(".pr-push")
    same.wait_for_function("document.querySelector('.pr-push-msg').textContent !== ''")
    assert not same.locator("#pr-push-dlg").is_visible()


def test_the_page_points_a_fixed_card_at_its_note_never_at_a_pr_comment(browser):
    """`decorate`, after a push, without a rebuild: the Fixed card's GitHub mark moves to
    the note on its fix commit; an auto-fix the record still has as a PR comment gets no
    PR link, and re-publish is offered."""
    def html(rec):
        stub = """<script>window.HR = {onready: function (f) { f({token: 't'}); },
          can: function () { return true; }, keepPlace: function () {},
          run: function () { return Promise.resolve({}); }};</script>"""
        card = ('<li class="fixed">' + build.fix_commit_links(fixed_item())
                + '<span class="f-title">Return the vet</span></li>')
        spec = {"_prPush": {"count": 1, "posted": 1, "pushedAt": None,
                            "counts": {"fixed": 1, "ignored": 0, "assumption": 0}}}
        return ('<!doctype html><html data-hr-root="/r/petclinic"><head>' + stub
                + '</head><body><p class="sub counts">' + build.push_pr_button(spec) + "</p>"
                + build.push_pr_dialog(spec) + f'<ol class="findings">{card}</ol></body></html>')

    for rec, want in (
            ({"status": "complete", "url": "https://github.com/o/r/pull/51", "pr": 51,
              "comments": {"F:return-the-vet": {
                  "mode": "commit", "commit_id": FIX,
                  "html_url": f"https://github.com/o/r/commit/{FIX}#commitcomment-9"}}},
             f"https://github.com/o/r/commit/{FIX}#commitcomment-9"),
            ({"status": "complete", "url": "https://github.com/o/r/pull/51", "pr": 51,
              "comments": {"F:return-the-vet": {
                  "mode": "line", "html_url": "https://github.com/o/r/pull/51#discussion_r7"}}},
             f"https://github.com/o/r/commit/{FIX}")):
        page = browser.new_page()

        def server(rec):
            def serve(route):
                name = route.request.url.rsplit("/", 1)[-1]
                if name == "review.html":
                    return route.fulfill(body=html(rec), content_type="text/html")
                if name == "pr-comments.posted.json":
                    return route.fulfill(body=json.dumps(rec), content_type="application/json")
                return route.fulfill(status=404, body="")
            return serve
        page.route("http://127.0.0.1:9/**", server(rec))
        page.goto("http://127.0.0.1:9/review.html")
        page.wait_for_function("document.querySelector('.pr-push').dataset.state === 'done'")
        links = page.locator("li.fixed > a.f-gh")
        assert links.count() == 1 and links.get_attribute("href") == want
        assert page.locator("a.f-gh[href*='discussion_r']").count() == 0
        assert page.locator(".pr-repub").is_visible() == (rec["comments"]["F:return-the-vet"]
                                                          ["mode"] != "commit")
        page.close()
