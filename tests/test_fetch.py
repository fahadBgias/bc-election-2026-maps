"""Tests for the results script. Run from the repo root: python -m unittest discover tests"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import fetch_results as fr  # noqa: E402

SAMPLE = (ROOT / "tests" / "fixtures" / "enr_2024_sample.html").read_text(encoding="utf-8")
CONTAINER = "https://example.test/electionsbcenr"
LISTING = """<?xml version="1.0" encoding="utf-8"?><EnumerationResults><Blobs>
<Blob><Name>electionresults.css</Name><Properties><Last-Modified>Tue, 20 Oct 2026 23:21:17 GMT</Last-Modified></Properties></Blob>
<Blob><Name>Results_7200_GE-2026-10-24_Candidate.html</Name><Properties><Last-Modified>Sun, 25 Oct 2026 03:05:00 GMT</Last-Modified></Properties></Blob>
<Blob><Name>Results_7201_GE-2026-10-24_Candidate.html</Name><Properties><Last-Modified>Sun, 25 Oct 2026 03:40:00 GMT</Last-Modified></Properties></Blob>
<Blob><Name>Results_7201_GE-2026-10-24_Party.html</Name><Properties><Last-Modified>Sun, 25 Oct 2026 03:40:00 GMT</Last-Modified></Properties></Blob>
</Blobs><NextMarker /></EnumerationResults>"""
GEOMETRY = {"names": {"ABM": "Abbotsford-Mission", "ABS": "Abbotsford South", "ABW": "Abbotsford West",
                      "BDS": "Boundary-Similkameen"}}


def fake_get(pages: dict[str, str]):
    def get(url: str, retries: int = 3) -> bytes:
        for prefix, body in pages.items():
            if url.startswith(prefix):
                return body.encode("utf-8")
        raise fr.FeedError(f"404 {url}")
    return get


def run(data_dir: Path, pages: dict[str, str]) -> tuple[int, str]:
    buf = io.StringIO()
    with mock.patch.object(fr, "http_get", fake_get(pages)), redirect_stdout(buf):
        code = fr.main(["--container", CONTAINER, "--data-dir", str(data_dir)])
    return code, buf.getvalue()


class ParseTests(unittest.TestCase):
    def test_reads_the_2024_page_layout(self):
        p = fr.parse_page(SAMPLE)
        self.assertEqual(p["asOf"], "As of October 21, 2024 16:00 PST")
        self.assertEqual(p["updated"], "2024-10-21T23:00:00Z")  # Pacific local time
        self.assertEqual(p["count"]["initial"], "Complete")
        self.assertEqual(p["totals"], {"valid": 2038787, "considered": None, "registered": 3550017})
        ndp = next(x for x in p["parties"] if x["name"] == "BC NDP")
        self.assertEqual((ndp["leading"], ndp["votes"], ndp["pct"], ndp["colour"]), (46, 909277, 44.6, "#ff6633"))
        am = p["districts"]["Abbotsford-Mission"]
        self.assertEqual(am["candidates"], [["Pam Alexis", "BC NDP", 10560], ["Reann Gasper", "Conservative Party", 13254]])
        self.assertEqual(am["total"], 23814)
        self.assertEqual(am["boxes"], {"advance": [5, 5], "final": [16, 16]})
        self.assertEqual(am["notes"], [["Out-of-district ballots", "Complete"]])

    def test_reads_the_2026_page_layout(self):
        # 2026: no pin-button column, and each district's totals are separate labelled rows
        p = fr.parse_page((ROOT / "tests" / "fixtures" / "enr_2026_sample.html").read_text(encoding="utf-8"))
        self.assertEqual(p["updated"], "2026-10-25T03:13:00Z")
        self.assertEqual(p["count"]["initial"], "In Progress")
        self.assertEqual(p["totals"]["considered"], 4268)
        am = p["districts"]["Abbotsford-Mission"]
        self.assertEqual(len(am["candidates"]), 5)
        self.assertNotIn("Total Valid Votes", [c[0] for c in am["candidates"]])
        self.assertEqual(am["total"], 1080)
        self.assertEqual(am["boxes"], {"advance": [2, 5], "final": [0, 75]})
        self.assertEqual(p["districts"]["Abbotsford South"]["total"], 0)
        onebc = next(x for x in p["parties"] if x["name"] == "OneBC")
        self.assertEqual((onebc["leading"], onebc["votes"]), (1, 4))

    def test_names_match_despite_dash_and_case_differences(self):
        self.assertEqual(fr.norm("Abbotsford – Mission"), fr.norm("abbotsford-mission"))
        self.assertEqual(fr.norm("Saanich North and  the Islands"), fr.norm("Saanich North and the Islands"))


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        (self.data / "geometry.json").write_text(json.dumps(GEOMETRY))

    def tearDown(self):
        self.tmp.cleanup()

    def test_waits_quietly_before_polls_close(self):
        empty = LISTING.split("<Blob><Name>Results_")[0] + "</Blobs><NextMarker /></EnumerationResults>"
        code, log = run(self.data, {CONTAINER + "?": empty})
        self.assertEqual(code, 0)
        self.assertIn("Waiting", log)
        self.assertEqual(json.loads((self.data / "results.json").read_text())["status"], "waiting")
        self.assertFalse((self.data / "results.csv").exists())

    def test_picks_the_newest_results_page_and_writes_outputs(self):
        newest = CONTAINER + "/Results_7201_GE-2026-10-24_Candidate.html"
        code, log = run(self.data, {CONTAINER + "?": LISTING, newest: SAMPLE})
        self.assertEqual(code, 0, log)
        out = json.loads((self.data / "results.json").read_text())
        self.assertEqual(out["source"], newest)
        self.assertEqual(out["status"], "counting")
        self.assertEqual(sorted(out["districts"]), ["ABM", "ABS", "ABW"])
        self.assertEqual(out["districts"]["ABM"]["candidates"][0], ["Reann Gasper", "Conservative Party", 13254])
        self.assertIn("1 districts missing from the results page: BDS", out["warnings"])
        rows = (self.data / "results.csv").read_text().splitlines()
        self.assertEqual(len(rows), 1 + 2 + 3 + 3)  # header + candidates in the three districts
        # same numbers again: nothing is rewritten
        before = (self.data / "history.csv").read_text()
        code, log = run(self.data, {CONTAINER + "?": LISTING, newest: SAMPLE})
        self.assertIn("No change", log)
        self.assertEqual((self.data / "history.csv").read_text(), before)

    def test_unreadable_page_leaves_files_alone(self):
        newest = CONTAINER + "/Results_7201_GE-2026-10-24_Candidate.html"
        run(self.data, {CONTAINER + "?": LISTING, newest: SAMPLE})
        before = (self.data / "results.json").read_text()
        code, log = run(self.data, {CONTAINER + "?": LISTING, newest: "<html><body>Service unavailable</body></html>"})
        self.assertEqual(code, 1)
        self.assertIn("::error::", log)
        self.assertEqual((self.data / "results.json").read_text(), before)


if __name__ == "__main__":
    unittest.main()
