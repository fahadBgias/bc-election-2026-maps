#!/usr/bin/env python3
"""Fetch the latest unofficial results of BC's 2026 provincial general election, district by district.

Source: Elections BC's election night results pages, published as static files in a public
storage container: https://electionsbcenr.blob.core.windows.net/electionsbcenr/
The results page is named Results_<number>_GE-2026-10-24_Candidate.html. The number isn't known
before polls close, so the script lists the container and picks the newest matching page.

Writes three files in data/:
  results.json   what the web page loads
  results.csv    one row per candidate, for anyone who wants the data
  history.csv    party totals each time the count moves, so the night can be replayed

Before the results page exists, results.json says the count hasn't started and nothing else
is written. If nothing changed since the last run, no file is touched.

Checks before writing:
  - every district on the page matches one of the 93 districts in data/geometry.json
  - each district's candidate votes add up to the district total on the page
Problems are reported as warnings in results.json; the numbers are shown as published.

Standard library only.
Exit codes: 0 = written, waiting, or nothing new; 1 = the source could not be read or parsed
(existing files are left as they were).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

CONTAINER = "https://electionsbcenr.blob.core.windows.net/electionsbcenr"
ELECTION = "GE-2026-10-24"
USER_AGENT = os.environ.get(
    "RESULTS_USER_AGENT",
    "bc-election-2026-maps/1.0 (+https://github.com/fahadBgias/bc-election-2026-maps)",
)
ROOT = Path(__file__).resolve().parent.parent
PACIFIC = ZoneInfo("America/Vancouver")
TOTAL_ROWS = {"total valid votes": "valid", "rejected ballots": "rejected", "total considered votes": "considered"}
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}


class FeedError(Exception):
    pass


# ---------- fetching ----------

def http_get(url: str, retries: int = 3) -> bytes:
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
            if isinstance(e, urllib.error.HTTPError) and e.code == 404:
                break
            time.sleep(2 * (attempt + 1))
    raise FeedError(f"could not read {url}: {last}")


def find_results_page(container: str, election: str) -> str | None:
    """Newest Results_<n>_<election>_Candidate.html in the container, or None before it exists."""
    pattern = re.compile(rf"^Results_(\d+)_{re.escape(election)}_Candidate\.html$")
    found, marker = [], ""
    while True:
        url = f"{container}?restype=container&comp=list" + (f"&marker={marker}" if marker else "")
        root = ET.fromstring(http_get(url))
        for blob in root.iter("Blob"):
            name = blob.findtext("Name") or ""
            m = pattern.match(name)
            if m:
                modified = blob.findtext("Properties/Last-Modified") or ""
                try:
                    ts = datetime.strptime(modified, "%a, %d %b %Y %H:%M:%S GMT").timestamp()
                except ValueError:
                    ts = 0.0
                found.append((ts, int(m.group(1)), name))
        marker = root.findtext("NextMarker") or ""
        if not marker:
            break
    if not found:
        return None
    return f"{container}/{max(found)[2]}"


# ---------- parsing ----------

class RowParser(HTMLParser):
    """Collects every table row: its attributes, its cells' text and any inline colours."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[dict] = []
        self.text: list[str] = []  # all visible text, for the header facts
        self._row = None
        self._cell = None
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "tr":
            self._row = {"attrs": a, "cells": [], "colours": []}
        elif tag in ("td", "th") and self._row is not None:
            self._cell = {"text": [], "colspan": int(a.get("colspan") or 1)}
        if self._row is not None:
            m = re.search(r"background-color:\s*(#[0-9A-Fa-f]{3,8})", a.get("style") or "")
            if m:
                self._row["colours"].append(m.group(1))

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row["cells"].append({"text": clean(" ".join(self._cell["text"])), "colspan": self._cell["colspan"]})
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._skip:
            return
        self.text.append(data)
        if self._cell is not None:
            self._cell["text"].append(data)


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def to_int(s: str) -> int:
    digits = re.sub(r"[^\d]", "", s)
    return int(digits) if digits else 0


