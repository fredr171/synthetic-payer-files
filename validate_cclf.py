"""Validate generated synthetic CCLF files and the answer key.

Reads only the CSVs on disk (never the generator's internals), re-derives the
expected results, and compares them to answer_key/. Prints a PASS/FAIL summary and
exits non-zero if any check fails.

Usage:
    python validate_cclf.py [--output-dir output] [--answer-key-dir answer_key]
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from cclf_common import (
    ADJ_ADJUSTMENT,
    ADJ_CANCEL,
    ADJ_ORIGINAL,
    FILE_COLUMNS,
    KB,
    MBI_REGEX,
    SIZE_RANGE_INITIAL,
    SIZE_RANGE_MONTHLY,
    add_months,
    coverage_spans,
    file_name,
    in_any_span,
    month_dirs,
    month_key,
    npi_is_valid,
    parse_month,
)

HERE = Path(__file__).resolve().parent
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
AMT_RE = re.compile(r"^-?\d+\.\d{2}$")
MAX_EXAMPLES = 3


@dataclass
class Check:
    name: str
    errors: list[str] = field(default_factory=list)
    info: str = ""

    def fail(self, msg: str) -> None:
        self.errors.append(msg)

    @property
    def passed(self) -> bool:
        return not self.errors


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        return header, [dict(zip(header, row, strict=True)) for row in reader]


def to_date(s: str) -> date:
    return date.fromisoformat(s)


def month_of(s: str) -> str:
    return s[:7]


class Validator:
    def __init__(self, output_dir: Path, key_dir: Path) -> None:
        self.output_dir, self.key_dir = output_dir, key_dir
        self.checks: list[Check] = []
        self.months = [parse_month(p.name) for p in month_dirs(output_dir)]
        self.data: dict[str, dict[str, list[dict[str, str]]]] = {}
        self.headers: dict[tuple[str, str], list[str]] = {}

    def check(self, name: str) -> Check:
        c = Check(name)
        self.checks.append(c)
        return c

    # -- loading ------------------------------------------------------------
    def load(self) -> None:
        c = self.check("all 4 files present for every month, contiguous months")
        if not self.months:
            c.fail("no month folders found")
            return
        expected = []
        cur = self.months[0]
        while cur <= self.months[-1]:
            expected.append(cur)
            cur = add_months(cur, 1)
        if expected != self.months:
            c.fail("month folders are not contiguous")
        for m in self.months:
            mk = month_key(m)
            self.data[mk] = {}
            for ft in FILE_COLUMNS:
                path = self.output_dir / mk / file_name(ft, m)
                if not path.exists():
                    c.fail(f"missing {path}")
                    self.data[mk][ft] = []
                    continue
                header, rows = read_csv(path)
                self.headers[(mk, ft)] = header
                self.data[mk][ft] = rows
        c.info = f"{len(self.months)} months x 4 = {len(self.months) * 4} files expected"

    def rows(self, ft: str):
        for mk in sorted(self.data):
            for row in self.data[mk][ft]:
                yield mk, row

    # -- checks -------------------------------------------------------------
    def check_headers_and_formats(self) -> None:
        c = self.check("headers match layouts; dates YYYY-MM-DD; amounts 2 decimals")
        date_cols = lambda cols: [x for x in cols if x.endswith("_DT")]  # noqa: E731
        amt_cols = lambda cols: [x for x in cols if x.endswith("_AMT")]  # noqa: E731
        for (mk, ft), header in self.headers.items():
            if header != FILE_COLUMNS[ft]:
                c.fail(f"{mk} {ft}: header mismatch")
        for ft, cols in FILE_COLUMNS.items():
            for mk, row in self.rows(ft):
                for col in date_cols(cols):
                    if row[col] and not DATE_RE.match(row[col]):
                        c.fail(f"{mk} {ft} {col}={row[col]!r}")
                for col in amt_cols(cols):
                    if not AMT_RE.match(row[col]):
                        c.fail(f"{mk} {ft} {col}={row[col]!r}")

    def check_sizes(self) -> None:
        c = self.check("file sizes in range (initial load and monthly)")
        first = month_key(self.months[0])
        lo_all, hi_all = [], []
        for m in self.months:
            mk = month_key(m)
            for ft in FILE_COLUMNS:
                path = self.output_dir / mk / file_name(ft, m)
                if not path.exists():
                    continue
                size = path.stat().st_size
                lo, hi = SIZE_RANGE_INITIAL[ft] if mk == first else SIZE_RANGE_MONTHLY
                if not lo <= size <= hi:
                    c.fail(f"{mk} {ft}: {size / KB:.1f} KB outside {lo / KB:.0f}-{hi / KB:.0f} KB")
                if mk != first:
                    lo_all.append(size)
                    hi_all.append(size)
        if lo_all:
            c.info = f"monthly files {min(lo_all) / KB:.1f}-{max(hi_all) / KB:.1f} KB"

    def check_members(self) -> None:
        c = self.check("CCLF8: valid MBIs, NEW once then UPDATEs, MBI/HICN stable")
        seen: dict[str, str] = {}
        for mk, row in self.rows("CCLF8"):
            mbi = row["BENE_MBI_ID"]
            if not MBI_REGEX.match(mbi):
                c.fail(f"{mk} invalid MBI {mbi}")
            kind = row["RECORD_CHANGE_TYPE"]
            if kind == "NEW" and mbi in seen:
                c.fail(f"{mk} {mbi} NEW but already delivered")
            if kind == "UPDATE" and mbi not in seen:
                c.fail(f"{mk} {mbi} UPDATE before NEW")
            if kind not in ("NEW", "UPDATE"):
                c.fail(f"{mk} {mbi} bad RECORD_CHANGE_TYPE {kind!r}")
            if mbi in seen and seen[mbi] != row["BENE_HIC_NUM"]:
                c.fail(f"{mk} {mbi} HICN changed")
            seen[mbi] = row["BENE_HIC_NUM"]
        per_month = Counter(mk for mk, _ in self.rows("CCLF8"))
        dup = [
            mk
            for mk in per_month
            if per_month[mk] != len({r["BENE_MBI_ID"] for r in self.data[mk]["CCLF8"]})
        ]
        for mk in dup:
            c.fail(f"{mk} beneficiary delivered twice in one month")
        c.info = f"{len(seen)} beneficiaries"

    def _member_index(self) -> tuple[dict[str, str], dict[str, date], dict[str, date]]:
        """first delivery month, BENE_RNG_BGN_DT, and delivered death date per MBI."""
        first, rng_bgn, death = {}, {}, {}
        for mk, row in self.rows("CCLF8"):
            mbi = row["BENE_MBI_ID"]
            if mbi not in first:
                first[mbi] = mk
                rng_bgn[mbi] = to_date(row["BENE_RNG_BGN_DT"])
            if row["BENE_DEATH_DT"]:
                death[mbi] = to_date(row["BENE_DEATH_DT"])
        return first, rng_bgn, death

    def check_referential_integrity(self) -> None:
        c = self.check("claim MBIs exist in CCLF8 delivered same month or earlier")
        first, _, _ = self._member_index()
        for ft in ("CCLF1", "CCLF4", "CCLF5"):
            for mk, row in self.rows(ft):
                mbi = row["BENE_MBI_ID"]
                if mbi not in first:
                    c.fail(f"{mk} {ft} {mbi} never delivered in CCLF8")
                elif first[mbi] > mk:
                    c.fail(f"{mk} {ft} {mbi} first in CCLF8 {first[mbi]}")

        c = self.check("CCLF4: claim in same-month CCLF1, 1-6 dx rows, seq 0 = principal")
        for mk in sorted(self.data):
            headers = {r["CUR_CLM_UNIQ_ID"]: r for r in self.data[mk]["CCLF1"]}
            dx_by_claim = defaultdict(list)
            for r in self.data[mk]["CCLF4"]:
                dx_by_claim[r["CUR_CLM_UNIQ_ID"]].append(r)
                if r["CUR_CLM_UNIQ_ID"] not in headers:
                    c.fail(f"{mk} CCLF4 {r['CUR_CLM_UNIQ_ID']} not in CCLF1 {mk}")
            for clm_id, hdr in headers.items():
                dx = dx_by_claim.get(clm_id, [])
                if not 1 <= len(dx) <= 6:
                    c.fail(f"{mk} claim {clm_id} has {len(dx)} dx rows")
                    continue
                seq0 = [r for r in dx if r["CLM_VAL_SQNC_NUM"] == "0"]
                if len(seq0) != 1 or seq0[0]["CLM_DGNS_CD"] != hdr["PRNCPL_DGNS_CD"]:
                    c.fail(f"{mk} claim {clm_id} seq 0 != PRNCPL_DGNS_CD")
                seqs = sorted(int(r["CLM_VAL_SQNC_NUM"]) for r in dx)
                if seqs != list(range(len(dx))):
                    c.fail(f"{mk} claim {clm_id} dx sequence gaps {seqs}")
                if any(r["BENE_MBI_ID"] != hdr["BENE_MBI_ID"] for r in dx):
                    c.fail(f"{mk} claim {clm_id} dx MBI differs from header")

    def check_claim_ids(self) -> None:
        c = self.check("CUR_CLM_UNIQ_ID unique across all files and months")
        owner: dict[str, tuple[str, str]] = {}
        for mk, row in self.rows("CCLF1"):
            cid = row["CUR_CLM_UNIQ_ID"]
            if cid in owner:
                c.fail(f"{cid} repeated ({owner[cid]} and {mk} CCLF1)")
            owner[cid] = (mk, "CCLF1")
        b_lines: dict[str, set[str]] = defaultdict(set)
        for mk, row in self.rows("CCLF5"):
            cid = row["CUR_CLM_UNIQ_ID"]
            if cid in owner and owner[cid] != (mk, "CCLF5"):
                c.fail(f"{cid} repeated ({owner[cid]} and {mk} CCLF5)")
            owner[cid] = (mk, "CCLF5")
            if row["CLM_LINE_NUM"] in b_lines[cid]:
                c.fail(f"{cid} duplicate line {row['CLM_LINE_NUM']}")
            b_lines[cid].add(row["CLM_LINE_NUM"])
        c.info = f"{len(owner)} claim IDs"

        c = self.check("CLM_IDR_LD_DT inside the delivery month; NPIs pass Luhn")
        for ft in ("CCLF1", "CCLF5"):
            for mk, row in self.rows(ft):
                if month_of(row["CLM_IDR_LD_DT"]) != mk:
                    c.fail(f"{mk} {ft} {row['CUR_CLM_UNIQ_ID']} loaded {row['CLM_IDR_LD_DT']}")
                for col in ("FAC_PRVDR_NPI_NUM", "ATNDG_PRVDR_NPI_NUM", "RNDRG_PRVDR_NPI_NUM"):
                    if col in row and not npi_is_valid(row[col]):
                        c.fail(f"{mk} {ft} bad NPI {row[col]}")
        npis = {r[col] for ft, cols in (("CCLF1", ("FAC_PRVDR_NPI_NUM", "ATNDG_PRVDR_NPI_NUM")),
                                        ("CCLF5", ("RNDRG_PRVDR_NPI_NUM",)))
                for _, r in self.rows(ft) for col in cols}  # fmt: skip
        c.info = f"{len(npis)} distinct NPIs"

    def _claim_versions(self) -> list[dict]:
        """One record per claim version: part, id, cntl, type, mbi, dates, amount, month."""
        out = []
        for mk, r in self.rows("CCLF1"):
            out.append({
                "part": "A", "id": r["CUR_CLM_UNIQ_ID"], "cntl": r["CLM_CNTL_NUM"],
                "adj": r["CLM_ADJSMT_TYPE_CD"], "mbi": r["BENE_MBI_ID"],
                "from": r["CLM_FROM_DT"], "thru": r["CLM_THRU_DT"], "month": mk,
                "amount": round(float(r["CLM_PMT_AMT"]), 2), "org": r["CLM_ORG_CNTL_NUM"],
            })  # fmt: skip
        b: dict[str, dict] = {}
        for mk, r in self.rows("CCLF5"):
            rec = b.setdefault(r["CUR_CLM_UNIQ_ID"], {
                "part": "B", "id": r["CUR_CLM_UNIQ_ID"], "cntl": r["CLM_CNTL_NUM"],
                "adj": r["CLM_ADJSMT_TYPE_CD"], "mbi": r["BENE_MBI_ID"],
                "from": r["CLM_FROM_DT"], "thru": r["CLM_THRU_DT"], "month": mk,
                "amount": 0.0, "org": r["CLM_CNTL_NUM"],
            })  # fmt: skip
            rec["amount"] = round(rec["amount"] + float(r["CLM_LINE_CVRD_PD_AMT"]), 2)
        return out + list(b.values())

    def check_actions(self) -> dict[tuple[str, str], float]:
        c = self.check("cancellations/adjustments link to one earlier original (same claim)")
        versions = self._claim_versions()
        originals = {}
        for v in versions:
            if v["adj"] == ADJ_ORIGINAL:
                key = (v["part"], v["cntl"])
                if key in originals:
                    c.fail(f"control number {key} used by two originals")
                originals[key] = v
        actioned: dict[tuple[str, str], dict] = {}
        for v in versions:
            if v["adj"] not in (ADJ_CANCEL, ADJ_ADJUSTMENT):
                continue
            key = (v["part"], v["cntl"])
            o = originals.get(key)
            if o is None:
                c.fail(f"{v['month']} {v['id']} has no original for {key}")
                continue
            if key in actioned:
                c.fail(f"original {o['id']} actioned twice")
            actioned[key] = v
            if o["month"] >= v["month"]:
                c.fail(f"{v['id']} ({v['month']}) not after original ({o['month']})")
            if (o["mbi"], o["from"], o["thru"]) != (v["mbi"], v["from"], v["thru"]):
                c.fail(f"{v['id']} identifying data differs from original {o['id']}")
            if v["part"] == "A" and v["org"] != o["cntl"]:
                c.fail(f"{v['id']} CLM_ORG_CNTL_NUM does not link to original")
            if v["adj"] == ADJ_CANCEL and abs(v["amount"] + o["amount"]) > 0.005:
                c.fail(f"cancel {v['id']} amount {v['amount']} != -{o['amount']}")
            if v["adj"] == ADJ_ADJUSTMENT and abs(v["amount"] - o["amount"]) < 0.005:
                c.fail(f"adjustment {v['id']} leaves amount unchanged")
        n_total = len(versions)
        n_cancel = sum(v["adj"] == ADJ_CANCEL for v in versions)
        n_adjust = sum(v["adj"] == ADJ_ADJUSTMENT for v in versions)
        for label, n in (("cancellations", n_cancel), ("adjustments", n_adjust)):
            if not 0.03 <= n / n_total <= 0.07:
                c.fail(f"{label} {n / n_total:.1%} of claims (expected ~5%)")
        c.info = (
            f"{n_cancel} cancels ({n_cancel / n_total:.1%}), "
            f"{n_adjust} adjustments ({n_adjust / n_total:.1%}) of {n_total} claim versions"
        )

        # Compare with answer_key/claim_actions.csv
        c = self.check("answer_key/claim_actions.csv matches the files")
        _, key_rows = read_csv(self.key_dir / "claim_actions.csv")
        expected = {
            (r["part"], r["CLM_CNTL_NUM"]): (
                r["original_clm_id"],
                r["action_clm_id"],
                r["action_type"],
            )
            for r in key_rows
        }
        found = {
            k: (originals[k]["id"], v["id"], "CANCEL" if v["adj"] == ADJ_CANCEL else "ADJUST")
            for k, v in actioned.items()
            if k in originals
        }
        if expected != found:
            c.fail(f"{len(set(expected.items()) ^ set(found.items()))} mismatched actions")

        # Final amount per claim family
        final = {}
        for k, o in originals.items():
            v = actioned.get(k)
            final[k] = 0.0 if v and v["adj"] == ADJ_CANCEL else (v["amount"] if v else o["amount"])
        self._originals = originals
        return final

    def check_dates_vs_members(self) -> None:
        c = self.check("claims inside an FFS span (BENE_RNG_BGN_DT..END_DT); none after death")
        _, _, death = self._member_index()
        spans = coverage_spans(
            [r for _, r in self.rows("CCLF8")], "BENE_MBI_ID", "BENE_RNG_BGN_DT", "BENE_RNG_END_DT"
        )
        n_after_death_late = 0
        for v in self._claim_versions():
            mbi = v["mbi"]
            if mbi in death and to_date(v["thru"]) > death[mbi]:
                c.fail(f"{v['id']} thru {v['thru']} after death {death[mbi]}")
            if not in_any_span(spans.get(mbi, []), to_date(v["from"]), to_date(v["thru"])):
                c.fail(f"{v['id']} {v['from']}..{v['thru']} outside FFS coverage spans")
            if v["from"] > v["thru"]:
                c.fail(f"{v['id']} from after thru")
            if mbi in death and month_of(v["month"]) > death[mbi].isoformat()[:7]:
                n_after_death_late += 1
        for _mk, r in self.rows("CCLF5"):
            if (
                r["BENE_MBI_ID"] in death
                and to_date(r["CLM_LINE_THRU_DT"]) > death[r["BENE_MBI_ID"]]
            ):
                c.fail(f"{r['CUR_CLM_UNIQ_ID']} line after death")
        ended = sum(1 for s in spans.values() for _, e in s if e is not None)
        c.info = (
            f"{len(death)} deaths, {ended} ended FFS spans; {n_after_death_late} claim versions "
            "loaded after the death month (service before death)"
        )

    def check_timing_and_concentration(self) -> None:
        c = self.check("~10% late-arriving originals (service 4-12 months before load)")
        late = total = 0
        for v in self._claim_versions():
            if v["adj"] != ADJ_ORIGINAL:
                continue
            total += 1
            lag = (int(v["month"][:4]) * 12 + int(v["month"][5:])) - (
                int(v["from"][:4]) * 12 + int(v["from"][5:7])
            )
            if lag > 12 or lag < 0:
                c.fail(f"{v['id']} service lag {lag} months")
            late += lag >= 4
        if not 0.07 <= late / total <= 0.13:
            c.fail(f"late share {late / total:.1%}")
        c.info = f"{late}/{total} = {late / total:.1%}"

        c = self.check("utilization concentrated in a minority of beneficiaries")
        per_bene = Counter(v["mbi"] for v in self._claim_versions() if v["adj"] == ADJ_ORIGINAL)
        n_benes = len({r["BENE_MBI_ID"] for _, r in self.rows("CCLF8")})
        top = sorted(per_bene.values(), reverse=True)[: max(1, n_benes // 5)]
        share = sum(top) / sum(per_bene.values())
        if share < 0.5:
            c.fail(f"top 20% of beneficiaries have only {share:.0%} of claims")
        c.info = f"top 20% of beneficiaries generate {share:.0%} of original claims"

    def check_answer_key(self, final: dict[tuple[str, str], float]) -> None:
        c = self.check("answer_key/final_members.csv = latest CCLF8 row per beneficiary")
        latest: dict[str, tuple[dict, str, str]] = {}
        for mk, row in self.rows("CCLF8"):
            mbi = row["BENE_MBI_ID"]
            first = latest[mbi][1] if mbi in latest else mk
            latest[mbi] = (row, first, mk)
        _, key_rows = read_csv(self.key_dir / "final_members.csv")
        key_by_mbi = {r["BENE_MBI_ID"]: r for r in key_rows}
        if set(key_by_mbi) != set(latest):
            c.fail("beneficiary sets differ")
        for mbi, (row, first, last) in latest.items():
            k = key_by_mbi.get(mbi)
            if k is None:
                continue
            exp = {col: v for col, v in row.items() if col != "RECORD_CHANGE_TYPE"}
            exp |= {"FIRST_DELIVERY_MONTH": first, "LAST_DELIVERY_MONTH": last}
            if exp != k:
                c.fail(f"{mbi} differs from latest delivered row")
        c.info = f"{len(key_rows)} members"

        c = self.check("answer_key/net_paid_by_service_month.csv matches recomputation")
        totals: dict[str, dict[str, float]] = defaultdict(lambda: {"A": 0.0, "B": 0.0})
        for (part, cntl), amount in final.items():
            m = month_of(self._originals[(part, cntl)]["from"])
            totals[m][part] = round(totals[m][part] + amount, 2)
        _, key_rows = read_csv(self.key_dir / "net_paid_by_service_month.csv")
        key = {r["service_month"]: r for r in key_rows}
        if set(key) != set(totals):
            c.fail("service months differ")
        for m, t in totals.items():
            k = key.get(m)
            if k and (
                abs(float(k["part_a_net_paid"]) - t["A"]) > 0.005
                or abs(float(k["part_b_net_paid"]) - t["B"]) > 0.005
            ):
                c.fail(f"{m}: key A={k['part_a_net_paid']} B={k['part_b_net_paid']} "
                       f"vs files A={t['A']:.2f} B={t['B']:.2f}")  # fmt: skip
        c.info = (
            f"Part A ${sum(t['A'] for t in totals.values()):,.2f}, "
            f"Part B ${sum(t['B'] for t in totals.values()):,.2f} net paid"
        )

        c = self.check("answer_key/net_paid_by_load_month.csv sums to the same total")
        _, key_rows = read_csv(self.key_dir / "net_paid_by_load_month.csv")
        by_load = sum(float(r["total_net_change"]) for r in key_rows)
        by_svc = sum(t["A"] + t["B"] for t in totals.values())
        if abs(by_load - by_svc) > 0.01:
            c.fail(f"load-month total {by_load:.2f} != service-month total {by_svc:.2f}")

    # -- driver -------------------------------------------------------------
    def run(self) -> bool:
        self.load()
        if not self.data:
            return self.report()
        self.check_headers_and_formats()
        self.check_sizes()
        self.check_members()
        self.check_referential_integrity()
        self.check_claim_ids()
        final = self.check_actions()
        self.check_dates_vs_members()
        self.check_timing_and_concentration()
        self.check_answer_key(final)
        return self.report()

    def report(self) -> bool:
        return report(self.checks, f"Validating {self.output_dir} against {self.key_dir}")


def report(checks: list[Check], title: str) -> bool:
    """Print PASS/FAIL per check (first few errors each); True if all passed."""
    print(f"{title}\n")
    for c in checks:
        status = "PASS" if c.passed else "FAIL"
        info = f"  ({c.info})" if c.info else ""
        print(f"[{status}] {c.name}{info}")
        for err in c.errors[:MAX_EXAMPLES]:
            print(f"         - {err}")
        if len(c.errors) > MAX_EXAMPLES:
            print(f"         ... and {len(c.errors) - MAX_EXAMPLES} more")
    n_pass = sum(c.passed for c in checks)
    print(f"\n{n_pass}/{len(checks)} checks passed")
    return n_pass == len(checks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output-dir", type=Path, default=HERE / "output")
    parser.add_argument("--answer-key-dir", type=Path, default=HERE / "answer_key")
    args = parser.parse_args(argv)
    return 0 if Validator(args.output_dir, args.answer_key_dir).run() else 1


if __name__ == "__main__":
    sys.exit(main())
