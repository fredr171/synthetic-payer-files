"""Generate synthetic, seeded CMS CCLF monthly test files (CCLF8, CCLF1, CCLF4, CCLF5).

Everything is fake: names, addresses, MBIs, HICNs, NPIs, CCNs and TINs are generated.
Beneficiaries, coverage spans and FFS <-> Humana MA switches come from the shared
member registry (member_registry.py), so generate_humana.py with the same --seed,
--start and --end sees the same people. Output is deterministic for those arguments.

Usage:
    python generate_cclf.py --seed 42 --start 2023-01 --end 2025-12

Writes:
    <output-dir>/YYYY-MM/CCLF{8,1,4,5}_YYYYMM.csv
    <answer-key-dir>/*.csv, summary.json   (expected ETL results)
    <answer-key-dir>/crossover_members.csv  (FFS <-> MA switchers, from the registry)
"""

from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from cclf_common import (
    ADJ_ADJUSTMENT,
    ADJ_CANCEL,
    ADJ_ORIGINAL,
    CCLF1_COLUMNS,
    CCLF4_COLUMNS,
    CCLF5_COLUMNS,
    CCLF8_COLUMNS,
    FILE_COLUMNS,
    KB,
    add_months,
    csv_line_bytes,
    file_name,
    fmt_amt,
    fmt_date,
    month_end,
    month_key,
    parse_month,
    print_size_summary,
    remove_stale_csvs,
    write_csv,
)
from clinical_reference import (
    ACUTE_OP_DX,
    DRGS,
    HCPCS_FEES,
    YEAR_FEE_FACTOR,
    Z_ANNUAL_WELLNESS,
    pick_dx,
    visit_lines,
    weighted,
)
from member_registry import DUAL_CODES, FFS, Address, Person, Registry, Span, moved_address

HERE = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Tunable volume parameters (chosen so files land in the size ranges in the brief).
# Population sizes live in member_registry.py (shared with the Humana generator).
# ---------------------------------------------------------------------------
MONTHLY_TARGET_BYTES = (3.75 * KB, 4.4 * KB)
INITIAL_CLAIMS_TARGET_BYTES = (9.5 * KB, 11.0 * KB)
MONTHLY_FLOOR_BYTES = 3.4 * KB  # keep drawing claims until each claims file reaches this
INITIAL_FLOOR_BYTES = 8.8 * KB
MAX_FILL_REJECTS = 200
HARD_CAP_BYTES = 4.85 * KB  # never exceed (range max is 5 KB)
LATE_ARRIVING_SHARE = 0.10
CANCEL_SHARE = 0.05
ADJUST_SHARE = 0.05
ACTION_LOOKBACK_MONTHS = 6

# Professional weights, in the registry's shared-professional order.
PRO_WEIGHTS = [16, 14, 12, 12, 10, 8, 8, 6, 6]

