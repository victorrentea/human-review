#!/usr/bin/env python3
"""Render a self-contained HTML review guide from a small JSON content file.

Splits the review into the half a machine should own and the half a human should:

  * this script owns the *mechanics* — page shell, styling, inlining the delta
    SVGs, laying out the diagram gallery, cutting every code snippet out of the
    working tree at build time via extract-snippet.py;
  * the JSON owns the *judgement* — what changed, what is risky, in what order a
    reviewer should look.

No snippet text ever lives in the JSON: only a `path:from-to` reference, so a
guide can never drift from the code it quotes.

Usage:
    build-review-html.py content.json --out .human-review/review.html
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import code_xref  # noqa: E402 - resolved from next to this file, not from site-packages

# --------------------------------------------------------------------------- #
# the page, one module per tab
# --------------------------------------------------------------------------- #
#
# Everything below `main` used to live here: 10,500 lines in which the Logging tab's
# model prompts sat forty lines from the cost table and both sat inside the same file
# as the CSS. It is now a package next door, `hrbuild/`, split the way the work is split —
# one module per tab under `hrbuild/tabs/`, everything two or more tabs share under
# `hrbuild/shared/`, and the stylesheet and the scripts as real files under
# `hrbuild/assets/`. The point is not tidiness: it is that a change to the Sequence tab
# and a change to the cost table are now edits to different files, so two agents can make
# them at the same time without either one rebasing over the other.
#
# What is left in this file is the orchestration and nothing else: read the content file,
# validate it, resolve the piles, render each tab's blocks in the order the content file
# asks for, assemble the document, write it. Every name the modules define is re-exported
# below, because this file is the import surface the rest of the skill (and its tests)
# has always used — `ds-audit.py`, `serve-review.py` and two dozen test modules load it
# by path and reach for a function on it.
# --------------------------------------------------------------------------- #

from hrbuild.shared.util import (
    CODEOWNERS, EXTRACT, PENCIL, SPELLED_OUT, TESTCHANGES, _git, _pretty   # HERE: defined above, same value
)
from hrbuild.shared.actions import (
    ACTIONS, ACTIONS_FILE, declare_action, declare_rerun_actions, declare_rerun_tests_action,
    declare_tab_reruns, slow_steps, RERUN_TESTS_ACTION,
    tab_rerun_id, tab_steps, TAB_AI, TAB_TIPS, AI_STEPS, PRICED, _load,
    RERUN_ACTION, RERUN_AI_ACTION, write_actions
)
from hrbuild.shared.assets import (
    APP_ENV_JS, CAPTION_JS, CSS, CSS_FILES, DGM_VIEWS_JS, EDITOR_JS, FOCUS_JS, FOOTER_CSS,
    GENSEQ_JS, HSCROLL_JS, LATE_CSS, PAINT_HOLD_JS, PAINT_RELEASE_JS, RERUN_JS, SEQFOLD_JS, SEQHEADS_JS,
    SEQLINK_JS, SERVER_JS, TABS_JS, TIP_JS, TRACE_JS, XREF_CSS, XREF_JS, FOLDERS_JS,
    FIXTURES_JS, EXPLAIN_JS
)
from hrbuild.shared.fixtures import (
    FIXTURE_COLORS_FILE,
    FIXTURE_PALETTE,
    FIXTURE_SEED,
    _FX_BLOCK,
    _FX_COLOUR,
    _FX_KW,
    _FX_NAME,
    _FX_SEEDWORD,
    _FX_SPEC,
    _FX_STEPDEF,
    _FX_TEST,
    _fx_block_end,
    _fx_explicit,
    _fx_expr_to_regex,
    _fx_feature_tests,
    _fx_local_imports,
    _fx_ls,
    _fx_spec_tests,
    _fx_step_defs,
    _fx_strip_comments,
    _fx_verdict,
    demo_fixtures,
    fixture_colors,
    fixture_dirs,
    fixture_registry,
    fixtures_by_test,
    render_fixture_registry
)
from hrbuild.shared.commands import (
    CMD_COPY, CMD_PLAY, CMD_RUN, CMD_STOP, command_html, COPY_TIP,
    drawio_open_html,
    regenerate_html, RERUN_AI_CHIP, RERUN_AI_CONFIRM, RERUN_CHIP, RERUN_DONE, RERUN_FAIL,
    PROGRESS_BUILD_SECONDS, PROGRESS_STEP_DEFAULT, rerun_progress_html, step_expectations,
    rerun_face, RERUN_MARK_AI, RERUN_MARK_SCRIPT, RERUN_MARK_SLOW, _RERUN_RING,
    rerun_html, rerun_tests_chip, RUN_TESTS_FACE, tab_rerun_html, TAB_RERUN_TIP,
    place_tab_reruns, tab_title_row, TAB_TITLES, CARD_HEADED_TABS, _TABTITLE_OPEN, _CARD_TITLE,
    reveal_html, runtime_html, fixtures_row_html, SEED_OFFLINE_TIP, STATIC_RUN_TIP,
    _app_anchor
)
from hrbuild.shared.snippets import (
    DIFF_CONTEXT, diff_html, DIFF_INLINE_TOKEN, diff_link_html, DIFF_TOKEN, diff_uri_handler,
    expand_snippets, github_blob_base, review_step_rev, SNIPPET_BASE, snippet_html,
    SNIPPET_TOKEN, _extract_module, _parse_unified, set_diff_base, commit_stamp
)
from hrbuild.shared.filemark import (
    FILE_MARK_D, FILE_PAGE, FILE_PAGE_D, FILE_PENCIL, FILE_PLUS, filemark, filemark_kind,
    mark_path, page_path
)
from hrbuild.shared.svg import (
    CREOLE_IN_TITLE, CREOLE_LINK, DIAGRAM_COLOR_VARS, DIAGRAM_FILL_ATTR, DIAGRAM_STYLE_COLOR,
    ENTITY_BLOCK, ENTITY_SOLE_ANCHOR, ENTITY_TITLE_BAND, inline_svg, resolve_source_links,
    SRC_HANDLE, SVG_TITLE, TEXT_LENGTH_ATTRS, _plain_svg_title, _scope_entity_links,
    _theme_diagram_colors
)
from hrbuild.shared.genseq import (
    genseq_by_test, GENSEQ_CALL_TITLE, genseq_details, genseq_details_at_base,
    genseq_details_at_render, genseq_file, genseq_overlay, genseq_overlay_copy,
    committed_since, _overlay_state, GENSEQ_OVERLAY, GENSEQ_HANDLE, HTTP_VERBS, MAPPING_ANNOTATION, MAPPING_NAMED_PATH,
    MAPPING_POSITIONAL_PATH, METHOD_NAME, pair_anchor, REQUEST_METHOD, SKIP_DIRS,
    spring_handlers, test_of_genseq, TYPE_DECL, _annotation_span, _controller_routes,
    _declared_test, _details_carrier, _join_route, _mapping_path, _with_handlers
)
from hrbuild.shared.folders import (
    folder_targets, link_folders, maven_modules, package_folders, puml_boxes, _artifact_id,
    _DECL, _ENTITY, _KEYWORD, _ls_files, _module_dir, _packages, _pattern, _SOURCE_DIR,
    _STEREO, _tree_base
)
from hrbuild.shared.diagrams import (
    CARD_NAME, card_rerun_html, card_rerun_id, REFRESH_CARD,
    CM_LEGEND_NEW, CM_LEGEND_TODO, DEFAULT_FOCUS, DGM_SRC_ANCHOR, dgm_views_html, DRAWIO_TOKEN,
    drawio_unchanged, drawio_unchanged_at, drawio_widget_html, expand_drawio,
    find_diagram_source, read_manifest, render_diagrams, render_puml, schema_unseen_note,
    select_rows, shorten_dgm_src, unchanged_row, UNCHANGED, UNCHANGED_BADGE, VIEW_WORDS,
    _context_svg, _diagram_views, _drawio_unchanged_card, _focus_views, _provenance,
    _source_link, _sql_shape, _SQL_COLLATE, _SQL_COLUMN, _SQL_INDEX, _SQL_TABLE,
    _unchanged_body, _why_not_drawn, trace_legend
)
from hrbuild.shared.c4 import C4_DIR, C4_MIN_SCALE, C4_SCALE, C4_VIEWBOX, render_c4, _c4_badge, _c4_body, \
    _c4_img, _c4_picture, _CHECKED_NOTE, _DSL_INCLUDE, _VIEW_KEYWORDS, _checked_label, _dsl_link, \
    _test_file, _view_definition
from hrbuild.shared.bands import (
    set_bands, _BANDS, _TOP_BANDS, _flush_bands, _flush_top_bands, _lede_above
)
from hrbuild.shared.chips import (
    base_state, base_warning, chip_face, chip_html, diffstat_chips, GENERATED_PATHSPECS,
    _compare_href, _numstat, _resolve_base, BASE_SOURCES, COMMITS_JSON, measured_from,
    page_base, POINTS_JSON, _front_matter, _is_ancestor, _recorded_bases,
    GENERATED_GLOBS, generated_globs, REVIEW_BOOKKEEPING, _project_cfg,
    raised_by_reviewer, review_chip_face, review_chip_key, _angry_bot, reviewer_names, _REVIEWER_WORD,
    patch_equivalent, NO_FETCH_ENV, fetch_base
)
from hrbuild.shared.masthead import (
    FAVICON, FAVICON_EMOJI, FAVICON_SVG, masthead_html, outside_note, page_title, ref_badges,
    title_ticket_ref, OUTSIDE_ID, outside_badge, _outside_where, _split_outside,
    _picked_outside
)
from hrbuild.shared.adopt import (
    DIAGRAM_CARD, DIAGRAM_KINDS, EXPLAIN, PETCLINIC, PIECES, PLACES, ROBOT, _CAPTION, _TAG,
    _close, _last_caption, adopt_html, adopt_prompt, explain_button, explain_data, explain_key,
    place_prompts
)
from hrbuild.shared.footer import (
    DEMO_DOCKER_URL, DEMO_PAGES_URL, DEMO_ZIP_URL, FOOTER_BOILERPLATE, HOME_URL, INVITATION,
    ISSUES_URL, PAST_INVITATIONS, PROVENANCE, RUNNING_STACK, TAKEAWAY, _link_home
)
from hrbuild.shared.tabstrip import (
    check_tab_enumeration, NUMBER_WORDS, spelled, TAB_COUNT_TOKEN
)
from hrbuild.shared.postprocess import (
    ANCHOR, check_baked_excerpts, one_tooltip_only, open_links_in_new_tabs, TARGET_ATTR
)
from hrbuild.shared.validate import (
    REQUIRED, validate
)
from hrbuild.shared.layout import (
    _layout_overridden, _layout_section, _video_step_ran, LAYOUT_ALWAYS, LAYOUT_MODEL_KEYS, LAYOUT_PRODUCER,
    LAYOUT_SECTIONS, LAYOUT_TABS, SHORT_TAB_LABELS, own_layout, LAYOUT_TESTPAIRS, _squash, _own_testpairs,
    _own_diagram_title, LAYOUT_WHEN_WRITTEN, LAYOUT_BLOCK_WHEN_WRITTEN
)
from hrbuild.tabs.review import (
    AFTERMATH_FILES, aftermath_html, aftermath_reads_takeover, AFTERMATH_JSON, commits_after_review, CONFIDENCE_TIP,
    opening_lede, PASS_DOCS,
    PILE_BLOCKS, pile_numbers, is_refuted, refuted_number, PILELEDE_SPY_JS, points_empty_html, POINTS_MISSING_BAND, points_note_band,
    POINTS_PILES, render_assumptions, render_autofixes, render_findings, render_pile_block,
    review_tab_badge, ASSUMPTION_BADGE, _decided_by, own_review_tab, pile_intro, PILE_TITLES,
    REVIEW_TAB_TIP,
    reset_list, resolve_refs, resolve_review_points, REVIEW_POINTS_JSON, scope_chip_face,
    _points_parser, unglue_refs,
    SCOPE_CHIP_MAX_LEN, SEVERITIES, _aftermath_commit, _aftermath_files_tip,
    _assumptions_block, _code_totals, _confidence_chip, _finding_refs, _finding_source, _fold_note_lists,
    grade_reasons, grade_reasons_html, _first_clause, _CLAUSE_END, PILE_ROUND, _round_kicker,
    _LEDE_SHOWN, _LIST_OFFSET, _merge_seam_shas, _open_list, _pile_anchor, _raised_by,
    _ref_link, _regenerate_offer, _score_target, _tooling_commit_shas,
    gh_comment_link, prepare_pr_push, attach_pr_body, pr_description_html, pr_body_html,
    _md_inline, PR_BODY_JSON, _PULL_URL, _PR_BODY_BOILERPLATE, pr_comment_slug, PR_COMMENTS_JSON,
    PR_PILE_LETTER,
    PR_POSTED_JSON, PR_PUSH_JS, push_pr_button, push_pr_dialog, PUSH_PR_ACTION,
    PUSH_PR_DRY_ACTION, _PR_SLUG_MAX, PR_PREVIEW_TAG, PR_RESULT_TAG, _payload_changed,
    VSCODE_PR_URI, vscode_pr_uri, vscode_pr_link, _PR_URL, VSC_ICON, vscode_file_link,
    _taken_fold_html, _tooling_fold_html,
    API_VERDICT_HTML, FILM_VERDICT_JSON, GATE_JSON, GRADE_CAPS, MODEL_GRADE_LINES, PILE_SIGNALS,
    SEQUENCE_VERDICT_JSON, _after_review_signal, _api_signal, _base_ref, _ci_signal,
    _evidence_signal, _git_out, _git_root, _out_of_range_signal, _pile_signals, _plain_text,
    _read_json, _signal, cap_grade, grade_signals,
    FIX_BOOKKEEPING, FIX_HUNK_REACH, _fix_range, _gap, _ref_spans, attribute_fix_hunks,
    fix_hunks, _assumption_why, drop_model_summary, pr_exists, resolve_piles,
    _reviewers, CLAUSE_CAP, _CODE_SPAN, TEST_MAPPING_FILES, REQMAP_HTML, NARROWED_LINES,
    NARROWED_QUOTE, _quote, _decision_link, _narrowed_signals, _grade_rows, fix_commit,
    ANCHORS_KEY, ANCHORS_AT_REVIEW, _written_at, reanchor_refs, _anchor_note,
    drop_stale_pr_comments, NO_PR_LINE, no_pr_line,
    _render_finding_items, REVIEW_COMMITS_JSON, generated_in, fix_commits,
    _commit_face, fix_commits_html, review_commits_warnings, SPEC_DOCS, QA_DOC,
    CITING_FIELDS, CITE_QUOTE, _CITE, _CITE_GUARD, _spec_change_dir, _doc_lines,
    _cited_quote, _find_line, _resolve_citation, link_spec_citations, urllib_quote,
    _before_range_commits, _spec_commit_signals, _PILE_COUNT, _Q_REF, model_line_conflict,
    _drop_model_line, SNIPPET_LINES, STATEMENT_LINES, _STATEMENT_SUFFIXES, _CONT_START,
    _OPEN_END, widen_anchor, _snapped_spans, _spans_ref, _first_lines, snippet_card, _snippet_card_open,
    GRADE_LINES_MAX, SPARE_SIGNALS, _REPEATS, _PILE_WORD, _PILE_OF_WORD, _PILE_SAID,
    _plain_words, named_items, hunk_bodies, _layout_free, format_only_hunk, _OBS_LABELS,
    _obs_label, _STOPWORDS, _words, restates_title,
    _CONST_DECL, constants_declared, replaces_constant
)
from hrbuild.tabs.sequence import (
    render_testpairs, SEQ_ARROW, SEQ_DECL,
    SEQ_UI_DRIVERS, FILEMARK, STALE, TEST_CATS, TEST_RUNNERS, _cat_chip, _lang_label,
    _folded_pair, _line_spans, _moved_since_base, _narrowed, _pair_cat,
    _pair_runner, _scenario_extents, _scenarios_drawn, _share_excerpts, _spans_for,
    _stale_sequence, _unchanged_sequence, _unquoted_note,
    SEQ_VERDICT, SEQ_VERDICT_ALARM, SEQ_VERDICT_FACE, sequence_verdict,
    sequence_verdict_alarm, sequence_verdict_html, lost_note_html, _lost,
    AUTO_SNIPPETS, derived_snippets, GENSEQ_TAG, scenario_span, tagged_scenarios, _counted,
    _drew_nothing, _FEATURE_DECL, _FEATURE_STOP, _JAVA_DECL, _ref, _SKIP_LINE, _STRINGS,
    _tagged_decl, _test_kind, _TS_DECL,
    SEQ_SELECTION, SEQ_WHY, sequence_selection, _slug, picked_for, _why_chip,
    SEQ_ALSO, ledger_status,
    SEQ_TOUCHED, touched_via, _branch_changed, _direct_imports, _TS_IMPORT, _JAVA_IMPORT,
    TRACE_SHOT, TRACE_SHOT_META, TRACE_ANSWER, TRACE_HOW, TRACE_SAMPLED, trace_how_html,
    _trace_shot_html
)
from hrbuild.tabs.tests import (
    LEDGER_TAB, CAT_LABELS, relabel_cats, TRACED_RANK, TRACED_LABEL, REQMAP_FIT_JS, render_requirements, render_test_ledger, _names_by_file, render_tests,
    render_traces,
    REQMAP_CSS, REQMAP_CUT, REQMAP_SEMCOV_JS, REQMAP_TIP_JS, reqmap_layout, resolve_tests,
    REQMAP_CHAPTERS_JS, REQMAP_LEDGER_JS, all_tests_toggle, all_tests_inventory, ALL_TESTS_ID,
    test_cover_files, TEST_COVER_ID, TEST_COVER_MAX,
    SEMCOV_LABEL, _SEMCOV_TIP, semcov_switch, SILENCED_LABEL, VIA_HELPER_LABEL, via_helper_tip,
    test_index, TEST_STATES,
    TICKET_CACHE, ticket_head, ticket_ref, tests_chip, _append_inside, _element, _find,
    drawn_ticket,
    CARD_WHO, CARD_WHEN, CARD_AI_TIP, card_head,
    SEMCOV, COV_NOT_MEASURED_SCRIPTED, _semcov_module, scripted_reqmap,
    RUN_TESTS_ACTION, run_tests_steps, declare_run_tests_rerun, run_tests_button,
    _gh_issue, _issue_url, _ms, _take, _test_changes_module,
    COVERAGE_JSON, COVCARD_WHO, COVCARD_WHO_PR, covcard_who, covcard_tip, COVCARD_TIP, COVCARD_TIP_ALL, COV_SHIELD, promote_traced, COV_COMMON_SHARE,
    COV_COMMON_MIN,
    COV_NOT_MEASURED, load_coverage, coverage_join, model_pairing, coverage_side, _model_key,
    TEMPLATE_UNSEEN, _rendered_templates,
    _cov_ranges, _snippet_module, COV_PART_MAX, _GHERKIN_NEXT, _cov_part,
    _API_MARKERS, _cov_cat, coverage_tests, coverage_after, _load_test_changes, suite_chips
)
from hrbuild.tabs.demo import (
    VOICE_TIPS, voice_films, voice_switch, embed_html, VERDICT_FACE, video_html, VIDEO_VERDICT,
    video_verdict_html, VOICE_EMOJI, STANDARD_VOICE_TIP, voice_face,
    _link_captions, derived_app_links, derived_runtime, _project_root, _screen_changed
)
from hrbuild.tabs.city import (
    CITY_HEADING, CITY_TAB, declare_city_run_tests
)
from hrbuild.tabs.logging import (
    LOGEXTRACT, logging_fragment, logging_libraries, logging_libraries_tip,
    MAX_ORIGIN_LINES_SHOWN, SRCREF_HREF, type_hint_html, _aim_at_statement, _hint_arguments,
    _CHAR, _insert_at, _LOG_PKGS_RE, _logextract, _logging_aside, _logging_listing,
    _logging_ref, _plain, _PRE, _ROW, _TAG_SPLIT, _change_line, _mark_rewritten, _ADDED_ROW
)
from hrbuild.tabs.owners import (
    codeowners_fragment
)
from hrbuild.tabs.cost import (
    COST_CACHE, cost_chip, cost_ledger_html, cost_ledger_report, COST_TAB_ID, PASS_ROWS,
    PHASE_ROWS, phase_rows_html, RESIDUAL_ROWS, tab_cost_report, TOTAL_FORMULA,
    _cost_env, _cost_inputs, _cost_money, cost_session,
    _cost_tab_rows, _cost_tokens, _when, components_html, cost_pill_label, cost_pill_title,
    _legacy_ledger_html, _HARNESS, _aic, _component_money, _minutes, _entry_line,
    guide_breakdown_html, _extension_line, _instants, _stamp_s, _span, _cost_cell,
    voice_money, voices_cost, voices_row_html, FISH_PRICE_PER_M_BYTES, FISH_DEFAULT_MODEL,
    BUSY_TIP, HUMAN_TIP, _human_cell, _human_total, _duration, _time_cell, COST_TITLE,
    _WHAT_SHORT, _cost_ledger_body, _entry_row,
    _split, TOOLING_INVESTMENT, tooling_investment_html
)


# An include that opens like a page rather than a fragment: a doctype, `<html>` or `<head>`
# before any content. Leading whitespace and comments are skipped; case does not matter.
WHOLE_DOCUMENT = re.compile(r"\s*(?:<!--.*?-->\s*)*<(?:!doctype\b|html\b|head\b)", re.I | re.S)

#: Blocks whose tab is never struck through, whatever the delta. The strike says "this
#: branch did not touch it", which is a statement about the diff — and the Sequence tab is
#: not about the diff: it is the overview of what the system really does when a test runs.
#: Victor, 5 Oct 2026: "The Sequence tab should never be crossed out."
NEVER_STRUCK = frozenset({"testpairs"})


def rebuild_interpreter() -> str:
    """How to say "python, with Pygments" on *this* machine, in a command a reader pastes.

    Not `sys.executable`. Under `uv run --with pygments` that is a path inside a build
    directory uv deletes on exit, so the one command guaranteed to have worked is also the
    one guaranteed not to work again — and a command that fails when pasted is worse than
    no command, because it is tried first and believed second.

    So: plain `python3` when it can already import Pygments, which is the legible answer
    and the portable one; `uv run --with pygments python` when it cannot and uv is here,
    which installs nothing permanently and is ~100ms warm; and only then the interpreter
    running this build, which at least names something real.
    """
    # The probe has to ask what `python3` means *in the reader's terminal*, not in this
    # process. Under `uv run` the front of PATH is two throwaway directories — the
    # interpreter's own, and the one holding the packages `--with` installed — and both
    # answer to `python3` with Pygments importable, so a naive probe cheerfully reports
    # that `python3` works about interpreters that are deleted on exit. Dropping every
    # ephemeral directory is what asks the right question; if nothing outside them answers
    # to `python3`, that is an answer too, and the uv line below is the honest one.
    env = dict(os.environ)
    throwaway = [str(Path(sys.prefix)), env.get("VIRTUAL_ENV") or "",
                 os.environ.get("UV_CACHE_DIR") or str(Path.home() / ".cache" / "uv")]
    env["PATH"] = os.pathsep.join(
        d for d in env.get("PATH", "").split(os.pathsep)
        if d and not any(t and (d == t or d.startswith(t + os.sep)) for t in throwaway))
    env.pop("VIRTUAL_ENV", None)
    outside = shutil.which("python3", path=env["PATH"])
    if outside and subprocess.run([outside, "-c", "import pygments"],
                                  capture_output=True, env=env).returncode == 0:
        return "python3"
    if shutil.which("uv", path=env["PATH"]) or shutil.which("uv"):
        return "uv run --with pygments python"
    return shlex.quote(sys.executable)


def main(argv=None) -> int:
    """`_main`, with the snippet base it moves put back afterwards.

    A build points every snippet at the page's one base (`set_diff_base`). In a process
    that builds once and exits that is the whole story; the test suite builds in-process
    too, and a base left behind by one page would be the next page's `origin/main`."""
    import hrbuild.shared.snippets as snippets_mod
    held, env = snippets_mod.SNIPPET_BASE, os.environ.get("HUMAN_REVIEW_DIFF_BASE")
    try:
        return _main(argv)
    finally:
        set_diff_base(held)
        if env is None:
            os.environ.pop("HUMAN_REVIEW_DIFF_BASE", None)


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("content", help="JSON content file")
    ap.add_argument("--out", required=True, help="where to write the HTML")
    # Accepted and ignored. It kept the Logging tab from buying privacy verdicts off a
    # model; that tab is read off declarations now and nothing in a build calls a model,
    # but `refresh-report.py` and older rebuild commands still pass it.
    ap.add_argument("--no-model", action="store_true",
                    help="no effect: nothing in a build calls a model any more")
    args = ap.parse_args(argv)

    root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
        ).stdout.strip()
    )
    spec = json.loads(Path(args.content).read_text(encoding="utf-8"))
    out_path = Path(args.out).resolve()
    out_dir = out_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    # Emptied here rather than trusted to be empty: the register is module state, and the
    # module is imported and driven directly by the test suite, where two builds in one
    # process would otherwise leave the second one declaring the first one's actions.
    ACTIONS.clear()
    # The masthead's two reruns go in first, before any tab renders: the aftermath band in
    # the Review tab offers the free one and reads its command out of the register, and a
    # band that rendered before the declaration would print an offer with no line behind it.
    declare_rerun_actions(root, out_dir, HERE)
    # And the masthead's ↺⏳: the tests and every other slow producer, forced, then the build.
    rerun_tests = declare_rerun_tests_action(root, out_dir, HERE)
    # And one per tab, for the ↻ at the end of that tab's own title on the served page.
    tab_reruns = declare_tab_reruns(root, out_dir, HERE,
                                    [t.get("id") for t in spec.get("tabs") or [] if t.get("id")])
    # And the Tests tab's third press — run the suites, then re-derive the tab — drawn in
    # the same `.tabre` span as the other two, so it shows exactly when they do.
    run_tests = declare_run_tests_rerun(root, out_dir, HERE)
    if run_tests and tab_reruns.get(LEDGER_TAB):
        tab_reruns[LEDGER_TAB]["extra"] = run_tests_button(run_tests)
    # ...and the Code City's, the same press with the city rebuilt after it: the city is
    # coloured by the coverage those suites measure.
    city_tests = declare_city_run_tests(root, out_dir, HERE)
    if city_tests and tab_reruns.get(CITY_TAB):
        tab_reruns[CITY_TAB]["extra"] = run_tests_button(city_tests)
    # How to start this build again — the last stage of every command offered under a
    # hand-drawn diagram.
    rebuild_cmd = " ".join([rebuild_interpreter(), shlex.quote(str(Path(__file__).resolve())),
                            shlex.quote(args.content), "--out", shlex.quote(args.out)])

    # Before `validate`, and before anything walks the piles: the three arrays may be a
    # delegation (`{"auto": "review-points"}`) rather than a list, and everything
    # downstream — the validator, the ref resolution, the ledes, the renderers — takes
    # them as lists. This is the point at which the branch's own record becomes the page's.
    resolve_review_points(spec, out_dir)
    prepare_pr_push(spec, out_dir, root, HERE)
    attach_pr_body(spec, out_dir, root)
    # The Tests tab's matrix is drawn from its inputs here, before `own_layout` looks for
    # the fragment: the ticket, the coverage and the pairing (tests.py:scripted_reqmap).
    scripted_reqmap(spec, out_dir, root)
    # The script-owned tabs, their sections and the scope bar are the skill's: whatever the
    # content file put there that no script produced is dropped here, before validation,
    # and named — the same treatment an unanchored assumption gets further down.
    for warning in own_layout(spec, out_dir):
        print(f"[review] WARNING: {warning}", file=sys.stderr)

    problems = validate(spec, out_dir)
    if problems:
        print(f"[review] {Path(args.content).name} cannot be rendered:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    # The ONE base every number this build computes is measured from — the files and lines
    # chips, CODEOWNERS, the Logging scan, a snippet's NEW FILE / NEW CODE badge and its
    # diff handles, the schema note under an ERD, the cost ledger. `page_base` says which
    # and why: the base the review audited when `review-points.md` records one, else the
    # base the producers ran against, else the fork point from the PR's base. Asked here,
    # before the first snippet renders, because the snippets are the first consumer. The
    # drift facts on the ref chip (`ahead`, `localBehind`) stay about the named base ref.
    # The base as it stands on the remote, not as it was at the last pull: eval run 11
    # counted the branch's drift against a local `main` hours behind origin.
    fetch_base(root, (spec.get("pr") or {}).get("base") or "origin/main")
    base_st = page_base(root, out_dir, (spec.get("pr") or {}).get("base") or "origin/main")
    spec["_afterReview"] = commits_after_review(out_dir, root)
    page_rev = (base_st or {}).get("diffBase")
    if page_rev:
        set_diff_base(page_rev)
    if (base_st or {}).get("stepsBase"):
        print(f"[review] WARNING: the producers ran against {base_st['stepsBase'][:8]} "
              f"(review-commits.json) and this page measures from {page_rev[:8]} "
              f"({BASE_SOURCES[base_st['diffBaseSource']]}) — the tabs they drew count a "
              f"different range than the header. Re-run: run-steps.py --base {page_rev[:8]}",
              file=sys.stderr)

    # The base every `diffs` entry is measured against when the entry does not name its
    # own: the rev the ledger recorded before the review pass touched anything. That is the
    # only left side that shows a fix on its own, and step 1 exists to record it.
    default_diff_base = review_step_rev(out_dir)
    for f in spec.get("findings", []) + spec.get("assumptions", []) + spec.get("autofixes", []):
        f["_refs"] = resolve_refs(f.get("refs", []), root)
        # `snippet_card`: the anchor widened to its statement, at most a dozen lines open.
        f["_snippets"] = "".join(
            snippet_card(s["ref"], s.get("caption"), root) for s in f.get("snippets", [])
        )
        # An applied fix that shows no diff is a claim with nothing behind it, so the build
        # says so — loudly enough to fix, quietly enough not to block a page whose ledger
        # has no rev (an older run, or a review re-rendered from a fresh clone).
        diffs = f.get("diffs", [])
        if diffs and not all(d.get("base") for d in diffs) and not default_diff_base:
            print("[review] WARNING: %r asks for a diff with no base, and the ledger's "
                  "review step recorded no rev \u2014 that diff is dropped"
                  % f.get("title", "")[:60], file=sys.stderr)
        f["_diffs"] = "".join(
            diff_html(d["path"], d.get("base") or default_diff_base, root, d.get("caption"),
                      d.get("head"), fold=True)
            for d in diffs
            if d.get("base") or default_diff_base
        )

    # An assumption with no code under it is the one item on this page that cannot be
    # checked at all. A finding without a snippet is at least a claim about a defect a
    # reader can go and look for; "I assumed the tenant is always the caller's" points at
    # nothing, and a model asked at the end of a long session what it was unsure about will
    # produce fluent sentences of exactly that shape whether or not it ever hesitated. The
    # anchor is what separates a recollection from a guess about a recollection, so an
    # unanchored one is dropped rather than printed with a shrug.
    floating = [a for a in spec.get("assumptions", [])
                if not (a.get("_snippets") or a.get("_diffs") or a.get("_refs"))]
    for a in floating:
        print("[review] WARNING: assumption %r names no code — dropped. Give it a "
              "'snippets' entry (or 'refs'/'diffs') pointing at the line the decision "
              "landed on; an assumption a reader cannot go and look at is indistinguishable "
              "from one that was never made." % (a.get("title", "")[:60]), file=sys.stderr)
    if floating:
        spec["assumptions"] = [a for a in spec["assumptions"] if a not in floating]

    # A page that never declares the block says nothing at all about the third pile, and
    # nothing reads as "there was nothing to say". The pile is the one part of this page no
    # pass can reconstruct afterwards, so its absence has to be noisy at build time rather
    # than silent on the page — declare it with its mode (`authoring-sessions.py` says
    # which) and the lede prints the count, zero included.
    if _assumptions_block(spec) is None:
        print("[review] WARNING: no 'assumptions' block in any tab — the page will say "
              "nothing about what the agent that wrote the code had to guess at, which a "
              "reader cannot tell from it having guessed at nothing. Declare the block "
              "with its mode (authoring-sessions.py --base ... says A, B or C) even when "
              "the pile is empty.", file=sys.stderr)

    # Every panel is `id="<tab id>"`, so a section that happens to share a tab's id puts the
    # same id on two elements — `id="api"` on the API contract panel and on the <h2> inside
    # it, which is what this page shipped for months. Nothing looked broken, because
    # getElementById returns the first match and the first match is the panel, which is
    # where `#api` should land anyway. It is still invalid HTML and still a trap for the
    # next person. The panel's id is not negotiable (the strip's aria-controls points at
    # it), so the duplicate is resolved on the heading, which loses nothing.
    tab_ids = {t.get("id") for t in (spec.get("tabs") or [])}

    # What the change set did to each test, computed by `test-changes.py` from the diff
    # itself. Loaded once: the content file only says which requirement a test belongs to.
    test_doc = (json.loads((out_dir / spec["testChanges"]).read_text(encoding="utf-8"))
                if spec.get("testChanges") else {})
    # The recordings the same run left behind, harvested by `playwright-traces.py`. Loaded
    # the same way and for the same reason: what happened in a test is measured, never
    # written down by hand.
    traces_doc = (json.loads((out_dir / spec["playwrightTraces"]).read_text(encoding="utf-8"))
                  if spec.get("playwrightTraces") else {})
    tests_idx = test_index(test_doc.get("tests", []))

    sections, by_id, unchanged_ids = [], {}, {}
    for s in spec.get("sections", []):
        unchanged_ids[s["id"]] = bool(s.get("unchanged"))
        snips = "".join(
            snippet_html(x["ref"], x.get("caption"), root) for x in s.get("snippets", [])
        )
        # An include is a fragment another generator produced (e.g. the complexity delta):
        # rendered by whoever owns that data, pasted in here rather than re-derived.
        inc = ""
        if s.get("includeHtml"):
            inc = (out_dir / s["includeHtml"]).read_text(encoding="utf-8")
        if inc and WHOLE_DOCUMENT.match(inc):
            # A fragment is pasted; a whole document must be framed. Another tool's full
            # report (pb33f's openapi-changes, written as `includeHtml` instead of
            # `embed`) brings its own <head>, and its stylesheet is not scoped: its `:root`
            # palette and `body` rule sit later in the cascade than ours and win. On a light
            # page that painted every tab's body near-black under a white masthead and set
            # the prose in the report's monospace face; in dark mode the hijack blended in,
            # which is how it shipped. A frame is the one boundary a stylesheet cannot cross.
            inc = (f'<iframe class="oacframe" src="{html.escape(s["includeHtml"])}" '
                   f'aria-label="{html.escape(s.get("title") or s["includeHtml"])}"></iframe>')
        elif inc:
            # One include is written by a model and is the whole of a tab: the
            # requirements↔tests matrix. What it *says* is the model's; where its two
            # columns sit, and the ticket title over them, is the same on every branch and
            # is put back here on every build. Every other fragment passes through
            # untouched — the function recognises the matrix by its own class and hands
            # anything else straight back.
            inc = reqmap_layout(inc, spec, out_dir, root)
        # Usually the include is commentary on the prose, so it follows it. `includeFirst`
        # is for the one shape where it is the other way round: the fragment *is* what the
        # section is about — the Tests tab opens on the ticket the branch answers —
        # and the prose reads as the reply to it. Off by default: every other tab wants a
        # sentence of its own before a generated fragment lands.
        include_first = bool(s.get("includeFirst"))
        vid = ""
        if s.get("video"):
            vid = video_html(s, out_dir)
        body = shorten_dgm_src(
            expand_drawio(expand_snippets(s.get("body", ""), root), out_dir, root,
                          rebuild_cmd))
        # A section that is nothing but a hand-drawn diagram the branch left alone (the
        # built-in `conceptual`) is context, not a change: it must not keep the Data tab
        # from being struck through when nothing on it moved.
        token = DRAWIO_TOKEN.fullmatch((s.get("body") or "").strip())
        if token and drawio_unchanged_at(token["name"], out_dir / "assets"):
            unchanged_ids[s["id"]] = True
        collides = s["id"] in tab_ids
        if collides:
            print(f'[review] section {s["id"]!r} shares its id with a tab: the heading drops '
                  f'its id, so #{s["id"]} lands on the panel (which is where it was already '
                  "going). Rename the section to get an anchor of its own.", file=sys.stderr)
        h2_id = "" if collides else f' id="{html.escape(s["id"])}"'
        rendered = (
            # An empty title means the section speaks for itself; emit no heading rather
            # than an empty one, which would still take the vertical space of a heading.
            (f'<h2{h2_id}>{html.escape(s["title"])}</h2>\n' if s.get("title") else "")
            + (f"{inc}\n" if include_first and inc else "")
            # A section with no prose of its own — the video tab is one — must not open with
            # a blank line where the paragraph would have been.
            + (f"{body}\n" if body else "")
            # The requirement list sits between the prose and anything else the section
            # carries: it *is* the section's answer, and a snippet or an include is
            # commentary on it.
            + render_requirements(s.get("requirements") or [], tests_idx, root)
            + f'{"" if include_first else inc}{vid}{snips}{embed_html(s, out_dir)}'
        )
        sections.append(rendered)
        by_id[s["id"]] = rendered

    city = spec.get("codecity")
    city_html = ""
    if city:
        # A heading, and not the lede that used to be here. The two are different kinds of
        # sentence and only one of them earns the space: the lede described the picture
        # ("10 buildings lit — the classes this change set touched"), which a reader is
        # looking at; the heading names what the picture is *for*, which they are not. The
        # tab pill says "Code City", the name of the visualisation; this says what it is
        # being shown to them to answer, and the trailing ellipsis is deliberate — the
        # three named axes are the ones the panel inside the shot lets them switch between,
        # and they are not all of them.
        heading = city.get("title") or CITY_HEADING
        # No lede. The line it held was always some version of *"10 buildings lit — the
        # classes this change set touched, in a city of the whole backend"*, which is three
        # claims the reader can already see: the count is legible in the shot, the lit slice
        # is what lit means, and the city being the whole backend is what a city is. It was
        # also a **hand-typed number** in a file nothing revalidates — the exact thing this
        # skill's own writing rule forbids — so it went stale silently the first time a
        # class was added.
        #
        # The anchor sits on the heading, so `#codecity` still lands here. The tab's title,
        # in the one dress every tab's first line wears (`tabtitle`, core.css): it also drops
        # the `h2` underline, which ran full width just above the card's own border and
        # separated nothing.
        if city.get("body"):
            print("[review] codecity.body is no longer rendered — the picture starts under "
                  "the tab strip. Delete it from the content file; every sentence it can "
                  "hold is either in the shot or a number that goes stale.", file=sys.stderr)
        city_html = (
            f'<h2 class="tabtitle" id="codecity">{html.escape(heading)}</h2>\n'
            f'<a class="city" href="{html.escape(city["href"])}"'
            f' target="_blank" rel="noopener"'
            f' data-tip="Open interactive">'
            f'<img src="{html.escape(city["png"])}" alt="Code City with the branch change set highlighted"></a>\n'
            # Only when there is one: the empty <p> still took a paragraph's margin under
            # the picture, on every page that never wrote a caption.
            + (f'<p class="sub">{city["caption"]}</p>' if city.get("caption") else "")
        )

    # Chips carry HTML on purpose: a chip is often a link (to the branch on GitHub, to a
    # section further down) or coloured (+added / -removed), and escaping would kill both.
    chips = []
    scope = spec.get("scope", [])
    # Where the base is was asked once, further up (`page_base`): the diffstat chip
    # measures from it and the ref chip warns about the named ref it was derived from, and
    # those two must never be talking about different commits.

    def emit(c: dict) -> None:
        """Render one resolved chip. Shared so that a chip which expands into several --
        `diffstat` becomes `files` and `lines` -- cannot pick up different markup than
        the ones written by hand beside it."""
        chips.append(chip_html(c))

    for c in scope:
        # The other chip that must never be typed. `{"auto": "autofixed"}` counts the
        # lists this page actually renders — the open findings, the fixes already
        # applied, and the assumptions the coding agent recorded — so the chip and the
        # Review tab can never disagree with each other. The reason it exists is that they
        # already did: the hand-typed `/code-review 8 findings` outlived the ninth finding
        # being added, and nothing caught it, because nothing was looking. `href` (and any
        # label or tip) still comes from the content file.
        if c.get("auto") == "autofixed":
            # No record, no chip. `🤖reviewer: 0 open, 0 fixed` is the whole failure
            # this flow exists to end: two measured-looking zeros asserting a review that
            # found nothing, where the truth is that nothing says a review happened. Every
            # other computed chip drops itself rather than print a number it cannot stand
            # behind; this one now does too.
            if (spec.get("_reviewPoints") or {}).get("missing"):
                continue
            # Counted here, after the unanchored assumptions have been dropped from `spec`
            # further up, so the chip promises exactly the pile the tab goes on to render.
            # A chip counting items the reader then cannot find is the same lie as a
            # hand-typed number, arrived at by a longer route.
            open_n, fixed, assumed = pile_numbers(spec)
            # Everything the reviewers raised, refuted claims included: the hover's
            # breakdown is counted off the same items (`_raised_by`).
            total = len(spec.get("findings", []) or []) + fixed
            # The REVIEWERS' model, not the first model on the run's bill — that one is
            # the implementation's, and named four Sonnet reviewers `Opus 5`.
            import harness_cost
            reviewer = ", ".join(harness_cost.reviewer_models(root, out_dir)) or c.get("by")
            # The hover's opening sentence: who read the diff AND who briefed them —
            # "Reviewers: Sonnet 5.5 (4 subagents), orchestrated by Opus 5.5" — because the
            # cost row for the review splits Opus/Sonnet and "Reviewed by Sonnet" hid half.
            reviewed = (harness_cost.review_line(root, out_dir)
                        or (f"Reviewed by {reviewer}" if reviewer else ""))
            computed = {
                # A colon, not a gap. The pill reads as one sentence — `🤖Fable 5
                # reviewer: 6 open, 4 fixed` — where before it was a label, a gap and a
                # row of numbers, which is the shape of a measurement rather than of a
                # statement. The robot is the page's own mark for "a model produced this",
                # the same one the inferred headings wear, and it is what makes the chip
                # legible as a claim by a machine rather than as another count of the diff.
                # With no name it is `🤖reviewer:`, not `🤖LLM review:`: the robot
                # already says a model did it, so those three letters only took the room
                # the chip's second sentence now needs. `reviewer`, an agent, to match the
                # `coder` the second half names — the two are the same kind of thing.
                # Every half computed. The chip used to read `auto-fixed <n>`, and the
                # label did the lying the tooltip then had to walk back: only three of the
                # twelve were fixed, and a reader who never hovers was told all twelve
                # were. No number here can drift from the lists behind it.
                #
                # Open leads, and the total is gone from the face: `12 raised` is the sum
                # of the two numbers beside it, so it is the one nobody acts on, while
                # `3 fixed` is the fact a reader cannot get anywhere else without opening
                # the tab. The order is the order of the work — what is left to do first,
                # what was already done for you second. The total is still one hover away.
                # `fixed`, not `auto-fixed`: how a fix arrived is the tooltip's business,
                # and the counts line under the tab's header keeps the longer word because
                # it has the width for it. The applied half is greyed: it is on the page so
                # the reader can check it, not so they can act on it, and at full contrast
                # it competes with the number that IS the work. Grey is the page's own
                # "already handled" — the same treatment the fixes get in the list below.
                #
                # Then a second clause, `🤖Code: 7 unsure;` — leading the pill — about a
                # different agent: the one that wrote the code, recording what it had to
                # guess at while writing it. It rides on this chip rather than on one of its own
                # because it answers the other half of "what did the machines do to this
                # branch", and because the masthead is a row, not a list. With nothing
                # assumed the clause is absent rather than zeroed — the rule the whole
                # chip lives by.
                #
                # One vocabulary now, not two. Read off `review-points.md` this used to
                # swap to `fixed, … declined`, which described the same review as the
                # counts line under the header in different words one scroll away — a
                # reader who compared the two was asked which of them to believe.
                # `scope_chip_face` reads `pile_numbers`, the same counts the counts
                # line itself reads, so neither can drift from the other again. It sets
                # the whole face, label included: the two agents it names are peers and
                # wear the same face and weight, which `label <b>value</b>` cannot give
                # them — that shape bolds everything after the first word, and the coder
                # then reads as a footnote to a bold `Review:`.
                #
                # Eval run 10: that face — `🤖Code: 6 unsure; 🤖Review: 10 open · 3
                # refuted, 6 fixed` — was the chip that wrapped the scope bar to a second
                # row. The counts stay; the agent names moved to the hover's first sentence
                # (`review_chip_key`), still off the same `pile_numbers`.
                "face": review_chip_face(open_n, fixed, assumed),
                # The total, which the face no longer carries, split by the pass that
                # raised each item. `by /code-review and /simplify` named the two passes
                # and left the reader to guess the split — which is the only thing the
                # hover could add, since the chip is already about a number. It is counted
                # off each item's own `source`, so the breakdown cannot disagree with the
                # stamps in the list below it.
                #
                # The coder's half gets the sentence the pill has no room for: who recorded
                # those assumptions, when, and where to go and read them. The face says
                # `🤖coder` and trusts the hover to unpack it.
                #
                # The model that reviewed opens the hover: the face says only `Review`,
                # so the name has one home and it is here.
                # Copy pass (3 Oct 2026): the per-reviewer split (`raised_by_reviewer`)
                # and "N assumptions the coding agent recorded while implementing" went:
                # the face counts them, the Review tab lists them, and neither changes
                # what a reviewer does next. Who reviewed is the one fact left.
                "tip": review_chip_key(open_n, fixed, assumed)
                + (f" {reviewed}." if reviewed else ""),
            }
            c = {**computed, **{k: v for k, v in c.items() if k != "auto"}}
        # A chip that has to be kept up to date by hand is a chip that will be wrong. The
        # cost of the run is the extreme case: it is still changing while the page is being
        # written, so it is computed here, at build time, and never typed into the content
        # file. `{"auto": "cost"}` is the whole declaration; label, value and tooltip all
        # come back from the script.
        # The same discipline for the test count: the page already parses every changed
        # test file to classify the rows under each requirement, so the number at the top
        # is read off that same manifest and cannot disagree with the list below it.
        # `files` and `lines` -- one measurement, two chips, so they can never end up
        # describing different ranges. `exclude` adds pathspecs to the generated-file list
        # this always applies; there is no way to turn that list off, because a diffstat
        # dominated by regenerated diagrams is not a stricter answer, it is a wrong one.
        if c.get("auto") == "diffstat":
            for computed in diffstat_chips(root, base_st, c.get("exclude"),
                                           spec.get("pr")):
                emit({**computed, **{k: v for k, v in c.items()
                                     if k not in ("auto", "exclude")}})
            continue

        if c.get("auto") == "tests":
            computed = tests_chip(test_doc)
            if computed is None:
                continue
            c = {**computed, **{k: v for k, v in c.items() if k != "auto"}}

        if c.get("auto") == "cost":
            # Dropped, not rendered and not an error. The cost is the last tab on the strip
            # now; a chip in the bar could only ever carry the review's own share of it,
            # which turned out to be the smaller half of what the reader wanted. Content
            # files in the wild still ask for the chip, and silence is the right answer to
            # them: the number they wanted is on the page, one pill further right.
            continue
        emit(c)
    chips = "".join(chips)

    # This used to require a chip per automated pass — /code-review hunts bugs, /simplify
    # shrinks the solution, so one number over both reports neither. The page now carries a
    # single merged chip instead, by decision, and the old check fired on every run: a
    # warning that is always on is a warning nobody reads.
    #
    # The concern behind it survives in a form the merged chip can actually fail. One number
    # over two questions is honest only while the split it summarises is still visible. That
    # split used to be a second tab with a chapter per pass; it is now the `source` stamp on
    # each item, which is a better home for it — the reader sees who raised a finding beside
    # the finding, rather than in a parallel listing that can fall out of step with this one.
    # So two things are checked: that the chip's target exists, and that the stamps are
    # actually there. A merged count with nothing behind it is the real regression.
    # A chip typed by hand (`files 25`, `gate green`) never gets this far: `own_layout`
    # drops every chip without an `auto` and names it on stderr — a typed number goes
    # stale with nothing noticing, and a typed claim says what the page already proves.

    if any(c.get("auto") == "autofixed" for c in scope):
        target = next((c.get("href", "") for c in scope if c.get("auto") == "autofixed"), "")
        anchor = target.lstrip("#")
        known = {tb.get("id") for tb in spec.get("tabs") or []} | \
                {sec.get("id") for sec in spec.get("sections", [])}
        if anchor and anchor not in known:
            print(f"[review] WARNING: the LLM-review chip deep-links to {target} but nothing "
                  "on the page has that id — the one number on the scope bar opens nothing",
                  file=sys.stderr)
        items = spec.get("findings", []) + spec.get("autofixes", [])
        if items and not any((i.get("source") or "").strip() for i in items):
            print("[review] WARNING: the chip merges /code-review and /simplify into one "
                  "number and not one item carries a 'source' — the split the chip "
                  "summarises is nowhere on the page behind it", file=sys.stderr)

    # The piles are rounds, in the order they happened: I, what the coder assumed while
    # implementing; II, what the code review raised (open) and fixed. So they render in
    # that order whatever order the content file lists them in — the file's own order used
    # to be enforced with a warning, and the page now simply puts them where they belong.
    _pile_rank = {"assumptions": 0, "findings": 1, "autofixes": 2}
    for _tb in spec.get("tabs") or []:
        _bl = _tb.get("blocks") or []
        _at = [i for i, b in enumerate(_bl) if b.get("type") in _pile_rank]
        for i, b in zip(_at, sorted((_bl[i] for i in _at), key=lambda b: _pile_rank[b["type"]])):
            _bl[i] = b

    v = spec.get("verdict")
    title_score = ""
    if v:
        n = int(v["score"])
        # The score belongs beside the title: it is the one thing a reader wants before
        # they have decided whether to read anything. The band below keeps the reasons.
        band = "v-good" if n >= 8 else ("v-mid" if n >= 5 else "v-bad")
        face = (f'<b>{n}</b><small>/10</small>'
                f'<i>{html.escape(v.get("label", ""))}</i>')
        # `5/10 not yet mergeable` states a conclusion and shows none of the reasoning, so
        # the click every reader tries on it is the one that goes to the findings. It is
        # not a decoration with a link bolted on: the tab it opens is found by *content* —
        # whichever tab renders the findings — so a page that arranges its tabs
        # differently still sends the score where its reasons actually are.
        target, target_label = _score_target(spec)
        title_score = (
            f'<a class="titlescore {band}" href="#{html.escape(target, quote=True)}" '
            f'data-tip="Why {n}/10? Open the {html.escape(target_label, quote=True)} tab">'
            f'{face}</a>'
            if target else f'<span class="titlescore {band}">{face}</span>')

    extra_css = "".join((out_dir / c).read_text(encoding="utf-8") for c in spec.get("extraCss", []))
    # The snippet extractor owns its own token colours, so the page asks it for them
    # rather than keeping a second copy that would drift from the highlighter.
    extra_css += subprocess.run(
        [sys.executable, str(EXTRACT), "--css"],
        capture_output=True,
        text=True,
        check=True,
        cwd=root,
    ).stdout
    # Same rule for the code-owners check: the block is rendered by the script, so the
    # content file never has to remember to list a stylesheet it does not own.
    if any(b.get("type") == "codeowners"
           for t in spec.get("tabs") or [] for b in t.get("blocks", [])):
        extra_css += subprocess.run(
            [sys.executable, str(CODEOWNERS), "--css"],
            capture_output=True, text=True, check=True, cwd=root,
        ).stdout

    dspec = spec.get("diagrams", {})
    manifest_rows = read_manifest(out_dir / dspec.get("manifest", "assets/diagrams/MANIFEST.tsv"))
    placed = set()

    def heading(block, fallback_id, fallback_title):
        title = block.get("title", fallback_title)
        if not title:
            return ""
        head = (f'<h2 id="{html.escape(block.get("id", fallback_id))}">'
                f'{html.escape(title)}</h2>')
        return head + (f'<p>{block["body"]}</p>' if block.get("body") else "")

    # A block can hang a badge on the tab that holds it — filled in per tab, below.
    auto_badge = {}
    # Whether any tab asked for the test ledger. If none did and there is a manifest, the
    # page appends it to the tab the manifest belongs to rather than dropping it: the
    # whole point of computing what happened to the tests is that a reviewer sees it, and
    # a content file written before this block existed must not silently lose it.
    placed_ledger: list[bool] = []
    reset_list()
    # What the Review tab owes the reader above its first heading. Both are facts about the
    # branch rather than about any pile, so they are set once here and drained by whichever
    # pile renders first.
    # The aftermath first: it is the louder statement and it governs how the piles under
    # it should be read. The missing-record band is second, directly above the piles it
    # explains.
    # The takeover note goes above the counts line itself: it says at which commit every
    # number on that line was counted — unless the aftermath band already lists the commits
    # it took over, read off git, which is the list the note was a frozen copy of.
    # No aftermath band any more (Victor, 7 Oct 2026): no judgement of what changed after
    # the review. The count rides on the masthead's branch chip (`7↑test-pr`); the
    # takeover note, which the band used to carry, is always drawn here now.
    set_bands([POINTS_MISSING_BAND if (spec.get("_reviewPoints") or {}).get("missing")
               else ""],
              top=[points_note_band(spec.get("_reviewPoints"), github_blob_base(root))])

    def render_block(block):
        """One block of a tab, as (html, weight, changes).

        `weight` is "is there anything at all to show" — a tab whose every block weighs
        nothing is dropped. `changes` is the narrower question "did *this branch* move
        anything here" — a tab that is all context and no delta is kept, and struck
        through on the strip. A picture of the current state is not a change; that is
        why `puml` and `codecity` carry weight but no changes."""
        kind = block.get("type", "section")
        if kind in PILE_BLOCKS:
            # The Review pill's number is the open pile, not this tab's render weight —
            # the module that owns the piles says which, and why (`review_tab_badge`).
            if kind == "findings":
                auto_badge.update(review_tab_badge(spec))
            return render_pile_block(spec, block, heading)
        if kind == "diagrams":
            # A block may name a manifest of its own. One producer does: the C2 view is
            # projected from the sequence diagrams rather than diffed out of a .puml that
            # changed, and it cannot file its row in `assets/diagrams/MANIFEST.tsv`
            # because `puml-diff.sh` does `rm -rf` on that whole directory every time it
            # runs — which the `sequence` step makes it do AFTER the `diagrams` step
            # wrote it. Its rows stay out of `placed` on purpose: `placed` answers "did
            # every row of the SHARED gallery find a tab", and a private manifest has no
            # orphans to warn about.
            own = block.get("manifest")
            source_rows = read_manifest(out_dir / own) if own else manifest_rows
            rows = [dict(r) for r in select_rows(source_rows, block)]
            if not own:
                placed.update(r["name"] for r in rows)
            # A diagram the block names is shown whether or not this branch changed it:
            # `puml-diff.sh` files only the ones whose text moved, and `only: ["DomainModel",
            # "DB"]` on a branch whose migration added nothing but indexes used to come out
            # as a Data tab with no DB on it. A name no row covers is drawn from its source
            # with the UNCHANGED badge — unless it is the block's `context`, which keeps its
            # own fallback below when nothing else in the block changed.
            context = block.get("context")
            have = {r["name"] for r in rows}
            for name in block.get("only") or []:
                if name in have:
                    continue
                if context and context.get("name") == name:
                    if rows:
                        rows.append(unchanged_row(name, context["src"]))
                    continue
                rel = find_diagram_source(name, root)
                if rel:
                    rows.append(unchanged_row(name, rel))
                else:
                    print(f"[review] diagrams block names {name!r}: no manifest row and no "
                          f"{name}.puml in the repository — nothing to show", file=sys.stderr)
            # What the picture cannot say. An ERD drawn from a schema dump is blind to an
            # index or a collation, so a schema that changed only there reads UNCHANGED;
            # the line under the card names it, and the card then counts as a change.
            merge_base = page_rev
            for r in rows:
                if r.get("kind") == "structural":
                    r["_unseen"] = schema_unseen_note(r["source"], root, merge_base,
                                                      columns=r["status"] == UNCHANGED)
            changed = sum(1 for r in rows if r["status"] != UNCHANGED or r.get("_unseen"))
            # Nothing of this family changed. A block that names a `context` diagram
            # (the Packages case: no delta, but the current package shape is still
            # worth showing) falls back to rendering it from source — exactly like a
            # standalone `puml` block, and `render_puml` never returns zero weight, not
            # even for a missing file. That is what makes a tab built on this one block
            # *reliably* struck-through-but-present rather than droppable: the guarantee
            # lives here, not in the discipline of remembering to pair it with a second
            # block that happens to always weigh 1.
            if not rows:
                if context:
                    return (heading(block, "diagrams", dspec.get("title", ""))
                            + render_puml(context, root, out_dir), 1, 0)
                return "", 0, 0
            merged = dict(dspec)
            # The rows are already filtered; `only` survives purely as the running order
            # the author asked for. Popping it here is what used to make
            # `only: ["DomainModel", "DB"]` come out alphabetical anyway.
            merged["only"] = block.get("only") or dspec.get("only") or []
            if own:
                # Every SVG a row names is resolved relative to its own manifest, so this
                # has to travel with the rows or the pictures 404 next to the gallery's.
                merged["manifest"] = own
            return (
                heading(block, "diagrams", dspec.get("title", ""))
                + render_diagrams(merged, root, out_dir, rows),
                len(rows), changed,
            )
        if kind == "testpairs":
            rows = [r for r in select_rows(manifest_rows, block) if r["kind"] == "sequence"]
            placed.update(r["name"] for r in rows)
            # Amber pill, the CODEOWNERS way, when the traced suites were not re-run or were
            # red: the band inside the tab says why, the colour says to open it.
            alarm = sequence_verdict_alarm(out_dir)
            if alarm:
                auto_badge["tabClass"] = "warn"
                auto_badge["label"] = alarm
            # The ledger, so a tagged picture of a test this branch also wrote says both.
            return render_testpairs(block, dspec, manifest_rows, root, out_dir,
                                    test_changes=test_doc.get("tests"))
        if kind == "logging":
            return logging_fragment(block, root, page_rev)
        if kind == "puml":
            return (heading(block, "puml", block.get("title", ""))
                    + render_puml(block, root, out_dir), 1, 0)
        if kind == "c4":
            # The repository's Structurizr views, drawn by Structurizr. Weight per view;
            # a change only where the DSL moved what a view shows.
            frag, n, moved = render_c4(block, root, out_dir)
            return heading(block, "c4", block.get("title", "")) + frag, n, moved
        if kind == "codeowners":
            frag, summary = codeowners_fragment(block, root, out_dir, page_rev)
            state, owned = summary["state"], summary["owned"]
            # No CODEOWNERS in the repository is not a finding, it is an absence: drop
            # the tab rather than teach the reviewer to ignore a permanent grey box.
            if state == "no_codeowners":
                print("[review] no CODEOWNERS file — dropping the code-owners tab",
                      file=sys.stderr)
                return "", 0, 0
            if state == "approval_required":
                auto_badge["tabClass"] = "alarm" if summary.get("severity") == "critical" else "warn"
                auto_badge["label"] = "approval required"
            # No default heading, for the reason `codecity` has none: the tab pill says
            # CODEOWNERS, its badge says "Code owners approval required", and the seal
            # under it says APPROVAL REQUIRED. A fourth `Code owners` above the first
            # filename is the label said again. An explicit `title` still renders.
            return (heading(block, "codeowners", "") + frag,
                    1, len(owned))
        if kind == "tests":
            frag, moved = render_test_ledger(test_doc.get("tests", []), root)
            placed_ledger.append(True)
            if not frag:
                return "", 0, 0
            # No default heading: the ledger's own summary line — `+56 new · −6 gone ·
            # ✍10 edited` — is its title, and the anchor (eval run 11). An explicit
            # `title` still renders.
            return (heading(block, "test-ledger-title", block.get("title", "")) + frag,
                    1, moved)
        if kind == "traces":
            touched = {(Path(t["path"]).name, t.get("line"))
                       for t in test_doc.get("tests", []) if t.get("status") != "unchanged"}
            frag, n = render_traces(traces_doc, root, out_dir, touched)
            if not frag:
                return "", 0, 0
            # No heading: the block is a registry the 🎭 on the covering-tests rows read,
            # not a thing to look at. Weight, and no changes — the same call `codecity`
            # and `puml` make. A trace is a recording of how the code behaves now; it is
            # evidence *about* the branch, not a thing the branch moved.
            return frag, n, 0
        if kind == "codecity":
            # A delta, unlike `puml`. The strike-through on a tab means "we looked and this
            # branch did not touch it", and a `puml` card earns it honestly: a context
            # diagram can be the same picture at both ends of the branch. This shot cannot
            # — the lit buildings *are* the classes the change set touched, so a city with
            # anything in it is a city this branch changed, and the tab was being struck
            # through over a picture whose whole subject is the change.
            return city_html, (1 if city_html else 0), (1 if city_html else 0)
        if kind == "section":
            body = by_id.get(block["id"])
            if body is None:
                raise SystemExit(f'[review] tab block references no section: {block["id"]}')
            # The API verdict band is red when the contract breaks (or the two differs
            # disagree): the pill goes red with it, the CODEOWNERS way.
            if block["id"] == "swaggerdiff" and 'class="apiverdict red"' in body:
                auto_badge["tabClass"] = "alarm"
                auto_badge["label"] = "breaking contract change"
            # The UX audit's header names the gaps this branch added (`⚠ +N gap`, the one
            # warning on that line): the pill goes amber with it, the CODEOWNERS way. A gap
            # the base already had is not this branch's and leaves the pill alone.
            if block["id"] == "ds-audit":
                hdr = re.search(r'<p class="dsa-hdr">.*?</p>', body, re.S)
                gap = hdr and re.search(r'class="dsa-gap"[^>]*>(?:⚠ )?\+(\d+) gap',
                                        hdr.group(0))
                if gap:
                    n = int(gap.group(1))
                    auto_badge["tabClass"] = "warn"
                    auto_badge["label"] = f"{n} new design-system gap{'' if n == 1 else 's'}"
            # A section is prose we wrote about the change, so it counts as a change
            # unless it declares itself context.
            return body, 1, 0 if unchanged_ids.get(block["id"]) else 1
        if kind == "html":
            has = 1 if block.get("html") else 0
            return block.get("html", ""), has, 0 if block.get("unchanged") else has
        raise SystemExit(f"[review] unknown tab block type: {kind}")

    tabs = spec.get("tabs")
    lede_html =f'<div class="lede">{spec.get("summary", "")}</div>' if spec.get("summary") else ""
    summary_html = lede_html
    overview_html = ""
    if tabs:
        # The summary and the verdict used to sit above the strip, which pushed the
        # questions below the fold on a laptop — a reviewer scrolled past the answers to
        # find out what the answers were. So they became a tab, and that was one move too
        # far: a tab is a question a reader chooses, and *"what is this change, and is it
        # mergeable"* is not chosen — it is what the page opens with. It bought a pill in
        # the strip, a click to leave, and a second click to come back for a summary
        # nobody returns to twice.
        #
        # They open the first tab instead, as its lede. The strip still lands in the first
        # screenful, the reader still reads the summary before anything else — and the tab
        # they are standing in when they finish it is the one they were going to open
        # next. It rides on `intro` rather than as a block, so it sits above the tab's own
        # preamble (the summary is about the change; an intro is about the tab) and, like
        # every intro, carries no weight: the panel it opens is kept alive by its own
        # content, never by the page's lede leaning on it.
        overview_html = lede_html
        summary_html = lede_html
        if overview_html:
            first, *rest = tabs
            tabs = [{**first, "intro": overview_html + first.get("intro", "")}, *rest]
            lede_html = ""
    # The ledger is derived data, like the requirement lists it sits under: nobody writes
    # it, and a content file that predates the block would otherwise leave the manifest
    # computed and unread. So a page that has a manifest and no `tests` block gets one,
    # appended to the tab the `tests` step feeds. Declaring the block explicitly is still
    # how you put it somewhere else in the panel — this only fills a gap, it never moves
    # a block the author placed.
    #
    # `"testLedger": false` turns the gap-filling off, and is for the one page shape that
    # does not have the gap: a Tests tab whose own card already lists every test the change
    # set moved — new, edited and deleted alike — which is what the requirements map does.
    # There the ledger is the same rows a second time, grouped by a question the stamps on
    # those rows already answer, and a reader made to hold two lists and diff them is a
    # reader the second list cost something. Off is a claim the author is making, so it is
    # said out loud rather than inferred: the build cannot read a hand-authored fragment
    # and know what is in it.
    if tabs and spec.get("testChanges") and not any(
        b.get("type") == "tests" for tab in tabs for b in tab.get("blocks", [])
    ):
        if spec.get("testLedger") is False:
            print("[review] testLedger:false — no ledger. Every test the change set moved "
                  "has to be listed on the page some other way, deleted ones included.",
                  file=sys.stderr)
        else:
            host = next((tab for tab in tabs if tab.get("id") == LEDGER_TAB), None)
            if host is None:
                print(f'[review] WARNING: there is a test manifest and no tab carries a '
                      f'"tests" block — and no tab is called {LEDGER_TAB!r} to append it '
                      "to, so what the change set did to the tests is on no page.",
                      file=sys.stderr)
            else:
                host["blocks"] = list(host.get("blocks", [])) + [{"type": "tests"}]

    # The recordings fill their gap the same way, and for the identical reason: the step
    # that harvests them runs whether or not a content file mentions them, and a run that
    # copied eleven traces onto disk for nobody to open has spent the reader's disk and
    # given them nothing. They go *under* the ledger — the ledger is what the branch did,
    # the recordings are what the run did, and that is the order the questions arrive in.
    if tabs and spec.get("playwrightTraces") and not any(
        b.get("type") == "traces" for tab in tabs for b in tab.get("blocks", [])
    ):
        host = next((tab for tab in tabs if tab.get("id") == LEDGER_TAB), None)
        if host is None:
            print("[review] WARNING: traces were harvested and no tab carries a "
                  f'"traces" block — and no tab is called {LEDGER_TAB!r} to append one '
                  "to, so the recordings are on no page.", file=sys.stderr)
        else:
            host["blocks"] = list(host.get("blocks", [])) + [{"type": "traces"}]

    # Only a tabbed page grows a masthead; the plain single-column guide keeps the
    # heading it always had.
    strip_html = allbtn_html = diskline_html = mode_html = rerun_fail_html = ""
    if tabs:
        # Measured once, for every tab, before the loop: one subprocess and one transcript
        # scan rather than one per tab. `led` is None only when review-cost.py itself
        # could not be asked; a tab's own entry inside it is never missing (see
        # `tab_cost_report`'s docstring) — a bad day comes back as a "not measured"
        # sentence, not as a tab silently getting no number at all.
        led = cost_ledger_report(root, [t["id"] for t in tabs],
                                 page_rev or (spec.get("pr") or {}).get("base") or "origin/main",
                                 out_dir)
        costs = (led or {}).get("tabs")
        strip, panels, dropped, quiet, emitted = [], [], [], [], []
        for tab in tabs:
            body, weight, changes = "", 0, 0
            auto_badge.clear()
            for block in tab.get("blocks", []):
                chunk, w, c = render_block(block)
                body += chunk
                weight += w
                changes += c
            if not weight and not tab.get("keepEmpty"):
                dropped.append(tab["label"])
                continue
            tid = html.escape(tab["id"])
            # A number on a tab is a promise that it means something. It does on the tab
            # holding the findings; on "Data model" it would just count pictures.
            # `count` from a block is a real count of things on the tab; `weight` is the
            # "is there anything at all to show" number this loop keeps or drops the tab
            # by, and a layout sentinel inside it (the assumptions pile weighs 1 even
            # empty) is not a quantity of anything. A tab that asked for a number and has
            # a block able to say what it counts gets that one.
            counted = auto_badge.get("count", weight)
            badge = (tab.get("badge") or auto_badge.get("badge")
                     or (str(counted) if tab.get("count") else ""))
            badge_class = tab.get("badgeClass") or (
                auto_badge.get("class", "") if not tab.get("badge") else "")
            # An alarm is a colour, not a mark: the tab's own label goes red rather than
            # growing a `!` beside it. That leaves the words it stands for with nowhere on
            # screen to live, so they go where a machine still finds them — the button's
            # `aria-label`, which has to restate the label too, because `aria-label`
            # replaces the accessible name rather than adding to it.
            badge_label = tab.get("badgeLabel") or (
                auto_badge.get("label", "") if not tab.get("badge") else "")
            # A class on the pill itself, for a fact about the whole tab rather than about
            # a number on it. `tabClass` in the content file overrides, the same way
            # `badgeClass` does.
            tab_class = tab.get("tabClass") or auto_badge.get("tabClass", "")
            count = (
                f'<span class="n{" " + html.escape(badge_class) if badge_class else ""}"'
                + (f' role="img" aria-label="{html.escape(badge_label)}"'
                   f' data-tip="{html.escape(badge_label[:1].upper() + badge_label[1:])}"'
                   if badge_label else "")
                + f'>{html.escape(badge)}</span>'
            ) if badge else ""
            # Struck through rather than dropped: the answer "we looked, and this branch
            # did not touch it" is worth as much to a reviewer as the answer that it did.
            # Not on a tab that is an overview rather than a delta (`NEVER_STRUCK`).
            still = (not changes and not tab.get("noStrike")
                     and not any(b.get("type") in NEVER_STRUCK for b in tab.get("blocks", [])))
            if still:
                quiet.append(tab["label"])
            # No `data-tip` on a tab header, on purpose. The strip used to carry two
            # sentences on hover — why a tab is struck through, and what it cost — and both
            # were removed: a hover hint on a pill is unfindable, and the strip is the one
            # part of the page a reviewer navigates by, not reads. Both facts still reach
            # the reader, elsewhere and visibly: the strike-through itself says the branch
            # did not touch that tab, and the cost moved into the breakdown the cost chip
            # opens (`cost_ledger_html`), where every tab's number can be read at once.
            strip.append(
                f'<button type="button" class="tab{" quiet" if still else ""}'
                f'{" " + html.escape(tab_class) if tab_class else ""}" role="tab" '
                f'id="tabbtn-{tid}" aria-controls="{tid}" aria-selected="false" tabindex="-1"'
                + (f' aria-label="{html.escape(tab["label"])} — {html.escape(badge_label)}"'
                   if tab_class and badge_label else "")
                + f'>{html.escape(SHORT_TAB_LABELS.get(tab["id"], tab["label"]))}{count}</button>'
            )
            # `intro` is prose about the *tab*, not about any one block in it — where the
            # data behind a whole panel came from, or what it deliberately does not say. It
            # is raw HTML and carries no weight: a tab is not kept alive by its own preamble.
            panels.append(
                f'<section class="panel" id="{tid}" role="tabpanel" '
                f'aria-labelledby="tabbtn-{tid}">'
                f'<p class="paneltag">{html.escape(tab["label"])}</p>'
                # The tab's presses, at the end of its own title rather than on the strip,
                # placed after the prompts so the prompts' anchors read the title unchanged.
                + place_tab_reruns(tab["id"], tab["label"],
                                   place_prompts(tab["id"], f'{tab.get("intro", "")}{body}'),
                                   tab_rerun_html(tab["id"], tab["label"],
                                                  tab_reruns.get(tab["id"])))
                + '</section>'
            )
            emitted.append(tab)
        # A diagram in the manifest that no tab claimed would vanish without a word —
        # the exact silent drop this pipeline exists to prevent.
        orphans = [r["name"] for r in manifest_rows if r["name"] not in placed]
        if orphans:
            print(f"[review] WARNING: no tab claims these changed diagrams: {', '.join(orphans)}",
                  file=sys.stderr)
        if dropped:
            print(f"[review] dropped empty tabs: {', '.join(dropped)}", file=sys.stderr)
        # The cost tab is the build's own, not the content file's, and it is appended
        # after every declared tab — the last pill on the strip, past CODEOWNERS. Two
        # reasons it cannot be declared: its label is a measured number (`$744`), and this
        # page's whole discipline is that a number nobody can keep up to date is a number
        # that will be wrong; and its position is a fact about the page rather than about
        # any one review — the bill goes at the end, where a bill goes.
        cost_tab_body = cost_ledger_html(led, emitted, voices_cost(out_dir))
        if cost_tab_body:
            # No decimals. `$744.18` on a tab pill invites reading the cents of a
            # list-price estimate whose error bars are the width of a whole session; `$744`
            # says the size, which is the only thing a label has room to say. The cents are
            # one click away, in the table the tab opens.
            # The pill says what the table says. Under the phase cut that is the phases'
            # own total, not the ledger's three overlapping measurements of one bill —
            # a `$703` pill over a `$207` table is the same contradiction as the footer's,
            # read first and by everyone.
            # A branch another agent wrote is not a cheap branch: its bill is simply not on
            # this disk. `$0` on the pill read as "free"; the `?` says the number is partial.
            # With the four components measured, the pill is their total (cost.py).
            cost_label = cost_pill_label(led)
            # While the number is partial the pill's accessible name says which share it
            # leaves out — the `?` alone told eval run 8's judges nothing. No `data-tip`:
            # tab headers stay bare (test_the_breakdown_did_not_come_back_as_a_tab_header_
            # tooltip); the same sentence opens the tab, visibly (cost.py).
            partial = (led.get("components") or {}).get("unmeasured")
            cost_name = html.escape(cost_pill_title(led) if partial else
                                    f"{cost_label} to write and review this change",
                                    quote=True)
            strip.append(
                f'<button type="button" class="tab" role="tab" id="tabbtn-{COST_TAB_ID}" '
                f'aria-controls="{COST_TAB_ID}" aria-selected="false" tabindex="-1" '
                f'aria-label="cost — {cost_name}">'
                f'{cost_label}</button>')
            panels.append(
                f'<section class="panel" id="{COST_TAB_ID}" role="tabpanel" '
                f'aria-labelledby="tabbtn-{COST_TAB_ID}">'
                '<p class="paneltag">Cost</p>'
                f'{place_prompts(COST_TAB_ID, cost_tab_body)}</section>')

        # The strip leaves the body: it belongs to the masthead now, and the masthead is
        # assembled around it below. `body_html` is the panels alone, which is what every
        # rewrite downstream of here (the tab count, the enumeration check) is about.
        strip_html = (
            '<div class="tabstrip" role="tablist" aria-label="Review sections">'
            + "".join(strip) + "</div>"
        )
        # The show-everything toggle is not part of the strip any more (see the CSS): it
        # is emitted at the foot of the page, centred on its own line under the footer's.
        # The label says what it does *next* and therefore has to change with the state,
        # which is what the pressed styling alone could no longer carry once the button
        # left the strip — down here there is nothing beside it to read the highlight
        # against.
        # And which of the two pages this is — served by scripts/serve-review.py, where
        # buttons run and recordings play in the page, or a static copy (a file, the zip,
        # Pages), where they copy their command and hand over a `show-trace` line. Every
        # control on the page already degrades on its own; this is the one place that
        # says which world the reader is in, so it goes in the title row, beside the
        # score: the two things a reader wants before pressing anything are how the
        # branch did and what this copy can do. (It sat in the footer for an evening; a
        # fact nobody scrolls down for is a fact nobody reads.) Emitted as static: the
        # probe in SERVER_JS promotes it, never the other way round.
        # The static badge is a button, and what it copies is the way out of static: one
        # line that starts the server on this directory and opens this page from it.
        # `serve-review.py` prints the URL it ends up serving on — the next free port when
        # :7654 is already serving another checkout — so the line has to *read* that URL
        # rather than assume it, which is what the `$(…)` is. Absolute paths on purpose:
        # this is a fact about the machine the page was built on, like the show-trace
        # command on every trace row, and a reader on another machine has the zip's own
        # README for the general recipe. And `exit` last: the server is detached
        # (`start_new_session`), so the terminal it was typed into has nothing left to do
        # and closes — only on success, so a failure stays on screen to be read.
        try:
            here = out_dir.resolve().relative_to(root.resolve())
        except ValueError:
            here = out_dir.resolve()
        serve_cmd = (f'cd {shlex.quote(str(root.resolve()))} && u="$('
                     f'{shlex.quote(str(HERE / "serve-review.py"))} {shlex.quote(str(here))}'
                     f' --page {shlex.quote(out_path.name)})" && (open "$u" 2>/dev/null'
                     ' || xdg-open "$u") && exit')
        # Two chips, not one. `static` only says what this copy is, so it has no hover;
        # `Serve` is the thing to do about it, and it is the one that explains why.
        # VSC first, hidden: only the served page can ask the editor bridges whether a
        # window is on the reviewed commit, so editor.js raises it there and colours it.
        mode_html = (
            '<button type="button" class="chip chip-vsc" id="hr-vsc" hidden>VSC</button>'
            '<span class="chip chip-mode" id="hr-mode">Static</span>'
            '<button type="button" class="chip chip-serve copycmd" id="hr-serve" '
            f'data-copy="{html.escape(serve_cmd, quote=True)}" '
            # Victor's words, verbatim: what the click does, then why anyone wants it.
            'data-tip="Copy terminal command to start this webpage via a backend so you '
            'click buttons for actions, not copy terminal commands.">'
            f'{CMD_COPY} Serve</button>')
        # And, on the served copy only, the way to make the page catch up with the
        # repository. Both pieces are constants above, so what the page carries is one
        # thing a test can read rather than a string assembled inside a 400-line function.
        # Two of them: the free one, and the same thing with the model's half in front of
        # it. Side by side and in that order, because the cheap answer is the one a reader
        # should reach first and the expensive one should be the deliberate second look.
        # ↺⏳ right after the ↺ — the same free verb, the slow one — and the paid one last.
        mode_html += RERUN_CHIP + rerun_tests_chip(rerun_tests) + RERUN_AI_CHIP
        rerun_fail_html = (rerun_progress_html(step_expectations(out_dir))
                       + RERUN_DONE + RERUN_FAIL + RERUN_AI_CONFIRM)
        # The one control at the foot, at the end of the closing sentence rather than on a
        # line of its own: the footer is two lines, and every line it spends is a line the
        # tab above it does not get.
        allbtn_html = (
            ' <button type="button" class="allbtn" aria-pressed="false" '
            'data-label-off="⇄ Single page" data-label-on="⇄ back to one tab at a time" '
            'data-tip="All tabs on one page (for \u2318F)">'
            "⇄ Single page</button>"
        )
        # Where this page sits on disk, on its own centred line under that sentence. The URL
        # bar says 127.0.0.1:7655 when it is served, which names a port and not a checkout —
        # and with three petclinic checkouts each building its own review.html, "which file
        # am I looking at?" has no other answer on screen. Absolute, like the Serve command,
        # because it is a fact about the machine that built the page.
        # Two controls, not one. The 📋 after the path copies it, served or not. The path
        # itself is plain text until the probe says this server can `open -R` it — then
        # editor.js turns it into a Finder link, the way it raises the VSC badge.
        diskline_html = (
            '<div class="diskline">'
            f'<span class="diskpath" id="hr-diskpath">{html.escape(str(out_path))}</span>'
            '<button type="button" class="diskcopy copycmd" '
            f'data-copy="{html.escape(str(out_path), quote=True)}" '
            'data-say="Copied the path of this page" aria-label="Copy the path" '
            f'data-tip="Copy this path">{CMD_COPY}</button></div>'
        )
        body_html = "\n".join(panels)
        # These two facts used to be appended to the page as a `<p class="sub">` — and the
        # append landed OUTSIDE every `<section class="panel">`, so the only element on the
        # page that no tab could hide sat under all eleven of them, restating a strike-
        # through the strip was already drawing three inches above it. Nothing in the
        # markup is a good home for it: a note about the strip belongs to the strip, and the
        # strip already says it (struck-through label, tooltip on hover; a dropped tab is
        # absent, which is the honest rendering of "nothing to show"). So it is said to the
        # build log, where the person assembling the page is the one who needs it.
        if quiet:
            print("[review] tabs kept as context (struck through, no delta): "
                  + ", ".join(quiet), file=sys.stderr)
        # Filled in from the tabs that survived, not from the tabs that were asked for: a
        # tab dropped for having nothing to show must not be counted in the walk-through
        # that promises the reader eleven of them.
        # The cost tab is deliberately absent from this list. `{{tabcount}}` and the
        # lede's walk-through are about the tabs that carry the review; requiring the
        # summary to also name `$744` would make every content file recite the page's own
        # furniture back at the reader.
        tab_labels = [t["label"] for t in emitted]
        body_html = body_html.replace(TAB_COUNT_TOKEN, spelled(len(tab_labels)))
        # The summary alone, not the whole overview: the walk-through is prose, and the
        # verdict beside it is a score and a label. Handed both, a page that dropped its
        # summary still arrives here with a non-empty string and gets warned that its
        # score dial forgot to name twelve tabs.
        check_tab_enumeration(summary_html, tab_labels)
    else:
        # No tab layout in the content file: the original single-column guide, unchanged.
        body_html = (
            '<h2 id="first">Requires human review</h2>\n'
            + render_findings(spec.get("findings", []))
            + f'\n<h2 id="diagrams">{html.escape(dspec.get("title", ""))}</h2>\n'
            + f'<p>{dspec.get("body", "")}</p>\n'
            + render_diagrams(dspec, root, out_dir)
            + f"\n{city_html}\n"
            + "".join(sections)
        )


    # The commits before the audited base that wrote this change's own OpenSpec documents
    # are the spec it was built against, not code nobody reviewed (eval run 10: b12c9bdb
    # listed among tooling commits). Marked here, off the Review tab's own reading
    # (`_before_range_commits`), so the masthead's list and the grade's reasons agree.
    if base_st and base_st.get("outside"):
        specs = {c["sha"]: c["spec"]
                 for c in _before_range_commits(spec, root, base_st.get("ref")) if c["spec"]}
        if specs:
            base_st = {**base_st, "outside": [{**c, "spec": specs.get(c["sha"])}
                                              for c in base_st["outside"]]}

    # The DB fixtures' colours and which one each E2E test starts from, read off the
    # project (`shared/fixtures.py`); nothing at all when it has no fixtures and no test
    # leans on the seed, and then neither tab draws a dot.
    fixtures_html = render_fixture_registry(root)
    fixtures_html = fixtures_html + "\n" + FIXTURES_JS if fixtures_html else ""
    doc = f"""<!doctype html>
<html lang="en"{commit_stamp(root)}><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(spec.get('title', 'Review guide'))}</title>
<link rel="icon" type="image/svg+xml" href="{FAVICON}">
{PAINT_HOLD_JS}
<style>{CSS}{FOOTER_CSS}{extra_css.rstrip()}
{LATE_CSS}{XREF_CSS}</style></head>
<body><div class="wrap">
{masthead_html(spec, mode_html + title_score, chips, strip_html, base_st)}
{rerun_fail_html}
{lede_html}

{body_html}
<footer><p class="footrow"><span>{_link_home(spec.get('footer', ''))}</span> {TAKEAWAY}{allbtn_html}</p>{diskline_html}</footer>
</div>
{SERVER_JS}
{CAPTION_JS}
{APP_ENV_JS}
{GENSEQ_JS}
{FOCUS_JS}
{DGM_VIEWS_JS}
{XREF_JS}
{EDITOR_JS}
{TRACE_JS}\n{SEQLINK_JS}\n{FOLDERS_JS}\n{SEQFOLD_JS}\n{SEQHEADS_JS}\n{HSCROLL_JS}\n{TABS_JS}\n{PAINT_RELEASE_JS}
{RERUN_JS}
{TIP_JS}
{explain_data()}
{EXPLAIN_JS}
{fixtures_html}
</body></html>
"""
    doc = code_xref.cross_link(doc)
    doc = open_links_in_new_tabs(doc)
    doc = one_tooltip_only(doc)
    doc = promote_traced(doc)
    check_baked_excerpts(doc)
    out_path.write_text(doc, encoding="utf-8")
    # After the page, so the manifest can never promise a verb for a build that failed to
    # write its own HTML — and every declaration is in by now, the register being filled
    # as the emitters run.
    write_actions(out_dir)
    print(f"[review] wrote {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
