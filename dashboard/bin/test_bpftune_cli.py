#!/usr/bin/env python3
"""Unit tests for bpftune-cli.py.

Tests the most-patched functions per the cleanup brief:
  - _label_for() (brief item #1: "the most-patched function")
  - _extract_dest() (the v6 dest6b truncation fix)
  - _parse_plain_map() (the plain-text bpftool fallback)
  - _swaps_mets_srates() + data_swap_outcomes() (the data pipeline)

Run:
    python3 /opt/bpftune-dashboard/bin/test_bpftune_cli.py

Or against a non-default path:
    BPFTUNE_CLI_PATH=/path/to/bpftune-cli.py python3 test_bpftune_cli.py

Exit code 0 = all pass.  Non-zero = failures (printed to stderr).

The test file is hermetic: it replaces the loader functions (_load_labels,
_load_fold, _load_aliases_labels) with lambdas returning mock data, so it
doesn't need /var/lib/bpftune/ or /etc/bpftune/ to exist.  It uses a mock
BPF log for the data pipeline tests.
"""
import importlib.util
import os
import sys
import unittest

CLI_PATH = os.environ.get(
    "BPFTUNE_CLI_PATH",
    "/opt/bpftune-dashboard/bin/bpftune-cli.py",
)


def _load_cli():
    """Load bpftune-cli.py as a module despite the hyphen in its name."""
    spec = importlib.util.spec_from_file_location("bpftune_cli", CLI_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ----- mock BPF log lines (match the regex in _swaps_mets_srates) -----

MOCK_LOG = "\n".join([
    "99.0: bpf_trace_printk: met cookie=1 rport=443 alg=1 segs=5 val=1000",
    "99.5: bpf_trace_printk: srate cookie=1 alg=1 srate=1000000",
    "100.0: bpf_trace_printk: swap cookie=1 from=0 to=1 bc=5 ac=5 d=0 "
    "mt=0 rb=1 dest=1378604897",
    "110.0: bpf_trace_printk: swap cookie=2 from=1 to=2 bc=5 ac=5 d=0 "
    "mt=1 rb=2 dest=0 dest6=2882400820",
    "120.0: bpf_trace_printk: swap cookie=3 from=2 to=3 bc=5 ac=5 d=0 "
    "mt=2 rb=3 dest=0 dest6=2882400820 dest6b=1450798573",
    "104.0: bpf_trace_printk: met cookie=1 rport=443 alg=1 segs=20 val=500",
    "108.0: bpf_trace_printk: srate cookie=1 alg=1 srate=2000000",
    "130.0: bpf_trace_printk: proof cookie=1 alg=1 rate=5000000 tier=2",
    "131.0: bpf_trace_printk: midsamp cookie=1 thr=0 rb=1 srate=1500000",
]) + "\n"


class TestExtractDest(unittest.TestCase):
    """Tests _extract_dest(row) — the v6 dest6b truncation fix."""

    @classmethod
    def setUpClass(cls):
        cls.cli = _load_cli()

    def test_v4_simple(self):
        row = (100.0, 1, 0, 1, 5, 5, 0, None, None, "1378604897", None, None, "fake")
        self.assertEqual(self.cli._extract_dest(row), "82.43.215.97")

    def test_v4_zero_dest_returns_none(self):
        row = (100.0, 1, 0, 1, 5, 5, 0, None, None, "0", None, None, "fake")
        self.assertIsNone(self.cli._extract_dest(row))

    def test_v4_none_dest_returns_none(self):
        row = (100.0, 1, 0, 1, 5, 5, 0, None, None, None, None, None, "fake")
        self.assertIsNone(self.cli._extract_dest(row))

    def test_v6_short(self):
        row = (100.0, 1, 0, 1, 5, 5, 0, None, None, None, "2882400820", None, "fake")
        self.assertEqual(self.cli._extract_dest(row), "abcd:f234::")

    def test_v6_long(self):
        row = (100.0, 1, 0, 1, 5, 5, 0, None, None, None,
               "2882400820", "1450798573", "fake")
        self.assertEqual(self.cli._extract_dest(row), "abcd:f234:5679:6ded::")

    def test_v6_dest6_zero_falls_back_to_v4(self):
        row = (100.0, 1, 0, 1, 5, 5, 0, None, None,
               "1378604897", "0", None, "fake")
        self.assertEqual(self.cli._extract_dest(row), "82.43.215.97")

    def test_v6_wins_over_v4_when_both_present(self):
        row = (100.0, 1, 0, 1, 5, 5, 0, None, None,
               "1378604897", "2882400820", None, "fake")
        self.assertEqual(self.cli._extract_dest(row), "abcd:f234::")

    def test_v6_long_malformed_dest6b_falls_back_to_short(self):
        row = (100.0, 1, 0, 1, 5, 5, 0, None, None, None,
               "2882400820", "not-a-number", "fake")
        self.assertEqual(self.cli._extract_dest(row), "abcd:f234::")

    def test_short_row_returns_none(self):
        row = (100.0, 1, 0, 1, 5, 5, 0, None, None)
        self.assertIsNone(self.cli._extract_dest(row))

    def test_v4_malformed_int_returns_none(self):
        row = (100.0, 1, 0, 1, 5, 5, 0, None, None, "garbage", None, None, "fake")
        self.assertIsNone(self.cli._extract_dest(row))


class TestLabelFor(unittest.TestCase):
    """Tests _label_for() — brief item #1: "the most-patched function".

    Replaces the loader functions with lambdas returning mock data so
    tests are hermetic — no file reads needed.
    """

    @classmethod
    def setUpClass(cls):
        cls.cli = _load_cli()

    def setUp(self):
        self._orig_ll = self.cli._load_labels
        self._orig_lf = self.cli._load_fold
        self._orig_lal = self.cli._load_aliases_labels
        self.cli._load_labels = lambda: {
            "82.43.0.0": "home-bucket",
            "abcd:f234::": "v6-peer",
        }
        self.cli._load_fold = lambda: {
            "v6:2603c020": "89.168.0.0",
        }
        self.cli._load_aliases_labels = lambda: {
            "162.120.232.10": "vps-peer",
        }

    def tearDown(self):
        self.cli._load_labels = self._orig_ll
        self.cli._load_fold = self._orig_lf
        self.cli._load_aliases_labels = self._orig_lal

    def test_empty_addr_returns_empty(self):
        self.assertEqual(self.cli._label_for(""), "")

    def test_none_addr_returns_none(self):
        self.assertIsNone(self.cli._label_for(None))

    def test_v4_with_label(self):
        self.assertEqual(self.cli._label_for("82.43.215.97"), "home-bucket")

    def test_v4_no_label_returns_bucket(self):
        self.assertEqual(self.cli._label_for("1.2.3.4"), "1.2.0.0")

    def test_v6_short_with_label(self):
        self.assertEqual(self.cli._label_for("abcd:f234::"), "v6-peer")

    def test_v6_bucket_form_with_label(self):
        result = self.cli._label_for("v6:abcd1234")
        self.assertEqual(result, "v6:abcd1234")

    def test_v6_no_label_returns_normalized(self):
        result = self.cli._label_for("1234:5678::")
        self.assertEqual(result, "1234:5678::")

    def test_folded_v6_to_v4(self):
        result = self.cli._label_for("v6:2603c020")
        self.assertEqual(result, "89.168.0.0")


class TestParsePlainMap(unittest.TestCase):
    """Tests _parse_plain_map() — the plain-text bpftool fallback parser."""

    @classmethod
    def setUpClass(cls):
        cls.cli = _load_cli()

    def test_empty_input_returns_empty_list(self):
        self.assertEqual(self.cli._parse_plain_map(""), [])

    def test_v4_entry(self):
        out = (
            "[{\n"
            "    key:\n"
            "    00 00 00 00 00 00 00 00 00 00 ff ff 0a 00 00 01\n"
            "    value:\n"
            "    instances 5  min_rtt 1000  max_rate_delivered 50000\n"
            "}]\n"
        )
        entries = self.cli._parse_plain_map(out)
        self.assertEqual(len(entries), 1)
        inst, addr, v = entries[0]
        self.assertEqual(inst, 5)
        self.assertEqual(addr, "10.0.0.1")
        self.assertEqual(v.get("min_rtt"), 1000)

    def test_v6_entry(self):
        out = (
            "[{\n"
            "    key:\n"
            "    ab cd 12 34 00 00 00 00 00 00 00 00 00 00 00 00\n"
            "    value:\n"
            "    instances 3\n"
            "}]\n"
        )
        entries = self.cli._parse_plain_map(out)
        self.assertEqual(len(entries), 1)
        inst, addr, v = entries[0]
        self.assertEqual(inst, 3)
        self.assertEqual(addr, "v6:abcd1234")

    def test_multiple_entries(self):
        out = (
            "[{\n"
            "    key:\n"
            "    00 00 00 00 00 00 00 00 00 00 ff ff 0a 00 00 01\n"
            "    value:\n"
            "    instances 5\n"
            "},{\n"
            "    key:\n"
            "    00 00 00 00 00 00 00 00 00 00 ff ff 0a 00 00 02\n"
            "    value:\n"
            "    instances 10\n"
            "}]\n"
        )
        entries = self.cli._parse_plain_map(out)
        self.assertEqual(len(entries), 2)

    def test_malformed_value_still_parses(self):
        out = (
            "[{\n"
            "    key:\n"
            "    00 00 00 00 00 00 00 00 00 00 ff ff 0a 00 00 01\n"
            "    value:\n"
            "    instances garbage\n"
            "}]\n"
        )
        entries = self.cli._parse_plain_map(out)
        self.assertEqual(len(entries), 1)
        inst, addr, v = entries[0]
        self.assertEqual(inst, 0)


class TestDataPipeline(unittest.TestCase):
    """Tests the data pipeline against a mock BPF log."""

    @classmethod
    def setUpClass(cls):
        cls.cli = _load_cli()

    def setUp(self):
        self.cli._SWMS_CACHE = {}

    def test_swaps_mets_srates_parses_mock_log(self):
        sw, met, srate = self.cli._swaps_mets_srates(MOCK_LOG)
        self.assertEqual(len(sw), 3)
        self.assertEqual(len(met), 1)
        self.assertEqual(len(met[1]), 2)
        self.assertEqual(len(srate), 1)
        self.assertEqual(len(srate[1]), 2)

    def test_extract_dest_on_parsed_swap_rows(self):
        sw, _, _ = self.cli._swaps_mets_srates(MOCK_LOG)
        self.assertEqual(self.cli._extract_dest(sw[0]), "82.43.215.97")
        self.assertEqual(self.cli._extract_dest(sw[1]), "abcd:f234::")
        self.assertEqual(self.cli._extract_dest(sw[2]), "abcd:f234:5679:6ded::")

    def test_outcome_composite_win(self):
        sw, met, _ = self.cli._swaps_mets_srates(MOCK_LOG)
        o = self.cli._outcome_composite(met, 1, 100.0)
        self.assertEqual(o, "win")

    def test_outcome_srate_win(self):
        sw, _, srate = self.cli._swaps_mets_srates(MOCK_LOG)
        o = self.cli._outcome_srate(srate, 1, 100.0)
        self.assertEqual(o, "win")

    def test_data_swap_outcomes_returns_expected_counts(self):
        result = self.cli.data_swap_outcomes(MOCK_LOG)
        comp = result["composite"]
        self.assertEqual(comp["measurable"], 1)
        self.assertEqual(comp["win"], 1)
        self.assertEqual(comp["unmeasurable"], 2)
        self.assertEqual(len(result["swaps_list"]), 3)

    def test_data_recent_swaps(self):
        rows = self.cli.data_recent_swaps(MOCK_LOG, n=10)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[-1]["to_alg"], "dctcp")

    def test_proof_events(self):
        events, samples = self.cli._proof_events(MOCK_LOG)
        self.assertEqual(len(events), 1)
        self.assertIn(1, events)
        self.assertEqual(events[1]["proved"], 1)

    def test_data_proof(self):
        rows = self.cli.data_proof(MOCK_LOG)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["alg"], "bbr")
        self.assertEqual(rows[0]["proved"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
