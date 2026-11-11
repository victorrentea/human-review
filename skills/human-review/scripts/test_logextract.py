"""logextract.py: the Logging tab's scan, over Python.

Fixtures in testdata/logextract/ — one module per thing a logger can be, and one of
things that only look like one."""
from __future__ import annotations

import os
import subprocess
from collections import defaultdict
from pathlib import Path

import logextract

HERE = Path(__file__).resolve().parent
FIX = HERE / "testdata" / "logextract"


def scan(*names, inherited=True):
    hits, antis, stats = logextract.extract([str(FIX / n) for n in names], root=str(FIX),
                                            allow_inherited=inherited)
    by_file = defaultdict(list)
    for h in hits:
        by_file[h.file].append(h)
    return by_file, antis, stats


# --- which names are loggers ------------------------------------------------------------

def test_a_powertools_logger_and_every_level_called_on_it():
    by, _, _ = scan("powertools_explicit.py")
    hits = by["powertools_explicit.py"]
    assert [h.level for h in hits] == ["INFO", "DEBUG", "DEBUG", "ERROR"]
    assert {h.flavour for h in hits} == {"Powertools"}
    assert {h.receiver for h in hits} == {"LOG"} and {h.confidence for h in hits} == {"declared"}


def test_a_level_check_is_not_a_log_statement():
    by, _, _ = scan("powertools_explicit.py", "tricky_negatives.py")
    assert "isEnabledFor" not in {h.method for hs in by.values() for h in hs}


def test_the_message_and_the_values_after_it_are_split():
    by, _, _ = scan("powertools_explicit.py")
    h = by["powertools_explicit.py"][0]
    assert h.format == '"Booking visit for owner %s pet %s"'
    assert h.args == ["owner", "pet_id"]
    assert h.arg_types == ["str", "int"], "a parameter's annotation is its type hint"
    assert (h.line, h.column, h.method_start) == (8, 9, 7)
    err = by["powertools_explicit.py"][-1]
    assert err.args == ["exc_info=True"], "keywords are logged values too"


def test_the_stdlib_logger_under_any_alias_and_the_root_logger():
    by, _, _ = scan("stdlib_logging.py")
    hits = by["stdlib_logging.py"]
    assert [(h.receiver, h.level) for h in hits] == [
        ("log", "WARN"), ("other", "FATAL"), ("logging", "INFO"), ("log", "LOG"),
        ("log", "ERROR")]
    assert {h.flavour for h in hits} == {"logging"}
    via_log = next(h for h in hits if h.method == "log")
    assert via_log.format == '"via log() %s"', "log(level, msg): the message is the second"


def test_a_logger_imported_from_a_scanned_module_is_resolved_through_it():
    by, _, _ = scan("imported.py", "shared")
    hits = {h.format: h for h in by["imported.py"]}
    mine = hits['"imported from a scanned module"']
    assert (mine.flavour, mine.confidence) == ("Powertools", "imported")
    assert "shared.log" in mine.evidence


def test_a_logger_imported_from_outside_the_scan_is_taken_on_its_name_and_says_so():
    by, _, _ = scan("imported.py", "shared")
    outside = next(h for h in by["imported.py"] if h.receiver == "shared_logger")
    assert outside.confidence == "heuristic" and "outside the scan" in outside.evidence
    by, _, _ = scan("imported.py", "shared", inherited=False)
    assert [h.receiver for h in by["imported.py"]] == ["logger"]


def test_class_attributes_instance_attributes_and_annotated_parameters():
    by, _, _ = scan("class_attr.py")
    got = {(h.receiver, h.flavour) for h in by["class_attr.py"]}
    assert got == {("self.logger", "Powertools"), ("self.audit", "logging"),
                   ("cls.logger", "Powertools"), ("logger", "Powertools")}


def test_structlog_and_loguru():
    by, _, _ = scan("other_libraries.py")
    assert {(h.receiver, h.flavour) for h in by["other_libraries.py"]} == {
        ("slog", "structlog"), ("logger", "loguru")}


def test_things_that_only_look_like_logging_are_not_hits():
    by, _, _ = scan("tricky_negatives.py")
    assert by["tricky_negatives.py"] == []


def test_console_output_is_collected_apart_and_never_counted_as_logging():
    by, antis, _ = scan("antipatterns.py")
    assert by["antipatterns.py"] == []
    assert [a.kind for a in antis] == ["print", "print_exc", "sys.stderr"]