def norm(name: str) -> str:
    """District names compared without accents, case, dash style or extra spaces."""
    s = re.sub(r"[‐-―−]", "-", name)  # en and em dashes, minus sign
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\s*-\s*", "-", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_as_of(text: str) -> tuple[str, str | None]:
    """'As of October 24, 2026 20:35 PDT' -> (raw text, ISO time in UTC)."""
    m = re.search(r"As of\s+([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})\s+(\d{1,2}):(\d{2})\s*([A-Z]{2,4})?", text)
    if not m:
        return "", None
    month = MONTHS.get(m.group(1).lower())
    if not month:
        return m.group(0), None
    local = datetime(int(m.group(3)), month, int(m.group(2)), int(m.group(4)), int(m.group(5)), tzinfo=PACIFIC)
    return clean(m.group(0)), local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_page(html: str) -> dict:
    p = RowParser()
    p.feed(html)
    text = clean(" ".join(p.text))
    as_of_text, updated = parse_as_of(text)

    def fact(label: str) -> int | None:
        m = re.search(r"([\d,]{3,})\s+" + re.escape(label), text)
        return to_int(m.group(1)) if m else None

    initial = re.search(r"INITIAL COUNT\s+(.{3,40}?)\s+FINAL COUNT", text, re.I)
    final = re.search(r"FINAL COUNT\s+([A-Za-z]+ \d{1,2}(?:-\d{1,2})?, \d{4}|Complete|In progress)", text, re.I)

    parties = []
    for row in p.rows:
        name = row["attrs"].get("data-party")
        if not name:
            continue
        cells = [c["text"] for c in row["cells"]]
        nums = [c for c in cells if re.fullmatch(r"[\d,]+(\.\d+)?\s*%?", c)]
        if len(nums) < 3:
            continue
        label = cells[0] if cells and cells[0] and not re.fullmatch(r"[\d,.%\s]*", cells[0]) else name
        parties.append({
            "name": clean(label),  # the page shows "Unaffiliated" for the row it tags as "Other"
            "colour": (row["colours"] or [None])[0],
            "leading": to_int(nums[-3]),
            "votes": to_int(nums[-2]),
            "pct": float(re.sub(r"[^\d.]", "", nums[-1]) or 0),
        })

    districts: dict[str, dict] = {}
    for row in p.rows:
        ed = row["attrs"].get("data-ed")
        if not ed:
            continue
        name = clean(ed)
        d = districts.setdefault(name, {"name": name, "candidates": [], "total": None, "boxes": {}, "notes": []})
        cells = row["cells"]
        texts = [c["text"] for c in cells]
        if len(cells) >= 5 and re.fullmatch(r"[\d.,]+\s*%", texts[-1] or "0%") and re.fullmatch(r"[\d,]+", texts[-2] or "0"):
            label = texts[-4].lower().rstrip("*").strip()
            if label in TOTAL_ROWS:  # 2026 layout: district totals are rows of their own
                if TOTAL_ROWS[label] == "valid":
                    d["total"] = to_int(texts[-2])
                else:
                    d.setdefault("extra", {})[TOTAL_ROWS[label]] = to_int(texts[-2])
            else:
                party = texts[-3] or "Unaffiliated"
                d["candidates"].append([texts[-4], party, to_int(texts[-2])])
        elif len(cells) in (2, 3) and cells[0]["colspan"] >= 3:  # 2024 layout: one unlabeled total row
            d["total"] = to_int(texts[1])
        elif len(cells) == 1:
            line = texts[0]
            m = re.match(r"(.+?)\s*\|\s*(\d+)\s+of\s+(\d+)\s+reported", line, re.I)
            if m:
                key = "advance" if "advance" in m.group(1).lower() else (
                    "final" if "final" in m.group(1).lower() else clean(m.group(1)).lower())
                d["boxes"][key] = [int(m.group(2)), int(m.group(3))]
            elif "|" in line:
                label, status = [clean(x) for x in line.split("|", 1)]
                d["notes"].append([label, status])

    if not districts:
        raise FeedError("no districts found on the results page")
    return {
        "asOf": as_of_text,
        "updated": updated,
        "count": {"initial": clean(initial.group(1)) if initial else None,
                  "final": clean(final.group(1)) if final else None},
        "totals": {"valid": fact("Total Valid Votes"), "considered": fact("Total Votes Considered"),
                   "registered": fact("Registered voters")},
        "parties": parties,
        "districts": districts,
    }


# ---------- building the output ----------

def load_districts(data_dir: Path) -> dict[str, str]:
    """normalized name -> district code, from data/geometry.json."""
    geo = json.loads((data_dir / "geometry.json").read_text(encoding="utf-8"))
    return {norm(name): code for code, name in geo["names"].items()}


