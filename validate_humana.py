"""Validate generated synthetic Humana MA files (HUM_ELIG, HUM_MEDCLM) and their answer key.

Reads only the CSVs on disk, re-derives the expected results, and compares them to
answer_key/humana/. Net paid is recomputed as a plain SUM(PAID_AMT) over every row
(reversals carry negated amounts), an independent method from the generator's
claim-family logic. Prints PASS/FAIL per check and exits non-zero on any failure.

Usage:
    python validate_humana.py [--output-dir output/humana] [--answer-key-dir answer_key/humana]
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from cclf_common import (
    KB,
    MBI_REGEX,
    add_months,
    coverage_spans,
    in_any_span,
    month_dirs,
    month_end,
    month_key,
    npi_is_valid,
    parse_month,
)
from humana_common import (
    AMOUNT_FIELDS,
    FORM_INSTITUTIONAL,
    FORM_PROFESSIONAL,
    FREQ_ORIGINAL,
    FREQ_REPLACEMENT,
    FREQ_VOID,
    HUM_FILE_COLUMNS,
    HUM_SIZE_RANGE_INITIAL,
    HUM_SIZE_RANGE_MONTHLY,
    MEMBER_ID_RE,
    PLANS,
    STATUS_DENIED,
    STATUS_PAID,
    STATUS_REVERSED,
    hum_file_name,
)
from validate_cclf import AMT_RE, DATE_RE, Check, read_csv, report

HERE = Path(__file__).resolve().parent
DX_RE = re.compile(r"^[A-Z][0-9][0-9A-Z]{1,5}$")
CAPITATION_SHARE_RANGE = (0.20, 0.40)


def d(s: str) -> date:
    return date.fromisoformat(s)


def amt(s: str) -> float:
    return float(s)


class HumanaValidator:
    def __init__(self, output_dir: Path, key_dir: Path) -> None:
        self.output_dir, self.key_dir = output_dir, key_dir
        self.checks: list[Check] = []
        self.months = [parse_month(p.name) for p in month_dirs(output_dir)]
        self.data: dict[str, dict[str, list[dict[str, str]]]] = {}

    def check(self, name: str) -> Check:
        c = Check(name)
        self.checks.append(c)
        return c

    def rows(self, ft: str):
        for mk in sorted(self.data):
            for row in self.data[mk][ft]:
                yield mk, row

    # -- loading ------------------------------------------------------------
    def load(self) -> None:
        c = self.check("both files present for every month, contiguous months, headers match")
        if not self.months:
            c.fail("no month folders found")
            return
        if self.months != [add_months(self.months[0], i) for i in range(len(self.months))]:
            c.fail("month folders are not contiguous")
        for m in self.months:
            mk = month_key(m)
            self.data[mk] = {}
            for ft, cols in HUM_FILE_COLUMNS.items():
                path = self.output_dir / mk / hum_file_name(ft, m)
                if not path.exists():
                    c.fail(f"missing {path}")
                    self.data[mk][ft] = []
                    continue
                header, rows = read_csv(path)
                if header != cols:
                    c.fail(f"{mk} {ft}: header mismatch")
                self.data[mk][ft] = rows
        c.info = f"{len(self.months)} months x 2 = {len(self.months) * 2} files expected"

    # -- checks -------------------------------------------------------------
    def check_formats_and_sizes(self) -> None:
        c = self.check("dates YYYY-MM-DD; amounts 2 decimals; ICD-10 codes, ICD_VERSION 0")
        for ft, cols in HUM_FILE_COLUMNS.items():
            date_cols = [x for x in cols if x.endswith("_DT") or x == "DOB"]
            amt_cols = [x for x in cols if x.endswith("_AMT")]
            for mk, row in self.rows(ft):
                for col in date_cols:
                    if row[col] and not DATE_RE.match(row[col]):
                        c.fail(f"{mk} {ft} {col}={row[col]!r}")
                for col in amt_cols:
                    if not AMT_RE.match(row[col]):
                        c.fail(f"{mk} {ft} {col}={row[col]!r}")
        for mk, row in self.rows("HUM_MEDCLM"):
            dx = [row[f"DX_CODE_{i}"] for i in range(1, 13)]
            if not dx[0] or row["ICD_VERSION"] != "0":
                c.fail(f"{mk} {row['CLAIM_ID']} missing principal dx or ICD_VERSION")
            if any(x and not DX_RE.match(x) for x in dx):
                c.fail(f"{mk} {row['CLAIM_ID']} malformed dx {dx}")

        c = self.check("file sizes in range (initial load and monthly)")
        first, sizes = month_key(self.months[0]), []
        for m in self.months:
            mk = month_key(m)
            for ft in HUM_FILE_COLUMNS:
                path = self.output_dir / mk / hum_file_name(ft, m)
                if not path.exists():
                    continue
                size = path.stat().st_size
                lo, hi = HUM_SIZE_RANGE_INITIAL[ft] if mk == first else HUM_SIZE_RANGE_MONTHLY
                if not lo <= size <= hi:
                    c.fail(f"{mk} {ft}: {size / KB:.1f} KB outside {lo / KB:.0f}-{hi / KB:.0f} KB")
                if mk != first:
                    sizes.append(size)
        if sizes:
            c.info = f"monthly files {min(sizes) / KB:.1f}-{max(sizes) / KB:.1f} KB"

    def check_eligibility(self) -> None:
        c = self.check("HUM_ELIG: IDs valid and 1:1, NEW first, nothing after TERM, plan rules")
        mbi_of: dict[str, str] = {}
        member_of: dict[str, str] = {}
        termed: set[str] = set()
        for mk in sorted(self.data):
            seen_this_month: set[str] = set()
            for row in self.data[mk]["HUM_ELIG"]:
                mid, mbi, kind = row["MEMBER_ID"], row["MBI"], row["RECORD_CHANGE_TYPE"]
                if not MEMBER_ID_RE.match(mid) or not MBI_REGEX.match(mbi):
                    c.fail(f"{mk} bad MEMBER_ID/MBI {mid} {mbi}")
                if mid in seen_this_month:
                    c.fail(f"{mk} {mid} delivered twice in one month")
                seen_this_month.add(mid)
                if kind not in ("NEW", "UPDATE", "TERM"):
                    c.fail(f"{mk} {mid} bad RECORD_CHANGE_TYPE {kind!r}")
                if (kind == "NEW") == (mid in mbi_of):
                    c.fail(f"{mk} {mid} {kind} but previously delivered={mid in mbi_of}")
                if mid in termed:
                    c.fail(f"{mk} {mid} delivered after TERM")
                if mbi_of.setdefault(mid, mbi) != mbi or member_of.setdefault(mbi, mid) != mid:
                    c.fail(f"{mk} MEMBER_ID/MBI not 1:1 for {mid} {mbi}")
                if kind == "TERM":
                    termed.add(mid)
                    if not row["ENROLLMENT_END_DT"]:
                        c.fail(f"{mk} {mid} TERM without ENROLLMENT_END_DT")
                product = row["PRODUCT_TYPE"]
                if product not in PLANS:
                    c.fail(f"{mk} {mid} bad PRODUCT_TYPE {product}")
                elif (row["CONTRACT_ID"], row["PBP_ID"], row["PLAN_NAME"]) != PLANS[product]:
                    c.fail(f"{mk} {mid} contract/PBP/plan name inconsistent with {product}")
                if product == "D-SNP" and row["DUAL_FLAG"] != "Y":
                    c.fail(f"{mk} {mid} D-SNP member is not dual")
                if row["FILE_AS_OF_DT"] != month_end(parse_month(mk)).isoformat():
                    c.fail(f"{mk} {mid} FILE_AS_OF_DT {row['FILE_AS_OF_DT']}")
                for col in ("PCP_NPI",):
                    if not npi_is_valid(row[col]):
                        c.fail(f"{mk} {mid} bad {col}")
        c.info = f"{len(mbi_of)} members, {len(termed)} terminated"

    def _elig_index(self):
        """first delivery month, enrollment spans, death date, and per-month state per member."""
        first, death, state = {}, {}, defaultdict(dict)
        for mk, row in self.rows("HUM_ELIG"):
            mid = row["MEMBER_ID"]
            first.setdefault(mid, mk)
            if row["DEATH_DT"]:
                death[mid] = d(row["DEATH_DT"])
            state[mid][mk] = row
        spans = coverage_spans(
            [r for _, r in self.rows("HUM_ELIG")],
            "MEMBER_ID", "ENROLLMENT_START_DT", "ENROLLMENT_END_DT",
        )  # fmt: skip
        return first, spans, death, state

    @staticmethod
    def _as_of(state: dict[str, dict], mk: str) -> dict | None:
        """Latest eligibility row delivered in or before month mk."""
        months = [m for m in state if m <= mk]
        return state[max(months)] if months else None

    def check_claims_vs_members(self) -> None:
        c = self.check("claims: member delivered same month or earlier; MBI and plan match")
        first, spans, death, state = self._elig_index()
        for mk, row in self.rows("HUM_MEDCLM"):
            mid = row["MEMBER_ID"]
            if mid not in first or first[mid] > mk:
                c.fail(f"{mk} {row['CLAIM_ID']} member {mid} not yet in HUM_ELIG")
                continue
            elig = self._as_of(state[mid], mk)
            if row["MBI"] != elig["MBI"]:
                c.fail(f"{mk} {row['CLAIM_ID']} MBI differs from eligibility")
            if (row["CONTRACT_ID"], row["PBP_ID"]) != (elig["CONTRACT_ID"], elig["PBP_ID"]):
                c.fail(f"{mk} {row['CLAIM_ID']} contract/PBP differs from eligibility")

        c = self.check("claims inside an enrollment span; none after death")
        for mk, row in self.rows("HUM_MEDCLM"):
            mid = row["MEMBER_ID"]
            lo, hi = d(row["SERVICE_FROM_DT"]), d(row["SERVICE_TO_DT"])
            if not in_any_span(spans.get(mid, []), lo, hi):
                c.fail(f"{mk} {row['CLAIM_ID']} {lo}..{hi} outside enrollment")
            if mid in death and hi > death[mid]:
                c.fail(f"{mk} {row['CLAIM_ID']} after death {death[mid]}")
        c.info = f"{len(death)} deaths"

    def _claims(self) -> dict[tuple[str, str], dict]:
        """One record per claim version keyed by (CLAIM_ID, CLAIM_FREQ_CD)."""
        out: dict[tuple[str, str], dict] = {}
        for mk, row in self.rows("HUM_MEDCLM"):
            key = (row["CLAIM_ID"], row["CLAIM_FREQ_CD"])
            rec = out.setdefault(key, {"month": mk, "header": row, "lines": {}})
            if rec["month"] != mk:
                rec["split"] = True
            rec["lines"][row["CLAIM_LINE_NUM"]] = row
        return out

    def check_keys_and_actions(self) -> dict[str, float]:
        c = self.check("unique (CLAIM_ID, CLAIM_LINE_NUM, CLAIM_FREQ_CD); claims not split")
        keys = Counter(
            (r["CLAIM_ID"], r["CLAIM_LINE_NUM"], r["CLAIM_FREQ_CD"])
            for _, r in self.rows("HUM_MEDCLM")
        )
        for k, n in keys.items():
            if n > 1:
                c.fail(f"duplicate key {k}")
        claims = self._claims()
        for k, rec in claims.items():
            if rec.get("split"):
                c.fail(f"claim version {k} spans several files")
        originals = {cid: r for (cid, f), r in claims.items() if f == FREQ_ORIGINAL}
        replacement_ids = {cid for (cid, f) in claims if f == FREQ_REPLACEMENT}
        if replacement_ids & set(originals):
            c.fail("a replacement reuses an original CLAIM_ID")
        c.info = f"{len(claims)} claim versions, {sum(keys.values())} lines"

        c = self.check("reversals negate an earlier original; replacements point to one")
        reversed_by: dict[str, dict] = {}
        replaced_by: dict[str, dict] = {}
        for (cid, freq), rec in claims.items():
            h = rec["header"]
            if freq == FREQ_VOID:
                o = originals.get(cid)
                if h["CLAIM_STATUS"] != STATUS_REVERSED or h["ORIG_CLAIM_ID"] != cid or o is None:
                    c.fail(f"reversal {cid} malformed or has no original")
                    continue
                if cid in reversed_by:
                    c.fail(f"original {cid} reversed twice")
                reversed_by[cid] = rec
                if rec["month"] <= o["month"]:
                    c.fail(f"reversal {cid} not after the original")
                if set(rec["lines"]) != set(o["lines"]):
                    c.fail(f"reversal {cid} lines differ from the original")
                for n, line in rec["lines"].items():
                    for f in AMOUNT_FIELDS:
                        if n in o["lines"] and abs(amt(line[f]) + amt(o["lines"][n][f])) > 0.005:
                            c.fail(f"reversal {cid} line {n} {f} is not the negated original")
            elif freq == FREQ_REPLACEMENT:
                orig_id = h["ORIG_CLAIM_ID"]
                o = originals.get(orig_id)
                if o is None or h["CLAIM_STATUS"] != STATUS_PAID:
                    c.fail(f"replacement {cid} has no original / bad status")
                    continue
                replaced_by[orig_id] = rec
                same = ("MEMBER_ID", "SERVICE_FROM_DT", "SERVICE_TO_DT", "CLAIM_FORM_TYPE")
                if any(h[f] != o["header"][f] for f in same):
                    c.fail(f"replacement {cid} identifying data differs from {orig_id}")
            elif freq != FREQ_ORIGINAL:
                c.fail(f"{cid} unexpected CLAIM_FREQ_CD {freq}")
        for orig_id, rec in replaced_by.items():
            rev = reversed_by.get(orig_id)
            if rev is None or rev["month"] != rec["month"]:
                c.fail(f"replacement of {orig_id} without a same-month reversal")
            elif abs(self._paid(rec) - self._paid(originals[orig_id])) < 0.005:
                c.fail(f"replacement of {orig_id} leaves PAID_AMT unchanged")
        n = len(originals)
        n_repl, n_void = len(replaced_by), len(reversed_by) - len(replaced_by)
        n_denied = sum(
            1 for o in originals.values() if o["header"]["CLAIM_STATUS"] == STATUS_DENIED
        )
        for label, k, lo, hi in (("replacements", n_repl, 0.03, 0.07),
                                 ("voids", n_void, 0.015, 0.045),
                                 ("denials", n_denied, 0.015, 0.045)):  # fmt: skip
            if not lo <= k / n <= hi:
                c.fail(f"{label} {k / n:.1%} of originals (expected {lo:.0%}-{hi:.0%})")
        c.info = (
            f"{n} originals: {n_repl} replaced ({n_repl / n:.1%}), {n_void} voided "
            f"({n_void / n:.1%}), {n_denied} denied ({n_denied / n:.1%})"
        )
        self._claims_cache, self._originals = claims, originals
        self._reversed, self._replaced = reversed_by, replaced_by
        return {}

    @staticmethod
    def _paid(rec: dict) -> float:
        return round(sum(amt(line["PAID_AMT"]) for line in rec["lines"].values()), 2)

    def check_form_and_amount_rules(self) -> None:
        c = self.check("form-type fields: I has BILL_TYPE/REVENUE_CD/CCN, P has POS/RENDERING")
        for mk, r in self.rows("HUM_MEDCLM"):
            cid = r["CLAIM_ID"]
            if r["CLAIM_FORM_TYPE"] == FORM_INSTITUTIONAL:
                if not (r["BILL_TYPE"] and r["REVENUE_CD"] and r["FACILITY_CCN"]):
                    c.fail(f"{mk} {cid} institutional row missing UB-04 fields")
                if r["PLACE_OF_SERVICE"] or r["RENDERING_NPI"]:
                    c.fail(f"{mk} {cid} institutional row has professional fields")
                if r["BILL_TYPE"][2:] != r["CLAIM_FREQ_CD"]:
                    c.fail(f"{mk} {cid} BILL_TYPE frequency digit != CLAIM_FREQ_CD")
                if r["BILL_TYPE"].startswith("11") and not (
                    r["DRG_CD"] and r["ADMIT_DT"] and r["DISCHARGE_DT"]
                ):
                    c.fail(f"{mk} {cid} inpatient claim missing DRG/admit/discharge")
            elif r["CLAIM_FORM_TYPE"] == FORM_PROFESSIONAL:
                if not (r["PLACE_OF_SERVICE"] and r["RENDERING_NPI"]):
                    c.fail(f"{mk} {cid} professional row missing POS/rendering NPI")
                if r["BILL_TYPE"] or r["REVENUE_CD"] or r["FACILITY_CCN"] or r["DRG_CD"]:
                    c.fail(f"{mk} {cid} professional row has institutional fields")
            else:
                c.fail(f"{mk} {cid} bad CLAIM_FORM_TYPE")
            for col in ("BILLING_NPI", "RENDERING_NPI", "ATTENDING_NPI"):
                if r[col] and not npi_is_valid(r[col]):
                    c.fail(f"{mk} {cid} bad {col}")

        c = self.check("amounts: BILLED > ALLOWED >= PAID; cost share = ALLOWED - PAID")
        for mk, r in self.rows("HUM_MEDCLM"):
            if r["CLAIM_STATUS"] == STATUS_REVERSED:
                continue  # checked as the negation of the original
            billed, allowed, paid = amt(r["BILLED_AMT"]), amt(r["ALLOWED_AMT"]), amt(r["PAID_AMT"])
            share = amt(r["COPAY_AMT"]) + amt(r["COINSURANCE_AMT"]) + amt(r["DEDUCTIBLE_AMT"])
            cid = f"{mk} {r['CLAIM_ID']}/{r['CLAIM_LINE_NUM']}"
            if not billed > allowed >= paid >= 0:
                c.fail(f"{cid} billed {billed} allowed {allowed} paid {paid}")
            if r["CLAIM_STATUS"] == STATUS_DENIED:
                if allowed or paid or share:
                    c.fail(f"{cid} denied line with allowed/paid/cost share")
            elif r["CAPITATED_FLAG"] == "Y":
                if r["CLAIM_FORM_TYPE"] != FORM_PROFESSIONAL or paid or not allowed:
                    c.fail(f"{cid} capitated line must be professional, paid 0, allowed > 0")
                if share > allowed + 0.005:
                    c.fail(f"{cid} capitated cost share exceeds allowed")
            elif abs(share - (allowed - paid)) > 0.005:
                c.fail(f"{cid} cost share {share:.2f} != allowed - paid {allowed - paid:.2f}")

        c = self.check("dates: PROCESSED_DT in delivery month; received/processed/paid ordered")
        for (cid, _f), rec in self._claims_cache.items():
            h, mk = rec["header"], rec["month"]
            processed, received = d(h["PROCESSED_DT"]), d(h["RECEIVED_DT"])
            if month_key(processed) != mk:
                c.fail(f"{cid} processed {processed} in file {mk}")
            if not d(h["SERVICE_TO_DT"]) <= received <= processed:
                c.fail(f"{cid} service/received/processed out of order")
            if (h["PAID_DT"] != "") != (abs(self._paid(rec)) > 0.005):
                c.fail(f"{cid} PAID_DT presence does not match a non-zero payment")
            if h["PAID_DT"] and d(h["PAID_DT"]) < processed:
                c.fail(f"{cid} paid before processed")

    def check_timing_capitation_network(self) -> None:
        c = self.check("~10% late-arriving originals (service 4-12 months before processing)")
        late = 0
        for cid, rec in self._originals.items():
            h = rec["header"]
            lag = (d(h["PROCESSED_DT"]).year * 12 + d(h["PROCESSED_DT"]).month) - (
                d(h["SERVICE_FROM_DT"]).year * 12 + d(h["SERVICE_FROM_DT"]).month
            )
            if not 0 <= lag <= 12:
                c.fail(f"{cid} lag {lag} months")
            late += lag >= 4
        share = late / len(self._originals)
        if not 0.06 <= share <= 0.14:
            c.fail(f"late share {share:.1%}")
        c.info = f"{late}/{len(self._originals)} = {share:.1%}"

        c = self.check("capitation: only PCP-rendered professional claims; ~30% of those")
        _, _, _, state = self._elig_index()
        pcp_rendered = capitated = 0
        for cid, rec in self._originals.items():
            h = rec["header"]
            if h["CLAIM_FORM_TYPE"] != FORM_PROFESSIONAL:
                continue
            elig = self._as_of(state[h["MEMBER_ID"]], rec["month"])
            own_pcp = h["RENDERING_NPI"] == elig["PCP_NPI"]
            pcp_rendered += own_pcp
            if h["CAPITATED_FLAG"] == "Y":
                capitated += 1
                if not own_pcp:
                    c.fail(f"{cid} capitated but not rendered by the member's PCP")
        share = capitated / pcp_rendered if pcp_rendered else 0
        lo, hi = CAPITATION_SHARE_RANGE
        if not lo <= share <= hi:
            c.fail(
                f"capitated share of PCP-rendered claims {share:.0%} (expected {lo:.0%}-{hi:.0%})"
            )
        c.info = f"{capitated}/{pcp_rendered} PCP-rendered professional claims = {share:.0%}"

        c = self.check("network: out-of-network claims present, all flagged Y/N")
        flags = Counter(r["IN_NETWORK_FLAG"] for _, r in self.rows("HUM_MEDCLM"))
        if set(flags) - {"Y", "N"} or not flags.get("N"):
            c.fail(f"IN_NETWORK_FLAG values {dict(flags)}")
        c.info = f"{dict(flags)} lines"

    def check_answer_key(self) -> None:
        c = self.check("answer_key/humana/final_members.csv = latest HUM_ELIG row per member")
        latest: dict[str, tuple[dict, str, str]] = {}
        for mk, row in self.rows("HUM_ELIG"):
            mid = row["MEMBER_ID"]
            latest[mid] = (row, latest[mid][1] if mid in latest else mk, mk)
        _, key_rows = read_csv(self.key_dir / "final_members.csv")
        key = {r["MEMBER_ID"]: r for r in key_rows}
        if set(key) != set(latest):
            c.fail("member sets differ")
        for mid, (row, first, last) in latest.items():
            exp = {k: v for k, v in row.items() if k != "RECORD_CHANGE_TYPE"}
            exp |= {"FIRST_DELIVERY_MONTH": first, "LAST_DELIVERY_MONTH": last}
            if key.get(mid) != exp:
                c.fail(f"{mid} differs from latest delivered row")
        c.info = f"{len(key_rows)} members"

        # Naive SUM(PAID_AMT) over every row, by the ORIGINAL's service month and form type.
        c = self.check("answer_key/humana net paid by service month = SUM(PAID_AMT) over all rows")
        svc_month = {cid: r["header"]["SERVICE_FROM_DT"][:7] for cid, r in self._originals.items()}
        totals: dict[str, dict[str, float]] = defaultdict(lambda: {"I": 0.0, "P": 0.0})
        for _mk, r in self.rows("HUM_MEDCLM"):
            family = r["CLAIM_ID"] if r["CLAIM_FREQ_CD"] != FREQ_REPLACEMENT else r["ORIG_CLAIM_ID"]
            t = totals[svc_month[family]]
            t[r["CLAIM_FORM_TYPE"]] = round(t[r["CLAIM_FORM_TYPE"]] + amt(r["PAID_AMT"]), 2)
        _, key_rows = read_csv(self.key_dir / "net_paid_by_service_month.csv")
        key = {r["service_month"]: r for r in key_rows}
        if set(key) != set(totals):
            c.fail("service months differ")
        for m, t in totals.items():
            k = key.get(m)
            if k and (abs(amt(k["institutional_net_paid"]) - t["I"]) > 0.005
                      or abs(amt(k["professional_net_paid"]) - t["P"]) > 0.005):  # fmt: skip
                c.fail(f"{m}: key I={k['institutional_net_paid']} P={k['professional_net_paid']} "
                       f"vs files I={t['I']:.2f} P={t['P']:.2f}")  # fmt: skip
        c.info = (
            f"institutional ${sum(t['I'] for t in totals.values()):,.2f}, "
            f"professional ${sum(t['P'] for t in totals.values()):,.2f}"
        )

        c = self.check("answer_key/humana net paid by processed month and capitation counts match")
        by_proc: dict[str, dict[str, float]] = defaultdict(lambda: {"I": 0.0, "P": 0.0})
        cap: dict[str, list] = defaultdict(lambda: [0, 0, 0.0])
        for mk, r in self.rows("HUM_MEDCLM"):
            t = by_proc[mk]
            t[r["CLAIM_FORM_TYPE"]] = round(t[r["CLAIM_FORM_TYPE"]] + amt(r["PAID_AMT"]), 2)
        for cid, rec in self._originals.items():
            if rec["header"]["CAPITATED_FLAG"] == "Y":
                b = cap[svc_month[cid]]
                b[0] += 1
                b[1] += len(rec["lines"])
                b[2] = round(b[2] + sum(amt(x["ALLOWED_AMT"]) for x in rec["lines"].values()), 2)
        _, key_rows = read_csv(self.key_dir / "net_paid_by_processed_month.csv")
        for r in key_rows:
            t = by_proc.get(r["processed_month"], {"I": 0.0, "P": 0.0})
            if (
                abs(amt(r["institutional_paid"]) - t["I"]) > 0.005
                or abs(amt(r["professional_paid"]) - t["P"]) > 0.005
            ):
                c.fail(f"processed month {r['processed_month']} differs")
        _, key_rows = read_csv(self.key_dir / "capitated_encounters_by_month.csv")
        got = {r["service_month"]: [int(r["capitated_claims"]), int(r["capitated_lines"]),
                                    amt(r["capitated_allowed_amt"])] for r in key_rows}  # fmt: skip
        if got != {m: [v[0], v[1], v[2]] for m, v in cap.items()}:
            c.fail("capitated encounter counts differ")
        c.info = f"{sum(v[0] for v in cap.values())} capitated encounters"

        c = self.check("answer_key/humana/claim_actions.csv matches the files")
        _, key_rows = read_csv(self.key_dir / "claim_actions.csv")
        expected = {(r["action_type"], r["original_claim_id"], r["replacement_claim_id"])
                    for r in key_rows}  # fmt: skip
        found = {("VOID", cid, "") for cid in self._reversed if cid not in self._replaced}
        found |= {
            ("REPLACE", oid, rec["header"]["CLAIM_ID"]) for oid, rec in self._replaced.items()
        }
        if expected != found:
            c.fail(f"{len(expected ^ found)} mismatched actions")

    # -- driver -------------------------------------------------------------
    def run(self) -> bool:
        self.load()
        if self.data:
            self.check_formats_and_sizes()
            self.check_eligibility()
            self.check_claims_vs_members()
            self.check_keys_and_actions()
            self.check_form_and_amount_rules()
            self.check_timing_capitation_network()
            self.check_answer_key()
        return report(self.checks, f"Validating {self.output_dir} against {self.key_dir}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output-dir", type=Path, default=HERE / "output" / "humana")
    parser.add_argument("--answer-key-dir", type=Path, default=HERE / "answer_key" / "humana")
    args = parser.parse_args(argv)
    return 0 if HumanaValidator(args.output_dir, args.answer_key_dir).run() else 1


if __name__ == "__main__":
    sys.exit(main())
