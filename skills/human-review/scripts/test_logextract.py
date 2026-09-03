#!/usr/bin/env python3
"""Unit tests for logextract.py — run with `python3 -m unittest test_logextract -v`."""

import os
import unittest
from collections import defaultdict

import logextract

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "testdata", "logextract")
RULE_DIR = os.path.join(HERE, "ast-grep-rules")


def scan(*names, inherited=True):
    paths = [os.path.join(FIX, n) for n in names]
    hits, antis, stats = logextract.extract(paths, root=FIX, allow_inherited=inherited)
    by_file = defaultdict(list)
    for h in hits:
        by_file[h.file].append(h)
    return by_file, antis, stats


class Slf4jExplicitTest(unittest.TestCase):
    def test_explicit_loggerfactory_field(self):
        by, _, _ = scan("Slf4jExplicit.java")
        hits = by["Slf4jExplicit.java"]
        self.assertEqual(4, len(hits), [h.text for h in hits])
        self.assertEqual({"INFO", "DEBUG", "TRACE", "ERROR"}, {h.level for h in hits})
        self.assertTrue(all(h.flavour == "SLF4J" for h in hits))
        self.assertTrue(all(h.receiver == "LOG" for h in hits))
        self.assertTrue(all(h.confidence == "declared" for h in hits))

    def test_isTraceEnabled_is_not_a_hit(self):
        by, _, _ = scan("Slf4jExplicit.java")
        self.assertNotIn("isTraceEnabled", {h.method for h in by["Slf4jExplicit.java"]})

    def test_args_after_the_format_string_are_captured(self):
        by, _, _ = scan("Slf4jExplicit.java")
        h = next(h for h in by["Slf4jExplicit.java"] if h.method == "info")
        self.assertEqual('"Booking visit for owner {} pet {}"', h.format)
        self.assertEqual(["owner", "petId"], h.args)

    def test_raw_line_and_position(self):
        by, _, _ = scan("Slf4jExplicit.java")
        h = next(h for h in by["Slf4jExplicit.java"] if h.method == "info")
        self.assertEqual(8, h.line)
        self.assertEqual(9, h.column)
        self.assertIn("Booking visit for owner", h.raw_line)


class LombokTest(unittest.TestCase):
    def test_slf4j_annotation_injects_log(self):
        by, _, _ = scan("LombokSlf4j.java")
        hits = by["LombokSlf4j.java"]
        self.assertEqual(3, len(hits), [h.text for h in hits])
        self.assertTrue(all(h.flavour == "Lombok @Slf4j" for h in hits))
        self.assertTrue(all(h.confidence == "lombok" for h in hits))

    def test_this_log_qualified_receiver(self):
        by, _, _ = scan("LombokSlf4j.java")
        h = next(h for h in by["LombokSlf4j.java"] if h.method == "info")
        self.assertEqual("log", h.receiver)

    def test_error_with_trailing_throwable_argument(self):
        by, _, _ = scan("LombokSlf4j.java")
        h = next(h for h in by["LombokSlf4j.java"] if h.method == "error")
        self.assertEqual(["e.getMessage()", "e"], h.args)

    def test_every_lombok_flavour(self):
        by, _, _ = scan("LombokFlavours.java")
        hits = by["LombokFlavours.java"]
        self.assertEqual({"Lombok @Log4j2", "Lombok @CommonsLog", "Lombok @Log (JUL)",
                          "Lombok @Flogger", "Lombok @XSlf4j"},
                         {h.flavour for h in hits})
        self.assertEqual(5, len(hits), [(h.flavour, h.text) for h in hits])

    def test_flogger_fluent_chain_resolves_to_info(self):
        by, _, _ = scan("LombokFlavours.java")
        h = next(h for h in by["LombokFlavours.java"] if h.receiver == "logger")
        self.assertEqual("INFO", h.level)
        self.assertEqual('logger.atInfo().log("flogger %s", "x")', h.text)


