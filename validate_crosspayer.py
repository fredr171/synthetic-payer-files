"""Cross-payer checks between the CCLF (Medicare FFS) and Humana (MA) synthetic files.

Derives coverage spans from each payer's eligibility files (CCLF8 BENE_RNG_BGN/END_DT,
HUM_ELIG ENROLLMENT_START/END_DT), then checks: no overlapping FFS and MA coverage, no
claims from one payer during the other payer's coverage, observed switches match
answer_key/crossover_members.csv, and demographics agree between CCLF8 and HUM_ELIG.

Usage:
    python validate_crosspayer.py [--cclf-dir output] [--humana-dir output/humana]
                                  [--crossover answer_key/crossover_members.csv]
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from cclf_common import (
    MBI_REGEX,
    coverage_spans,
    file_name,
    month_dirs,
    overlaps_any_span,
    parse_month,
)
from humana_common import hum_file_name
from validate_cclf import Check, read_csv, report

HERE = Path(__file__).resolve().parent
SWITCHER_RANGE = (15, 20)
GENDER = {"1": "M", "2": "F"}

Spans = list[tuple[date, date | None]]


def load(folder: Path, kind: str, names: list[str]) -> dict[str, list[tuple[str, dict]]]:
    """{file_type: [(delivery month, row), ...]} in delivery order."""
    out: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for month_dir in month_dirs(folder):
        m = parse_month(month_dir.name)
        for ft in names:
            name = file_name(ft, m) if kind == "cclf" else hum_file_name(ft, m)
            path = month_dir / name
            if path.exists():
                out[ft] += [(month_dir.name, row) for row in read_csv(path)[1]]
    return out


def transitions(ffs: Spans, ma: Spans) -> set[tuple[str, date]]:
    """(direction, effective date) wherever one payer's span ends the day before the other's."""
    tagged = sorted([(s, e, "FFS") for s, e in ffs] + [(s, e, "MA") for s, e in ma])
    out = set()
    for (_s1, e1, p1), (s2, _e2, p2) in zip(tagged, tagged[1:], strict=False):
        if p1 != p2 and e1 is not None and e1 + timedelta(days=1) == s2:
            out.add(("FFS_TO_MA" if p1 == "FFS" else "MA_TO_FFS", s2))
    return out


def spans_overlap(a: Spans, b: Spans) -> bool:
    return any(overlaps_any_span(b, s, e or date.max) for s, e in a)


def run(cclf_dir: Path, humana_dir: Path, crossover: Path) -> bool:
    checks: list[Check] = []

    def check(name: str) -> Check:
        checks.append(Check(name))
        return checks[-1]

    cclf = load(cclf_dir, "cclf", ["CCLF8", "CCLF1", "CCLF5"])
    hum = load(humana_dir, "humana", ["HUM_ELIG", "HUM_MEDCLM"])
    cclf8 = [r for _, r in cclf["CCLF8"]]
    elig = [r for _, r in hum["HUM_ELIG"]]
    ffs = coverage_spans(cclf8, "BENE_MBI_ID", "BENE_RNG_BGN_DT", "BENE_RNG_END_DT")
    ma_by_member = coverage_spans(elig, "MEMBER_ID", "ENROLLMENT_START_DT", "ENROLLMENT_END_DT")
    member_of = {r["MBI"]: r["MEMBER_ID"] for r in elig}
    ma = {mbi: ma_by_member[mid] for mbi, mid in member_of.items()}
    _, xrows = read_csv(crossover)
    switchers = {r["BENE_MBI_ID"] for r in xrows}

    c = check("crossover list: 15-20 switchers, valid MBIs, directions and effective dates")
    if not SWITCHER_RANGE[0] <= len(switchers) <= SWITCHER_RANGE[1]:
        c.fail(f"{len(switchers)} distinct switchers")
    for r in xrows:
        eff = date.fromisoformat(r["effective_date"])
        if not MBI_REGEX.match(r["BENE_MBI_ID"]) or eff.day != 1:
            c.fail(f"{r['BENE_MBI_ID']}: bad MBI or effective date {eff}")
        if r["direction"] == "MA_TO_FFS" and eff.month != 1:
            c.fail(f"{r['BENE_MBI_ID']}: MA -> FFS must be effective Jan 1, got {eff}")
        if r["direction"] == "FFS_TO_MA" and eff.month not in (1, 2, 3, 4):
            c.fail(f"{r['BENE_MBI_ID']}: FFS -> MA must be AEP (Jan) or OEP/SEP (Feb-Apr)")
        if r["direction"] not in ("FFS_TO_MA", "MA_TO_FFS"):
            c.fail(f"{r['BENE_MBI_ID']}: bad direction {r['direction']}")
    counts = defaultdict(int)
    for r in xrows:
        counts[(r["direction"], r["enrollment_period"])] += 1
    c.info = f"{len(switchers)} switchers, {len(xrows)} switch events: " + ", ".join(
        f"{d} {p}={n}" for (d, p), n in sorted(counts.items())
    )

    c = check("the only MBIs in both payers are the listed switchers; MEMBER_IDs match")
    both = set(ffs) & set(ma)
    if both != switchers:
        c.fail(f"in both payers but not listed: {sorted(both - switchers)[:3]}; "
               f"listed but not in both: {sorted(switchers - both)[:3]}")  # fmt: skip
    for r in xrows:
        if member_of.get(r["BENE_MBI_ID"]) != r["MEMBER_ID"]:
            c.fail(f"{r['BENE_MBI_ID']}: MEMBER_ID {r['MEMBER_ID']} not in HUM_ELIG for that MBI")
    c.info = f"{len(ffs)} FFS beneficiaries, {len(ma)} MA members, {len(both)} in both"

    c = check("no overlapping FFS and MA coverage; observed switches match crossover list")
    expected = defaultdict(set)
    for r in xrows:
        expected[r["BENE_MBI_ID"]].add((r["direction"], date.fromisoformat(r["effective_date"])))
    for mbi in sorted(both | switchers):
        f_spans, m_spans = ffs.get(mbi, []), ma.get(mbi, [])
        if spans_overlap(f_spans, m_spans):
            c.fail(f"{mbi}: FFS {f_spans} overlaps MA {m_spans}")
        if transitions(f_spans, m_spans) != expected[mbi]:
            c.fail(f"{mbi}: observed {sorted(transitions(f_spans, m_spans))} "
                   f"!= listed {sorted(expected[mbi])}")  # fmt: skip

    c = check("no CCLF claims during MA coverage; no Humana claims during FFS coverage")
    n_cclf = n_hum = 0
    for ft in ("CCLF1", "CCLF5"):
        for mk, r in cclf[ft]:
            mbi = r["BENE_MBI_ID"]
            if mbi in ma:
                n_cclf += 1
                lo, hi = date.fromisoformat(r["CLM_FROM_DT"]), date.fromisoformat(r["CLM_THRU_DT"])
                if overlaps_any_span(ma[mbi], lo, hi):
                    c.fail(f"{mk} {ft} {r['CUR_CLM_UNIQ_ID']} {lo}..{hi} during MA coverage")
    for mk, r in hum["HUM_MEDCLM"]:
        mbi = r["MBI"]
        if mbi in ffs:
            n_hum += 1
            lo, hi = (
                date.fromisoformat(r["SERVICE_FROM_DT"]),
                date.fromisoformat(r["SERVICE_TO_DT"]),
            )
            if overlaps_any_span(ffs[mbi], lo, hi):
                c.fail(f"{mk} HUM_MEDCLM {r['CLAIM_ID']} {lo}..{hi} during FFS coverage")
    c.info = f"{n_cclf} CCLF and {n_hum} Humana claim rows for switchers checked"

    c = check("demographics agree: name/DOB/sex always; address and dual at each handoff")
    by_month_cclf: dict[tuple[str, str], dict] = {}
    by_month_hum: dict[tuple[str, str], dict] = {}
    ident_cclf: dict[str, set] = defaultdict(set)
    ident_hum: dict[str, set] = defaultdict(set)
    for mk, r in cclf["CCLF8"]:
        if r["BENE_MBI_ID"] in switchers:
            by_month_cclf[(r["BENE_MBI_ID"], mk)] = r
            ident_cclf[r["BENE_MBI_ID"]].add((
                r["BENE_1ST_NAME"], r["BENE_MIDL_NAME"], r["BENE_LAST_NAME"], r["BENE_DOB"],
                GENDER.get(r["BENE_SEX_CD"], "?"),
            ))  # fmt: skip
    for mk, r in hum["HUM_ELIG"]:
        if r["MBI"] in switchers:
            by_month_hum[(r["MBI"], mk)] = r
            ident_hum[r["MBI"]].add(
                (r["FIRST_NAME"], r["MIDDLE_INITIAL"], r["LAST_NAME"], r["DOB"], r["GENDER"])
            )
    for mbi in sorted(switchers):
        if len(ident_cclf[mbi] | ident_hum[mbi]) != 1:
            c.fail(f"{mbi}: name/DOB/sex differ: {sorted(ident_cclf[mbi] | ident_hum[mbi])}")
    handoffs = 0
    for key in sorted(set(by_month_cclf) & set(by_month_hum)):
        a, b = by_month_cclf[key], by_month_hum[key]
        handoffs += 1
        mine = (a["BENE_LINE_1_ADR"], a["GEO_ZIP_PLC_NAME"], a["GEO_USPS_STATE_CD"],
                a["GEO_ZIP5_CD"], a["BENE_FIPS_STATE_CD"] + a["BENE_FIPS_CNTY_CD"])  # fmt: skip
        theirs = (b["ADDRESS_LINE_1"], b["CITY"], b["STATE"], b["ZIP"], b["COUNTY_FIPS"])
        if mine != theirs:
            c.fail(f"{key}: address CCLF8 {mine} != HUM_ELIG {theirs}")
        if (a["BENE_DUAL_STUS_CD"] != "NA") != (b["DUAL_FLAG"] == "Y"):
            c.fail(f"{key}: dual status differs")
    if handoffs < len(xrows):
        c.fail(f"only {handoffs} same-month CCLF8/HUM_ELIG row pairs for {len(xrows)} switches")
    c.info = f"{handoffs} same-month row pairs compared"

    return report(checks, f"Cross-payer validation: {cclf_dir} vs {humana_dir}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cclf-dir", type=Path, default=HERE / "output")
    parser.add_argument("--humana-dir", type=Path, default=HERE / "output" / "humana")
    parser.add_argument(
        "--crossover", type=Path, default=HERE / "answer_key" / "crossover_members.csv"
    )
    args = parser.parse_args(argv)
    return 0 if run(args.cclf_dir, args.humana_dir, args.crossover) else 1


if __name__ == "__main__":
    sys.exit(main())