# Part A claim types: code -> weight
CLAIM_TYPE_WEIGHTS = {"40": 64, "60": 15, "20": 5, "30": 2, "10": 8, "50": 6}
# (bill facility type, bill classification)
BILL_TYPE = {
    "40": ("1", "3"),
    "60": ("1", "1"),
    "20": ("2", "1"),
    "30": ("1", "8"),
    "10": ("3", "2"),
    "50": ("8", "1"),
}
FACILITY_ROLE_FOR_TYPE = {
    "40": "HOSPITAL",
    "60": "HOSPITAL",
    "30": "HOSPITAL",
    "20": "SNF",
    "10": "HHA",
    "50": "HOSPICE",
}
DX_COUNT_WEIGHTS = {
    "40": ([1, 2, 3], [36, 40, 24]),
    "60": ([2, 3, 4, 5, 6], [10, 25, 30, 20, 15]),
    "20": ([2, 3, 4, 5], [25, 35, 25, 15]),
    "30": ([2, 3, 4], [40, 40, 20]),
    "10": ([1, 2, 3, 4], [20, 35, 30, 15]),
    "50": ([1, 2, 3], [40, 40, 20]),
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class Bene:
    """CCLF view of a registry person: current CCLF8 attribute values."""

    person: Person
    first_month: date  # first CCLF8 delivery
    spans: list[Span]  # FFS coverage spans (registry truth, including future ones)
    rng_bgn: date  # BENE_RNG_BGN_DT of the current span
    rng_end: date | None  # BENE_RNG_END_DT once the beneficiary leaves FFS
    dual: str
    part_b_bgn: date | None
    address: Address
    death_reveal_month: date | None = None
    death_revealed: bool = False

    @property
    def mbi(self) -> str:
        return self.person.mbi

    @property
    def hic(self) -> str:
        return self.person.hic

    @property
    def weight(self) -> float:
        return self.person.weight

    @property
    def conditions(self) -> list[str]:
        return self.person.conditions

    @property
    def true_death(self) -> date | None:
        return self.person.true_death

    @property
    def frozen(self) -> bool:
        """Switchers change only through registry events (keeps payers consistent)."""
        return self.person.switcher

    def in_ffs(self, month: date) -> bool:
        return any(s.start <= month_end(month) and (s.end is None or s.end >= month)
                   for s in self.spans)  # fmt: skip

    def buyin(self) -> str:
        """Entitlement buy-in indicator derived from Part B and dual status."""
        if self.dual != "NA":
            return "C" if self.part_b_bgn else "A"
        return "3" if self.part_b_bgn else "1"

    def to_row(self, as_of: date, change_type: str) -> list[str]:
        """Full current-state CCLF8 row as of a delivery month."""
        p, a = self.person, self.address
        age = as_of.year - p.dob.year - ((as_of.month, as_of.day) < (p.dob.month, p.dob.day))
        death = p.true_death if self.death_revealed else None
        values = {
            "BENE_MBI_ID": p.mbi,
            "BENE_HIC_NUM": p.hic,
            "BENE_FIPS_STATE_CD": a.state_fips,
            "BENE_FIPS_CNTY_CD": a.county_fips,
            "BENE_ZIP_CD": a.zip5 + a.zip4,
            "BENE_DOB": fmt_date(p.dob),
            "BENE_SEX_CD": p.sex,
            "BENE_RACE_CD": p.race,
            "BENE_AGE": str(age),
            "BENE_MDCR_STUS_CD": p.mdcr_stus,
            "BENE_DUAL_STUS_CD": self.dual,
            "BENE_DEATH_DT": fmt_date(death),
            "BENE_RNG_BGN_DT": fmt_date(self.rng_bgn),
            "BENE_RNG_END_DT": fmt_date(death or self.rng_end),
            "BENE_1ST_NAME": p.first,
            "BENE_MIDL_NAME": p.middle,
            "BENE_LAST_NAME": p.last,
            "BENE_ORGNL_ENTLMT_RSN_CD": p.orig_entl,
            "BENE_ENTLMT_BUYIN_IND": self.buyin(),
            "BENE_PART_A_ENRLMT_BGN_DT": fmt_date(p.part_a_bgn),
            "BENE_PART_B_ENRLMT_BGN_DT": fmt_date(self.part_b_bgn),
            "BENE_LINE_1_ADR": a.line1,
            "GEO_ZIP_PLC_NAME": a.city,
            "GEO_USPS_STATE_CD": a.usps,
            "GEO_ZIP5_CD": a.zip5,
            "RECORD_CHANGE_TYPE": change_type,
        }
        return [values[c] for c in CCLF8_COLUMNS]


@dataclass
class Claim:
    """One delivered claim version (original, cancellation, or adjustment)."""

    part: str  # "A" or "B"
    clm_id: str
    cntl: str
    adj_type: str
    mbi: str
    from_dt: date
    thru_dt: date
    load_month: date
    amount: float  # CLM_PMT_AMT (A) or sum of line paid (B)
    header: list[str] | None = None  # CCLF1 row
    dx_rows: list[list[str]] = field(default_factory=list)  # CCLF4 rows
    lines: list[list[str]] = field(default_factory=list)  # CCLF5 rows
    actioned: bool = False
    late: bool = False


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------
class CclfGenerator:
    """Emits monthly CCLF deliveries for the FFS side of the shared registry."""

    def __init__(self, seed: int, start: date, end: date) -> None:
        self.rng = random.Random(seed)
        self.registry = Registry(seed, start, end)
        self.months = self.registry.months
        self.start, self.end = self.months[0], self.months[-1]
        self.benes: list[Bene] = []
        self.by_mbi: dict[str, Bene] = {}
        self.next_claim_id = 1_000_000_000_000 + self.rng.randint(0, 99_999_999)
        self.next_cntl_seq = self.rng.randint(1_000_000, 2_000_000)
        self.originals: list[Claim] = []
        self.actions: list[tuple[str, Claim, Claim]] = []  # (kind, original, action)
        self.member_changes: list[dict] = []
        self.action_carry: dict[tuple[str, str], float] = defaultdict(float)
        self.files: dict[date, dict[str, list[list[str]]]] = {}
        self.facilities = self.registry.providers_for(FFS, "FACILITY")
        self.professionals = self.registry.providers_for(FFS, "PROFESSIONAL")
        # Registry persons with FFS coverage, grouped by first CCLF8 delivery month.
        self.ffs_by_first_month: dict[date, list[Person]] = defaultdict(list)
        for p in self.registry.persons:
            first = self.registry.first_delivery(p, FFS)
            if first is not None:
                self.ffs_by_first_month[first].append(p)

    # -- identifiers --------------------------------------------------------
    def _claim_id(self) -> str:
        self.next_claim_id += self.rng.randint(1, 40)
        return str(self.next_claim_id)

    def _cntl(self, part: str, efctv: date) -> str:
        self.next_cntl_seq += self.rng.randint(1, 25)
        julian = efctv.timetuple().tm_yday
        return f"{part}{efctv.year % 100:02d}{julian:03d}{self.next_cntl_seq:08d}"

    def _weighted(self, pairs: list[tuple[str, int]]) -> str:
        return weighted(self.rng, pairs)

    # -- population ---------------------------------------------------------
    def _attribute(self, person: Person, month: date) -> Bene:
        """Create the CCLF view of a registry person at their first delivery."""
        spans = person.spans_for(FFS)
        first_span = spans[0]
        bene = Bene(
            person=person,
            first_month=month,
            spans=spans,
            rng_bgn=first_span.start,
            rng_end=None,
            dual=person.dual,
            part_b_bgn=person.part_b_bgn,
            address=person.address_as_of(month),
        )
        if person.true_death:
            death_month = date(person.true_death.year, person.true_death.month, 1)
            reveal = add_months(death_month, self.rng.choice([0, 1]))
            bene.death_reveal_month = min(reveal, self.end)
        self.benes.append(bene)
        self.by_mbi[person.mbi] = bene
        return bene

    # -- CCLF8 --------------------------------------------------------------
    def _cclf8_month(self, month: date, initial: bool) -> list[list[str]]:
        rows: list[list[str]] = []
        touched: set[str] = set()

        def emit(bene: Bene, change: str, detail: str) -> None:
            if bene.frozen:  # registry-planned address as of this month
                bene.address = bene.person.address_as_of(month)
            rows.append(bene.to_row(month, "NEW" if change == "NEW" else "UPDATE"))
            touched.add(bene.mbi)
            self.member_changes.append(
                {"delivery_month": month_key(month), "BENE_MBI_ID": bene.mbi,
                 "change_type": change, "details": detail}
            )  # fmt: skip

        new_people = self.ffs_by_first_month.get(month, [])
        for person in new_people:
            reason = "initial population" if initial else (
                "switched from MA" if person.spans[0].payer != FFS else "newly attributed"
            )  # fmt: skip
            emit(self._attribute(person, month), "NEW", reason)
        if initial:
            return rows

        # Coverage changes from the registry: leaving FFS (to MA) and returning to FFS.
        for bene in self.benes:
            if bene.mbi in touched:
                continue
            for span in bene.spans:
                if span.end is not None and span.end + timedelta(days=1) == month:
                    bene.rng_end = span.end
                    emit(bene, "FFS_END", f"FFS coverage ends {span.end.isoformat()}")
                elif span.start == month and span.start != bene.spans[0].start:
                    bene.rng_bgn, bene.rng_end = span.start, None
                    emit(bene, "FFS_RETURN", f"FFS coverage resumes {span.start.isoformat()}")
        for bene in self.benes:
            if bene.death_reveal_month == month and not bene.death_revealed:
                bene.death_revealed = True
                emit(bene, "DEATH", f"death date {bene.true_death.isoformat()}")
        for bene in self.benes:  # registry-planned moves of switchers while in FFS
            if bene.mbi in touched or not bene.in_ffs(month):
                continue
            if any(when == month for when, _ in bene.person.moves):
                old_zip = bene.address.zip5
                bene.address = bene.person.address_as_of(month)
                emit(bene, "ADDRESS", f"ZIP {old_zip} -> {bene.address.zip5}")

        target = self.rng.uniform(*MONTHLY_TARGET_BYTES)
        size = csv_line_bytes(CCLF8_COLUMNS) + sum(csv_line_bytes(r) for r in rows)
        candidates = [
            b for b in self.benes
            if b.mbi not in touched and b.first_month < month and not b.death_revealed
            and not b.frozen and b.in_ffs(month)
            and not (b.true_death and b.true_death < add_months(month, 1))
        ]  # fmt: skip
        for _attempt in range(200):
            if not candidates:
                break
            bene = candidates.pop(self.rng.randrange(len(candidates)))
            # Budget check before mutating, with slack for a longer address/ZIP afterwards.
            if size + csv_line_bytes(bene.to_row(month, "UPDATE")) + 8 > target:
                break
            change, detail = self._apply_change(bene, month)
            size += csv_line_bytes(bene.to_row(month, "UPDATE"))
            emit(bene, change, detail)
        return rows

    def _apply_change(self, bene: Bene, month: date) -> tuple[str, str]:
        """Mutate one attribute of a (non-switcher) beneficiary; returns (type, detail)."""
        r = self.rng
        kinds = [("ADDRESS", 65), ("DUAL", 20)]
        if bene.part_b_bgn is None:
            kinds.append(("PARTB", 80))
        kind = self._weighted(kinds)
        if kind == "PARTB":
            bene.part_b_bgn = month
            return kind, f"Part B enrollment begins {month.isoformat()}"
        if kind == "DUAL":
            old = bene.dual
            if old == "NA":
                bene.dual = self._weighted(DUAL_CODES)
            elif r.random() < 0.5:
                bene.dual = "NA"
            else:
                bene.dual = self._weighted([c for c in DUAL_CODES if c[0] != old])
            return kind, f"dual status {old} -> {bene.dual}"
        old_zip = bene.address.zip5
        bene.address = moved_address(r, bene.address)
        return "ADDRESS", f"ZIP {old_zip} -> {bene.address.zip5}"

    # -- claim helpers ------------------------------------------------------
    def _lag_months(self, late: bool) -> int:
        """Months between service and load: 4-12 if late-arriving, else 0-3."""
        if late:
            return self.rng.randint(4, 12)
        return self.rng.choices([0, 1, 2, 3], weights=[22, 40, 25, 13])[0]

    def _load_dates(self, month: date) -> tuple[date, date]:
        """(CLM_EFCTV_DT, CLM_IDR_LD_DT); load date always inside the delivery month."""
        load = month + timedelta(days=self.rng.randint(4, month_end(month).day - 1))
        return load - timedelta(days=self.rng.randint(1, 4)), load

    def _eligible(self, month: date, part: str) -> list[Bene]:
        return [
            b for b in self.benes
            if b.first_month <= month and (part == "A" or b.part_b_bgn is not None)
        ]  # fmt: skip

    def _service_window(
        self, bene: Bene, svc_month: date, latest_thru: date, part: str, los: int
    ) -> tuple[date, date] | None:
        """Random (from, thru) in svc_month inside one FFS span, before death and load."""
        span = bene.person.span_overlapping(FFS, svc_month, month_end(svc_month))
        if span is None:
            return None
        earliest = max(svc_month, span.start)
        if part == "B":
            earliest = max(earliest, bene.part_b_bgn)
        if span.end is not None:
            latest_thru = min(latest_thru, span.end)
        if bene.true_death:
            latest_thru = min(latest_thru, bene.true_death)
        latest_from = min(month_end(svc_month), latest_thru - timedelta(days=los))
        if latest_from < earliest:
            return None
        from_dt = earliest + timedelta(days=self.rng.randint(0, (latest_from - earliest).days))
        return from_dt, from_dt + timedelta(days=los)

    def _pick_dx(self, bene: Bene, principal: str, count: int) -> list[str]:
        return pick_dx(self.rng, bene.conditions, principal, count)

    # -- Part A -------------------------------------------------------------
    def _new_part_a(self, month: date, clm_type: str | None = None) -> Claim | None:
        r = self.rng
        if clm_type is None:
            types, weights = list(CLAIM_TYPE_WEIGHTS), list(CLAIM_TYPE_WEIGHTS.values())
            clm_type = r.choices(types, weights=weights)[0]
        efctv, load = self._load_dates(month)
        benes = self._eligible(month, "A")
        weights = [b.weight for b in benes]
        late = r.random() < LATE_ARRIVING_SHARE  # fixed per claim; only the bene is retried
        for _ in range(60):
            bene = r.choices(benes, weights=weights)[0]
            lag = self._lag_months(late)
            drg = None
            if clm_type == "60":
                drg = r.choices(DRGS, weights=[d[6] for d in DRGS])[0]
                los = r.randint(*drg[3])
            elif clm_type == "20":
                los = r.randint(8, 35)
            elif clm_type == "30":
                los = r.randint(5, 14)
            elif clm_type == "10":
                los = 29
            elif clm_type == "50":
                los = r.randint(14, 27)
            else:
                los = 0
            window = self._service_window(bene, add_months(month, -lag), efctv, "A", los)
            if window:
                break
        else:
            return None
        from_dt, thru_dt = window
        days = max((thru_dt - from_dt).days, 1)

        admsn_type = admsn_src = admit_dx = drg_cd = op_srvc = ""
        if clm_type == "60":
            drg_cd, principal, admit_dx, _, pay_rng, admsn_type, _ = drg
            pmt = r.uniform(*pay_rng)
            admsn_src = "1" if admsn_type == "3" else r.choice("12")
            status = self._weighted([("01", 60), ("06", 20), ("03", 15), ("62", 5)])
            charge_factor = r.uniform(2.8, 4.2)
        elif clm_type in ("20", "30"):
            principal = self._weighted([("M1711", 2), ("I5022", 2), ("J189", 1), ("S72001A", 2)])
            pmt = days * r.uniform(520, 640)
            admsn_type, admsn_src = "3", "4"
            status = self._weighted([("01", 70), ("06", 30)])
            charge_factor = r.uniform(1.4, 1.9)
        elif clm_type == "10":
            principal = self._weighted([("I5022", 3), ("J449", 3), ("M1711", 2), ("E1165", 2)])
            pmt = r.uniform(1900, 3300)
            status = "01"
            charge_factor = r.uniform(1.2, 1.6)
        elif clm_type == "50":
            principal = self._weighted([("G309", 3), ("I509", 3), ("J449", 2), ("C3490", 2)])
            pmt = (days + 1) * r.uniform(205, 225)
            status = "30" if thru_dt == month_end(thru_dt) else "01"
            charge_factor = r.uniform(1.1, 1.3)
        else:  # 40 outpatient: single day
            principal = (
                self._weighted(ACUTE_OP_DX) if r.random() < 0.35 else r.choice(bene.conditions)
            )
            pmt = min(r.lognormvariate(5.9, 0.75), 4200)
            status = "01"
            op_srvc = r.choice("1333")
            charge_factor = r.uniform(2.5, 4.5)

        role = FACILITY_ROLE_FOR_TYPE[clm_type]
        fac = r.choice([f for f in self.facilities if f.specialty == role])
        atndg = r.choice([p for p in self.professionals if p.specialty in ("08", "11", "06")])
        fac_type, clsfctn = BILL_TYPE[clm_type]
        dx_list = self._pick_dx(bene, principal, r.choices(*DX_COUNT_WEIGHTS[clm_type])[0])
        claim = Claim(
            part="A", clm_id=self._claim_id(), cntl=self._cntl("A", efctv), adj_type=ADJ_ORIGINAL,
            mbi=bene.mbi, from_dt=from_dt, thru_dt=thru_dt, load_month=month,
            amount=round(pmt, 2), late=late,
        )  # fmt: skip
        values = {
            "CUR_CLM_UNIQ_ID": claim.clm_id,
            "PRVDR_OSCAR_NUM": fac.ccn,
            "BENE_MBI_ID": bene.mbi,
            "BENE_HIC_NUM": bene.hic,
            "CLM_TYPE_CD": clm_type,
            "CLM_FROM_DT": fmt_date(from_dt),
            "CLM_THRU_DT": fmt_date(thru_dt),
            "CLM_BILL_FAC_TYPE_CD": fac_type,
            "CLM_BILL_CLSFCTN_CD": clsfctn,
            "PRNCPL_DGNS_CD": principal,
            "ADMTG_DGNS_CD": admit_dx,
            "CLM_MDCR_NPMT_RSN_CD": "",
            "CLM_PMT_AMT": fmt_amt(claim.amount),
            "CLM_NCH_PRMRY_PYR_CD": "",
            "PRVDR_FAC_FIPS_ST_CD": "39",
            "BENE_PTNT_STUS_CD": status,
            "DGNS_DRG_CD": drg_cd,
            "CLM_OP_SRVC_TYPE_CD": op_srvc,
            "FAC_PRVDR_NPI_NUM": fac.npi,
            "ATNDG_PRVDR_NPI_NUM": atndg.npi,
            "CLM_ADJSMT_TYPE_CD": ADJ_ORIGINAL,
            "CLM_EFCTV_DT": fmt_date(efctv),
            "CLM_IDR_LD_DT": fmt_date(load),
            "CLM_ADMSN_TYPE_CD": admsn_type,
            "CLM_ADMSN_SRC_CD": admsn_src,
            "CLM_BILL_FREQ_CD": "1",
            "DGNS_PRCDR_ICD_IND": "0",
            "CLM_MDCR_INSTNL_TOT_CHRG_AMT": fmt_amt(claim.amount * charge_factor),
            "CLM_CNTL_NUM": claim.cntl,
            "CLM_ORG_CNTL_NUM": "",
        }
        claim.header = [values[c] for c in CCLF1_COLUMNS]
        for seq, dx in enumerate(dx_list):
            poa = ""
            if clm_type == "60":
                poa = "Y" if seq == 0 else self._weighted([("Y", 85), ("N", 8), ("W", 4), ("U", 3)])
            dx_values = {
                "CUR_CLM_UNIQ_ID": claim.clm_id,
                "BENE_MBI_ID": bene.mbi,
                "BENE_HIC_NUM": bene.hic,
                "CLM_TYPE_CD": clm_type,
                "CLM_PROD_TYPE_CD": "P" if seq == 0 else "D",
                "CLM_VAL_SQNC_NUM": str(seq),
                "CLM_DGNS_CD": dx,
                "PRVDR_OSCAR_NUM": fac.ccn,
                "CLM_FROM_DT": fmt_date(from_dt),
                "CLM_THRU_DT": fmt_date(thru_dt),
                "CLM_POA_IND": poa,
                "DGNS_PRCDR_ICD_IND": "0",
            }
            claim.dx_rows.append([dx_values[c] for c in CCLF4_COLUMNS])
        return claim

    def _part_a_action(self, original: Claim, kind: str, month: date) -> Claim:
        """Cancellation (negated amounts) or adjustment (new amount) of a Part A original."""
        efctv, load = self._load_dates(month)
        idx = {c: i for i, c in enumerate(CCLF1_COLUMNS)}
        header = list(original.header)
        new_id = self._claim_id()
        if kind == ADJ_CANCEL:
            amount = -original.amount
            charges = -float(header[idx["CLM_MDCR_INSTNL_TOT_CHRG_AMT"]])
            freq = "8"
        else:
            amount = original.amount
            while abs(amount - original.amount) < 1:
                amount = round(original.amount * self.rng.uniform(0.6, 1.25), 2)
            charges = float(header[idx["CLM_MDCR_INSTNL_TOT_CHRG_AMT"]])
            freq = "7"
        header[idx["CUR_CLM_UNIQ_ID"]] = new_id
        header[idx["CLM_PMT_AMT"]] = fmt_amt(amount)
        header[idx["CLM_MDCR_INSTNL_TOT_CHRG_AMT"]] = fmt_amt(charges)
        header[idx["CLM_ADJSMT_TYPE_CD"]] = kind
        header[idx["CLM_BILL_FREQ_CD"]] = freq
        header[idx["CLM_EFCTV_DT"]] = fmt_date(efctv)
        header[idx["CLM_IDR_LD_DT"]] = fmt_date(load)
        header[idx["CLM_ORG_CNTL_NUM"]] = original.cntl
        dx_rows = [[new_id] + row[1:] for row in original.dx_rows]
        return Claim(
            part="A", clm_id=new_id, cntl=original.cntl, adj_type=kind, mbi=original.mbi,
            from_dt=original.from_dt, thru_dt=original.thru_dt, load_month=month,
            amount=round(amount, 2), header=header, dx_rows=dx_rows,
        )  # fmt: skip

    # -- Part B -------------------------------------------------------------
    def _new_part_b(self, month: date) -> Claim | None:
        r = self.rng
        efctv, load = self._load_dates(month)
        benes = self._eligible(month, "B")
        weights = [b.weight for b in benes]
        late = r.random() < LATE_ARRIVING_SHARE
        for _ in range(60):
            bene = r.choices(benes, weights=weights)[0]
            lag = self._lag_months(late)
            window = self._service_window(bene, add_months(month, -lag), efctv, "B", 0)
            if window:
                break
        else:
            return None
        svc_dt = window[0]
        pro = r.choices(self.professionals, weights=PRO_WEIGHTS)[0]
        codes = visit_lines(self.rng, pro.specialty)
        if codes[0][0] == "G0439":
            dx = [Z_ANNUAL_WELLNESS] + self._pick_dx(bene, bene.conditions[0], 2)[:2]
        else:
            dx = self._pick_dx(bene, r.choice(bene.conditions), r.choice([1, 2, 2, 3]))
        dx = dx[:3]
        claim = Claim(
            part="B", clm_id=self._claim_id(), cntl=self._cntl("B", efctv), adj_type=ADJ_ORIGINAL,
            mbi=bene.mbi, from_dt=svc_dt, thru_dt=svc_dt, load_month=month, amount=0.0,
            late=late,
        )  # fmt: skip
        factor = YEAR_FEE_FACTOR.get(svc_dt.year, 1.0)
        for line_num, (hcpcs, mdfr) in enumerate(codes, start=1):
            base, fed_tos, paid_share = HCPCS_FEES[hcpcs]
            allowed = round(base * factor * r.uniform(0.97, 1.03), 2)
            paid = round(allowed * paid_share, 2)
            claim.amount = round(claim.amount + paid, 2)
            line_dx = dx[0] if hcpcs != "36415" else dx[-1]
            values = {
                "CUR_CLM_UNIQ_ID": claim.clm_id,
                "CLM_LINE_NUM": str(line_num),
                "BENE_MBI_ID": bene.mbi,
                "BENE_HIC_NUM": bene.hic,
                "CLM_TYPE_CD": "71",
                "CLM_FROM_DT": fmt_date(svc_dt),
                "CLM_THRU_DT": fmt_date(svc_dt),
                "RNDRG_PRVDR_TYPE_CD": "1",
                "RNDRG_PRVDR_FIPS_ST_CD": "39",
                "CLM_PRVDR_SPCLTY_CD": pro.specialty,
                "CLM_FED_TYPE_SRVC_CD": fed_tos,
                "CLM_POS_CD": "11",
                "CLM_LINE_FROM_DT": fmt_date(svc_dt),
                "CLM_LINE_THRU_DT": fmt_date(svc_dt),
                "CLM_LINE_HCPCS_CD": hcpcs,
                "CLM_LINE_CVRD_PD_AMT": fmt_amt(paid),
                "CLM_LINE_PRMRY_PYR_CD": "",
                "CLM_LINE_DGNS_CD": line_dx,
                "CLM_RNDRG_PRVDR_TAX_NUM": pro.tin,
                "RNDRG_PRVDR_NPI_NUM": pro.npi,
                "CLM_CARR_PMT_DNL_CD": "1",
                "CLM_PRCSG_IND_CD": "A",
                "CLM_ADJSMT_TYPE_CD": ADJ_ORIGINAL,
                "CLM_EFCTV_DT": fmt_date(efctv),
                "CLM_IDR_LD_DT": fmt_date(load),
                "CLM_CNTL_NUM": claim.cntl,
                "CLM_LINE_ALOWD_CHRG_AMT": fmt_amt(allowed),
                "CLM_LINE_SRVC_UNIT_QTY": "1",
                "HCPCS_1_MDFR_CD": mdfr,
                "CLM_DGNS_1_CD": dx[0],
                "CLM_DGNS_2_CD": dx[1] if len(dx) > 1 else "",
                "CLM_DGNS_3_CD": dx[2] if len(dx) > 2 else "",
                "DGNS_PRCDR_ICD_IND": "0",
            }
            claim.lines.append([values[c] for c in CCLF5_COLUMNS])
        return claim

    def _part_b_action(self, original: Claim, kind: str, month: date) -> Claim:
        """Cancellation (all lines negated) or adjustment (one line repriced) of a Part B claim."""
        efctv, load = self._load_dates(month)
        idx = {c: i for i, c in enumerate(CCLF5_COLUMNS)}
        new_id = self._claim_id()
        repriced = self.rng.randrange(len(original.lines))
        lines, total = [], 0.0
        for n, line in enumerate(original.lines):
            row = list(line)
            paid = float(row[idx["CLM_LINE_CVRD_PD_AMT"]])
            allowed = float(row[idx["CLM_LINE_ALOWD_CHRG_AMT"]])
            if kind == ADJ_CANCEL:
                paid, allowed = -paid, -allowed
            elif n == repriced:
                share = paid / allowed if allowed else 0.8
                new_allowed = allowed
                while abs(new_allowed - allowed) < 0.5:
                    new_allowed = round(allowed * self.rng.uniform(0.7, 1.2), 2)
                    if allowed < 5:  # cheap lab lines: force a visible change
                        new_allowed = round(allowed + self.rng.choice([-1, 1]) * 1.0, 2)
                allowed, paid = new_allowed, round(new_allowed * share, 2)
            row[idx["CUR_CLM_UNIQ_ID"]] = new_id
            row[idx["CLM_LINE_CVRD_PD_AMT"]] = fmt_amt(paid)
            row[idx["CLM_LINE_ALOWD_CHRG_AMT"]] = fmt_amt(allowed)
            row[idx["CLM_ADJSMT_TYPE_CD"]] = kind
            row[idx["CLM_EFCTV_DT"]] = fmt_date(efctv)
            row[idx["CLM_IDR_LD_DT"]] = fmt_date(load)
            total = round(total + paid, 2)
            lines.append(row)
        return Claim(
            part="B", clm_id=new_id, cntl=original.cntl, adj_type=kind, mbi=original.mbi,
            from_dt=original.from_dt, thru_dt=original.thru_dt, load_month=month,
            amount=total, lines=lines,
        )  # fmt: skip

    # -- monthly claims -----------------------------------------------------
    def _actions_for_month(self, month: date, part: str) -> list[Claim]:
        """Cancel/adjust earlier-month originals; at most one action per original.

        The count is a quota: share x last month's claim volume, with the fractional
        remainder carried forward, so the overall share stays close to 5% for any seed.
        """
        out: list[Claim] = []
        pool = [
            c for c in self.originals
            if c.part == part and not c.actioned
            and add_months(month, -ACTION_LOOKBACK_MONTHS) <= c.load_month < month
        ]  # fmt: skip
        prev = add_months(month, -1)
        volume = sum(1 for c in self.originals if c.part == part and c.load_month == prev)
        volume += sum(1 for _, _, a in self.actions if a.part == part and a.load_month == prev)
        for kind, share in ((ADJ_CANCEL, CANCEL_SHARE), (ADJ_ADJUSTMENT, ADJUST_SHARE)):
            # Small jitter keeps month-to-month counts from looking mechanical.
            self.action_carry[(part, kind)] += share * volume * self.rng.uniform(0.8, 1.2)
            n = int(self.action_carry[(part, kind)])
            self.action_carry[(part, kind)] -= n
            for _ in range(n):
                if not pool:
                    break
                original = pool.pop(self.rng.randrange(len(pool)))
                original.actioned = True
                build = self._part_a_action if part == "A" else self._part_b_action
                action = build(original, kind, month)
                self.actions.append((kind, original, action))
                out.append(action)
        return out

    def _claims_month(self, month: date, initial: bool) -> dict[str, list[list[str]]]:
        lo_hi = INITIAL_CLAIMS_TARGET_BYTES if initial else MONTHLY_TARGET_BYTES
        out = {"CCLF1": [], "CCLF4": [], "CCLF5": []}

        floor = INITIAL_FLOOR_BYTES if initial else MONTHLY_FLOOR_BYTES
        hard_cap = INITIAL_CLAIMS_TARGET_BYTES[1] + 0.5 * KB if initial else HARD_CAP_BYTES
        # Part A: CCLF1 + CCLF4 share one claim stream. A claim that would overflow
        # either file ends the month once both files reach the floor; otherwise it is
        # discarded and another claim is drawn.
        target = self.rng.uniform(*lo_hi)
        size1, size4 = csv_line_bytes(CCLF1_COLUMNS), csv_line_bytes(CCLF4_COLUMNS)
        stream = [] if initial else self._actions_for_month(month, "A")
        rejects = 0
        while rejects < MAX_FILL_REJECTS:
            # One file full, the other short: top up with the claim type that grows the
            # short file faster (inpatient: 2-6 dx rows; outpatient: 1-3), letting the
            # full file run past its soft target up to the hard cap.
            fill_dx = size1 >= target - 300 and size4 < floor
            fill_hdr = size4 >= target - 150 and size1 < floor
            cap1 = hard_cap if fill_dx else target
            cap4 = hard_cap if fill_hdr else target
            forced = "60" if fill_dx else "40" if fill_hdr else None
            claim = stream.pop(0) if stream else self._new_part_a(month, clm_type=forced)
            if claim is None:
                break
            add1 = csv_line_bytes(claim.header)
            add4 = sum(csv_line_bytes(r) for r in claim.dx_rows)
            if claim.adj_type == ADJ_ORIGINAL and (size1 + add1 > cap1 or size4 + add4 > cap4):
                if size1 >= floor and size4 >= floor:
                    break
                rejects += 1
                continue
            size1, size4 = size1 + add1, size4 + add4
            out["CCLF1"].append(claim.header)
            out["CCLF4"].extend(claim.dx_rows)
            if claim.adj_type == ADJ_ORIGINAL:
                self.originals.append(claim)

        # Part B: CCLF5 lines.
        target = self.rng.uniform(*lo_hi)
        size5 = csv_line_bytes(CCLF5_COLUMNS)
        stream = [] if initial else self._actions_for_month(month, "B")
        rejects = 0
        while rejects < MAX_FILL_REJECTS:
            claim = stream.pop(0) if stream else self._new_part_b(month)
            if claim is None:
                break
            add5 = sum(csv_line_bytes(r) for r in claim.lines)
            if claim.adj_type == ADJ_ORIGINAL and size5 + add5 > target:
                if size5 >= floor:
                    break
                rejects += 1
                continue
            size5 += add5
            out["CCLF5"].extend(claim.lines)
            if claim.adj_type == ADJ_ORIGINAL:
                self.originals.append(claim)
        return out

    # -- driver -------------------------------------------------------------
    def run(self) -> None:
        for i, month in enumerate(self.months):
            initial = i == 0
            files = {"CCLF8": self._cclf8_month(month, initial)}
            files.update(self._claims_month(month, initial))
            self.files[month] = files

    # -- output -------------------------------------------------------------
    def write_files(self, output_dir: Path) -> dict[tuple[str, str], int]:
        """Write CSVs; returns {(month_key, file_type): size_bytes}."""
        remove_stale_csvs(output_dir, pattern="*/CCLF*.csv")
        sizes = {}
        for month, files in self.files.items():
            folder = output_dir / month_key(month)
            folder.mkdir(parents=True, exist_ok=True)
            for file_type in ("CCLF8", "CCLF1", "CCLF4", "CCLF5"):
                path = folder / file_name(file_type, month)
                write_csv(path, FILE_COLUMNS[file_type], files[file_type])
                sizes[(month_key(month), file_type)] = path.stat().st_size
        return sizes

    def write_answer_key(self, key_dir: Path) -> None:
        """Expected ETL results derived from the simulation (not by re-reading the CSVs)."""
        remove_stale_csvs(key_dir, pattern="*.csv")
        key_dir.mkdir(parents=True, exist_ok=True)

        # Final member table: the last delivered row for every beneficiary.
        latest: dict[str, tuple[list[str], str, str]] = {}
        for month, files in self.files.items():
            for row in files["CCLF8"]:
                first = latest[row[0]][1] if row[0] in latest else month_key(month)
                latest[row[0]] = (row, first, month_key(month))
        member_cols = CCLF8_COLUMNS[:-1] + ["FIRST_DELIVERY_MONTH", "LAST_DELIVERY_MONTH"]
        write_csv(
            key_dir / "final_members.csv",
            member_cols,
            [row[:-1] + [first, last] for mbi, (row, first, last) in sorted(latest.items())],
        )

        # Net paid after cancellations/adjustments: one final amount per claim family.
        final_amount = {(c.part, c.cntl): c.amount for c in self.originals}
        for kind, original, action in self.actions:
            final_amount[(original.part, original.cntl)] = (
                0.0 if kind == ADJ_CANCEL else action.amount
            )
        by_service: dict[str, dict[str, float]] = {}
        for c in self.originals:
            m = month_key(c.from_dt)
            bucket = by_service.setdefault(m, {"A": 0.0, "B": 0.0})
            bucket[c.part] = round(bucket[c.part] + final_amount[(c.part, c.cntl)], 2)
        write_csv(
            key_dir / "net_paid_by_service_month.csv",
            ["service_month", "part_a_net_paid", "part_b_net_paid", "total_net_paid"],
            [
                [m, fmt_amt(v["A"]), fmt_amt(v["B"]), fmt_amt(v["A"] + v["B"])]
                for m, v in sorted(by_service.items())
            ],
        )

        # Net change booked in each delivery month (original + adjustment delta - cancel).
        by_load = {month_key(m): {"A": 0.0, "B": 0.0} for m in self.months}
        for c in self.originals:
            b = by_load[month_key(c.load_month)]
            b[c.part] = round(b[c.part] + c.amount, 2)
        for kind, original, action in self.actions:
            b = by_load[month_key(action.load_month)]
            delta = -original.amount if kind == ADJ_CANCEL else action.amount - original.amount
            b[original.part] = round(b[original.part] + delta, 2)
        write_csv(
            key_dir / "net_paid_by_load_month.csv",
            ["load_month", "part_a_net_change", "part_b_net_change", "total_net_change"],
            [
                [m, fmt_amt(v["A"]), fmt_amt(v["B"]), fmt_amt(v["A"] + v["B"])]
                for m, v in by_load.items()
            ],
        )

        write_csv(
            key_dir / "claim_actions.csv",
            ["action_type", "part", "CLM_CNTL_NUM", "BENE_MBI_ID", "original_clm_id",
             "original_load_month", "original_amount", "action_clm_id", "action_load_month",
             "action_amount", "final_amount"],
            [
                ["CANCEL" if kind == ADJ_CANCEL else "ADJUST", o.part, o.cntl, o.mbi, o.clm_id,
                 month_key(o.load_month), fmt_amt(o.amount), a.clm_id, month_key(a.load_month),
                 fmt_amt(a.amount), fmt_amt(final_amount[(o.part, o.cntl)])]
                for kind, o, a in self.actions
            ],
        )  # fmt: skip

        write_csv(
            key_dir / "member_changes.csv",
            ["delivery_month", "BENE_MBI_ID", "change_type", "details"],
            [[c["delivery_month"], c["BENE_MBI_ID"], c["change_type"], c["details"]]
             for c in self.member_changes],
        )  # fmt: skip

        orig_a = [c for c in self.originals if c.part == "A"]
        orig_b = [c for c in self.originals if c.part == "B"]
        summary = {
            "beneficiaries": len(latest),
            "deaths": sum(1 for b in self.benes if b.death_revealed),
            "part_a_original_claims": len(orig_a),
            "part_b_original_claims": len(orig_b),
            "cancellations": sum(1 for k, _, _ in self.actions if k == ADJ_CANCEL),
            "adjustments": sum(1 for k, _, _ in self.actions if k == ADJ_ADJUSTMENT),
            "late_arriving_originals": sum(1 for c in self.originals if c.late),
            "part_a_total_net_paid": fmt_amt(sum(v["A"] for v in by_service.values())),
            "part_b_total_net_paid": fmt_amt(sum(v["B"] for v in by_service.values())),
            "net_paid_rule": (
                "Group claim versions by (part, CLM_CNTL_NUM). Cancelled family -> 0; "
                "adjusted family -> the adjustment's amount; otherwise the original's "
                "amount. Part B amount = sum of CLM_LINE_CVRD_PD_AMT over the claim's "
                "lines. Service month = month of the original CLM_FROM_DT."
            ),
        }
        (key_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        self.registry.write_crossover(key_dir)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=42, help="random seed (default 42)")
    parser.add_argument("--start", default="2023-01", help="first month YYYY-MM (initial load)")
    parser.add_argument("--end", default="2025-12", help="last month YYYY-MM")
    parser.add_argument("--output-dir", type=Path, default=HERE / "output")
    parser.add_argument("--answer-key-dir", type=Path, default=HERE / "answer_key")
    args = parser.parse_args(argv)

    gen = CclfGenerator(args.seed, parse_month(args.start), parse_month(args.end))
    gen.run()
    sizes = gen.write_files(args.output_dir)
    gen.write_answer_key(args.answer_key_dir)
    print(f"seed={args.seed} months={len(gen.months)} output={args.output_dir}")
    print_size_summary(sizes, ("CCLF8", "CCLF1", "CCLF4", "CCLF5"))


if __name__ == "__main__":
    main()