class OtherFlavoursTest(unittest.TestCase):
    def test_log4j2(self):
        by, _, _ = scan("Log4j2Explicit.java")
        hits = by["Log4j2Explicit.java"]
        self.assertEqual(2, len(hits))
        self.assertTrue(all(h.flavour == "Log4j2" for h in hits))
        self.assertEqual({"FATAL", "DEBUG"}, {h.level for h in hits})

    def test_commons_logging(self):
        by, _, _ = scan("CommonsLogging.java")
        hits = by["CommonsLogging.java"]
        self.assertEqual(2, len(hits))
        self.assertTrue(all(h.flavour == "Commons Logging" for h in hits))

    def test_jul(self):
        by, _, _ = scan("JulExplicit.java")
        hits = by["JulExplicit.java"]
        self.assertEqual(3, len(hits))
        self.assertTrue(all(h.flavour == "JUL" for h in hits))
        self.assertEqual({"INFO", "ERROR", "LOG"}, {h.level for h in hits})


class InheritedTest(unittest.TestCase):
    def test_heuristic_tier_via_imports_when_the_base_class_is_out_of_scope(self):
        by, _, _ = scan("InheritedLogger.java")
        hits = by["InheritedLogger.java"]
        self.assertEqual(1, len(hits))
        self.assertEqual("heuristic", hits[0].confidence)
        self.assertEqual("Commons Logging", hits[0].flavour)

    def test_inherited_tier_can_be_switched_off(self):
        by, _, _ = scan("InheritedLogger.java", inherited=False)
        self.assertEqual([], by["InheritedLogger.java"])

    def test_cross_file_extends_resolves_a_protected_logger(self):
        """The child file has no logging import at all — only `extends`."""
        by, _, _ = scan("SpringBase.java", "SpringChild.java")
        hits = by["SpringChild.java"]
        self.assertEqual(1, len(hits))
        self.assertEqual("inherited", hits[0].confidence)
        self.assertEqual("Commons Logging", hits[0].flavour)
        self.assertIn("inherited from `SpringBase`", hits[0].evidence)

    def test_child_alone_finds_nothing(self):
        by, _, _ = scan("SpringChild.java")
        self.assertEqual([], by["SpringChild.java"])

    def test_private_base_field_is_not_inheritable(self):
        by, _, _ = scan("PrivateBase.java", "PrivateChild.java")
        self.assertEqual(1, len(by["PrivateBase.java"]))
        self.assertEqual([], by["PrivateChild.java"],
                         "a private logger in the base must not leak to the subclass")


class AnnotationOrderTest(unittest.TestCase):
    def test_slf4j_is_found_when_it_is_not_the_first_annotation(self):
        """Regression: a `constraints:` block on `has` does not backtrack past
        the first annotation, so @RestController used to shadow @Slf4j."""
        by, _, _ = scan("AnnotationOrder.java")
        hits = by["AnnotationOrder.java"]
        self.assertEqual(1, len(hits))
        self.assertEqual("lombok", hits[0].confidence)
        self.assertEqual("Lombok @Slf4j", hits[0].flavour)
        self.assertEqual(["criteria"], hits[0].args)


class InlineFactoryTest(unittest.TestCase):
    def test_receiver_is_the_factory_call_itself(self):
        by, _, _ = scan("InlineFactory.java")
        hits = by["InlineFactory.java"]
        self.assertEqual(2, len(hits), [h.text for h in hits])
        self.assertEqual({"SLF4J", "Commons Logging"}, {h.flavour for h in hits})
        self.assertEqual({"WARN", "ERROR"}, {h.level for h in hits})
        self.assertTrue(all("factory call" in h.evidence for h in hits))


class AntiPatternTest(unittest.TestCase):
    def test_console_calls_are_a_separate_category(self):
        by, antis, _ = scan("AntiPatterns.java")
        self.assertEqual([], by["AntiPatterns.java"], "console output must not be a logger hit")
        self.assertEqual({"System.out", "System.err", "printStackTrace"},
                         {a.kind for a in antis})
        self.assertEqual(4, len(antis))