def test_the_libraries_the_tab_names_are_the_ones_recognised():
    assert set(logextract.LIBRARIES) == {"logging", "aws_lambda_powertools", "structlog", "loguru"}


# --- data flow ----------------------------------------------------------------------------

def test_a_logged_value_points_back_to_where_it_was_bound():
    by, _, _ = scan("data_flow.py")
    info = next(h for h in by["data_flow.py"] if h.level == "INFO")
    assert [(o["name"], o["kind"], o["line"]) for o in info.origins] == [
        ("total", "local", 8), ("customer_id", "param", 6)]
    debug = next(h for h in by["data_flow.py"] if h.level == "DEBUG")
    assert [(o["name"], o["line"]) for o in debug.origins] == [("item", 9)]


# --- what Ludvig's standard asks to look at twice -------------------------------------

def test_concerns_name_personal_data_whole_payloads_org_numbers_and_f_strings():
    by, _, _ = scan("concerns.py")
    got = [[(c["kind"], c["name"]) for c in h.concerns] for h in by["concerns.py"]]
    assert got == [
        [("pii", "email")],
        [("whole-object", "payload")],
        [("whole-object", "body"), ("f-string", "")],
        [("org-number", "orgNumber"), ("org-number", "org_number")],
        [],
    ]
    assert all(c["why"] for h in by["concerns.py"] for c in h.concerns)


# --- the diff -------------------------------------------------------------------------

def _repo(tmp_path, before: str, after: str) -> Path:
    r = tmp_path / "r"
    r.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=r, check=True, capture_output=True)
    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (r / "m.py").write_text(before)
    run("add", "-A")
    run("commit", "-qm", "base")
    run("tag", "base")
    (r / "m.py").write_text(after)
    return r


BEFORE = ('import logging\nlog = logging.getLogger()\n\n\ndef f(x):\n'
          '    log.warning("Validation failed: %s", x)\n    return x\n')


def _changed(repo):
    hits, _, _ = logextract.extract([str(repo)], root=str(repo))
    rng = logextract.changed_ranges(str(repo), "base")
    keep = [h for h in hits if logextract.in_ranges(h.line, rng.get(h.file, []))]
    logextract.pair_with_old_calls(keep, logextract.changed_hunks(str(repo), "base"), str(repo))
    return keep


def test_a_new_statement_is_added(tmp_path):
    repo = _repo(tmp_path, BEFORE, BEFORE.replace("    return x\n",
                                                   '    log.info("done %s", x)\n    return x\n'))
    [h] = _changed(repo)
    assert (h.method, h.change) == ("info", "added")


def test_a_literal_swapped_for_its_constant_is_rewritten_and_logs_the_same_text(tmp_path):
    after = ('import logging\nlog = logging.getLogger()\nVALIDATION_FAILED = "Validation failed: %s"\n'
             '\ndef f(x):\n    log.warning(VALIDATION_FAILED, x)\n    return x\n')
    repo = _repo(tmp_path, BEFORE, after)
    [h] = _changed(repo)
    assert (h.change, h.same_output) == ("modified", True)
    assert h.was == 'log.warning("Validation failed: %s", x)'


def test_changed_arguments_are_rewritten_but_not_the_same_output(tmp_path):
    repo = _repo(tmp_path, BEFORE, BEFORE.replace('%s", x)', '%s", x * 2)'))
    [h] = _changed(repo)
    assert (h.change, h.same_output) == ("modified", False)


def test_the_cli_writes_the_json_the_tab_reads(tmp_path):
    repo = _repo(tmp_path, BEFORE, BEFORE.replace("    return x\n",
                                                   '    print(x)\n    log.info("x")\n    return x\n'))
    out = tmp_path / "logging.json"
    assert logextract.main([".", "--repo", str(repo), "--root", str(repo), "--since", "base",
                            "--json", str(out)]) == 0
    import json
    doc = json.loads(out.read_text())
    assert set(doc) == {"all", "changed", "stats"}
    assert [h["method"] for h in doc["changed"]["logging"]] == ["info"]
    assert [a["kind"] for a in doc["changed"]["antipattern"]] == ["print"]
    assert doc["stats"]["files_with_loggers"] == 1