def build(parsed: dict, source: str, codes: dict[str, str]) -> dict:
    warnings = []
    out_d = {}
    for name, d in parsed["districts"].items():
        code = codes.get(norm(name))
        if not code:
            warnings.append(f"'{name}' on the results page doesn't match any district on the map")
            code = name
        cands = sorted(d["candidates"], key=lambda c: -c[2])
        total = sum(c[2] for c in cands)
        if d["total"] is not None and d["total"] != total:
            warnings.append(f"{name}: candidates add up to {total:,} but the page's total is {d['total']:,}")
        out_d[code] = {"name": name, "candidates": cands, "total": d["total"] if d["total"] is not None else total,
                       "boxes": d["boxes"], "notes": d["notes"]}
    missing = sorted(set(codes.values()) - set(out_d))
    if missing:
        warnings.append(f"{len(missing)} districts missing from the results page: {', '.join(missing)}")
    boxes = [0, 0]
    for d in out_d.values():
        for r, t in d["boxes"].values():
            boxes[0] += r
            boxes[1] += t
    return {
        "election": ELECTION,
        "status": "counting",
        "updated": parsed["updated"],
        "asOf": parsed["asOf"],
        "fetched": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "count": parsed["count"],
        "totals": parsed["totals"],
        "boxes": boxes,
        "parties": parsed["parties"],
        "districts": dict(sorted(out_d.items())),
        "warnings": warnings,
    }


def same_count(old: dict, new: dict) -> bool:
    keys = ("status", "asOf", "totals", "boxes", "parties", "districts")
    return all(old.get(k) == new.get(k) for k in keys)


def write_outputs(data_dir: Path, out: dict) -> None:
    (data_dir / "results.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    with open(data_dir / "results.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["district_code", "district", "candidate", "party", "votes", "pct_of_district", "leading",
                    "advance_boxes_reported", "advance_boxes_total", "final_day_boxes_reported", "final_day_boxes_total"])
        for code, d in out["districts"].items():
            adv = d["boxes"].get("advance", ["", ""])
            fin = d["boxes"].get("final", ["", ""])
            top = d["candidates"][0][2] if d["candidates"] else 0
            for name, party, votes in d["candidates"]:
                pct = round(100 * votes / d["total"], 2) if d["total"] else ""
                w.writerow([code, d["name"], name, party, votes, pct, "yes" if votes == top and top > 0 else "",
                            adv[0], adv[1], fin[0], fin[1]])
    hist = data_dir / "history.csv"
    new_file = not hist.exists()
    with open(hist, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["updated_utc", "boxes_reported", "boxes_total", "party", "districts_leading", "votes"])
        for p in out["parties"]:
            w.writerow([out["updated"] or out["fetched"], out["boxes"][0], out["boxes"][1], p["name"], p["leading"], p["votes"]])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--container", default=CONTAINER)
    ap.add_argument("--election", default=ELECTION)
    ap.add_argument("--page-url", help="read this results page instead of looking it up")
    ap.add_argument("--data-dir", default=str(ROOT / "data"))
    args = ap.parse_args(argv)
    data_dir = Path(args.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    out_path = data_dir / "results.json"
    old = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}

    try:
        page = args.page_url or find_results_page(args.container, args.election)
        if not page:
            if old.get("status") in (None, "waiting"):
                waiting = {"election": args.election, "status": "waiting",
                           "checked": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
                if old.get("status") != "waiting":
                    out_path.write_text(json.dumps(waiting, separators=(",", ":")), encoding="utf-8")
                print("No results page yet. Waiting for polls to close.")
            else:
                print("::warning::The results page was not found this time; keeping the last numbers.")
            return 0
        html = http_get(page).decode("utf-8", errors="replace")
        parsed = parse_page(html)
        out = build(parsed, page, load_districts(data_dir))
    except (FeedError, ET.ParseError, ValueError, KeyError, OSError) as e:
        print(f"::error::{e}")
        return 1

    for w in out["warnings"]:
        print(f"::warning::{w}")
    if same_count(old, out):
        print(f"No change since {out['asOf'] or out['updated']}.")
        return 0
    write_outputs(data_dir, out)
    lead = ", ".join(f"{p['name']} {p['leading']}" for p in sorted(out["parties"], key=lambda p: -p["leading"]) if p["leading"])
    print(f"Wrote results as of {out['asOf'] or out['updated']}: {out['boxes'][0]} of {out['boxes'][1]} ballot boxes. Leading: {lead or 'none yet'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