class TrickyNegativesTest(unittest.TestCase):
    def test_math_log_is_not_a_logger(self):
        _, _, _ = scan("TrickyNegatives.java")
        by, _, _ = scan("TrickyNegatives.java")
        texts = [h.text for h in by["TrickyNegatives.java"]]
        self.assertNotIn("Math.log(2.0)", texts)

    def test_variable_named_log_that_is_not_a_logger(self):
        by, _, _ = scan("TrickyNegatives.java")
        texts = [h.text for h in by["TrickyNegatives.java"]]
        self.assertNotIn("log.log()", texts)
        self.assertNotIn('log.info("this is a ledger entry")', texts)

    def test_duration_log_is_not_a_logger(self):
        by, _, _ = scan("TrickyNegatives.java")
        self.assertNotIn("duration.log()", [h.text for h in by["TrickyNegatives.java"]])

    def test_no_hits_at_all_in_the_negative_fixture(self):
        by, _, _ = scan("TrickyNegatives.java")
        self.assertEqual([], by["TrickyNegatives.java"],
                         [h.text for h in by["TrickyNegatives.java"]])

    def test_timer_log_in_a_file_with_no_logging_import(self):
        by, _, _ = scan("DurationLog.java")
        self.assertEqual([], by["DurationLog.java"])


class DiffRestrictionTest(unittest.TestCase):
    def test_hunk_parsing(self):
        rng = logextract.HUNK_RE.match("@@ -1,3 +12,5 @@ class X {")
        self.assertEqual(("12", "5"), rng.groups())
        self.assertTrue(logextract.in_ranges(14, [(12, 16)]))
        self.assertFalse(logextract.in_ranges(11, [(12, 16)]))

    def test_pure_deletion_hunk_contributes_no_range(self):
        m = logextract.HUNK_RE.match("@@ -5,2 +4,0 @@")
        self.assertEqual("0", m.group(2))


class SymbolTableTest(unittest.TestCase):
    def test_root_receiver_strips_this_and_chains(self):
        self.assertEqual("log", logextract.root_receiver("this.log"))
        self.assertEqual("log", logextract.root_receiver("log.atInfo().addKeyValue(1)"))
        self.assertEqual("Math", logextract.root_receiver("Math"))
        self.assertIsNone(logextract.root_receiver('"literal"'))

    def test_symbols_are_reported_per_file(self):
        _, _, stats = scan("Slf4jExplicit.java", "LombokSlf4j.java")
        self.assertIn("LOG", {s["name"] for s in stats["symbols"]["Slf4jExplicit.java"]})
        self.assertIn("log", {s["name"] for s in stats["symbols"]["LombokSlf4j.java"]})


class RuleFilesTest(unittest.TestCase):
    """`ast-grep-rules/` is the same eleven rules as a standalone scan config.

    logextract.py stays dependency-free by carrying its rules as strings and writing them
    to a temp dir per run, so anyone who wants to run `ast-grep scan` against them by hand
    — or drop them into a project's own `.ast-grep/rules/` — needs a copy on disk. Two
    copies of the same YAML is exactly the arrangement that silently rots, so the build of
    the copy is checked against the original here rather than trusted."""

    def test_every_embedded_rule_has_a_file_and_they_agree(self):
        for rid, text in logextract.RULES.items():
            with self.subTest(rule=rid):
                path = os.path.join(RULE_DIR, rid + ".yml")
                self.assertTrue(os.path.isfile(path), path + " is missing")
                with open(path, encoding="utf-8") as fh:
                    self.assertEqual(text.strip(), fh.read().strip())

    def test_no_rule_file_is_orphaned(self):
        on_disk = {f[:-4] for f in os.listdir(RULE_DIR) if f.endswith(".yml")}
        self.assertEqual(set(logextract.RULES), on_disk)


if __name__ == "__main__":
    unittest.main()
