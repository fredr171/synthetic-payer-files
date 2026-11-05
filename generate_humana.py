"""Generate synthetic, seeded Humana Medicare Advantage monthly files (HUM_ELIG, HUM_MEDCLM).

Second-payer companion to generate_cclf.py. Members come from the shared member registry
(member_registry.py): run both generators with the same --seed, --start and --end and the
FFS <-> MA switchers line up (same MBI, name, DOB, address; no overlapping coverage).

Field names are invented (modeled on MA payer extracts / the APCD Common Data Layout);
Humana's real layouts are proprietary. Everything is synthetic.

Usage:
    python generate_humana.py --seed 42 --start 2023-01 --end 2025-12

Writes:
    <output-dir>/YYYY-MM/HUM_{ELIG,MEDCLM}_YYYYMM.csv      (default output/humana/)
    <answer-key-dir>/*.csv, summary.json                    (default answer_key/humana/)
    <crossover-dir>/crossover_members.csv                   (default answer_key/)
"""

from __future__ import annotations

import argparse
import json
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from cclf_common import (
    KB,
    add_months,
    csv_line_bytes,
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
from humana_common import (
    AMOUNT_FIELDS,
    DX_COLUMNS,
    FORM_INSTITUTIONAL,
    FORM_PROFESSIONAL,
    FREQ_ORIGINAL,
    FREQ_REPLACEMENT,
    FREQ_VOID,
    HUM_ELIG_COLUMNS,
    HUM_FILE_COLUMNS,
    HUM_MEDCLM_COLUMNS,
    PLANS,
    STATUS_DENIED,
    STATUS_PAID,
    STATUS_REVERSED,
    hum_file_name,
)
from member_registry import FFS, MA, Address, Person, Provider, Registry, Span, moved_address

HERE = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Tunable parameters (sizes in bytes; ranges in humana_common.py)
# ---------------------------------------------------------------------------
ELIG_MONTHLY_TARGET = (4.3 * KB, 5.0 * KB)
CLM_MONTHLY_TARGET = (4.5 * KB, 5.4 * KB)
CLM_INITIAL_TARGET = (11.5 * KB, 13.5 * KB)
CLM_MONTHLY_FLOOR = 4.3 * KB
CLM_INITIAL_FLOOR = 10.6 * KB
CLM_HARD_CAP = 5.85 * KB
CLM_INITIAL_HARD_CAP = 14.6 * KB
MAX_FILL_REJECTS = 200
LATE_ARRIVING_SHARE = 0.10
REPLACE_SHARE = 0.05
VOID_SHARE = 0.03
DENY_SHARE = 0.03
ACTION_LOOKBACK_MONTHS = 6
PCP_RENDERS_SHARE = 0.60  # professional claims rendered by the member's own PCP
OON_PROFESSIONAL_SHARE = 0.04
OON_ER_SHARE = 0.10
# Capitated PCP arrangements: (TIN group index, product). Groups 0 and 1 are shared
# practices, group 3 the Humana-only primary care group (member_registry.PROFESSIONALS).
# Under the arrangement, about CAPITATED_SHARE of all PCP-rendered professional claims are
# capitated encounters (PAID 0); services carved out of the capitation pay fee-for-service.
CAPITATION = {(g, p) for g in (0, 1, 3) for p in ("HMO", "D-SNP")}
CAPITATED_SHARE = 0.30

# Claim kinds -> weight. PROF = CMS-1500; OP/ER/IP/SNF = UB-04.
CLAIM_KIND_WEIGHTS = {"PROF": 64, "OP": 12, "ER": 10, "IP": 9, "SNF": 5}
BILL_TYPE_PREFIX = {"OP": "13", "ER": "13", "IP": "11", "SNF": "21"}

# Member cost sharing by product (D-SNP: $0, Medicaid covers it).
COST_SHARE = {
    "HMO": {
        "pcp": 0,
        "spec": 35,
        "urgent": 40,
        "er": 90,
        "op_visit": 25,
        "ip_day": 295,
        "snf_day": 196,
        "rad_coins": 0.0,
        "ded_prob": 0.0,
    },
    "PPO": {
        "pcp": 10,
        "spec": 45,
        "urgent": 55,
        "er": 110,
        "op_visit": 40,
        "ip_day": 350,
        "snf_day": 200,
        "rad_coins": 0.20,
        "ded_prob": 0.30,
    },
    "D-SNP": {
        "pcp": 0,
        "spec": 0,
        "urgent": 0,
        "er": 0,
        "op_visit": 0,
        "ip_day": 0,
        "snf_day": 0,
        "rad_coins": 0.0,
        "ded_prob": 0.0,
    },
}
IP_COPAY_DAYS = 5
SNF_FREE_DAYS = 20
PCP_SPECIALTIES = ("08", "11", "50")

# Outpatient facility fees (approx. OPPS) and revenue codes.
OP_FEES = {
    "G0463": (130.00, "0510"),
    "99283": (240.00, "0450"),
    "99284": (430.00, "0450"),
    "99285": (650.00, "0450"),
    "80053": (10.50, "0300"),
    "85025": (7.80, "0300"),
    "36415": (3.00, "0300"),
    "83036": (9.70, "0300"),
    "71046": (62.00, "0320"),
    "93000": (18.00, "0730"),
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class Member:
    """Humana view of a registry person: current HUM_ELIG attribute values."""

    person: Person
    member_id: str
    first_month: date
    spans: list[Span]  # MA spans (registry truth)
    enroll_start: date
    enroll_end: date | None
    product: str
    pcp: Provider
    pcp_start: date
    dual: bool
    lis: bool
    address: Address
    hospice: str = "N"
    death_reveal_month: date | None = None
    death_revealed: bool = False
    termed: bool = False

    @property
    def frozen(self) -> bool:
        return self.person.switcher

    def in_ma(self, month: date) -> bool:
        return any(
            s.start <= month_end(month) and (s.end is None or s.end >= month) for s in self.spans
        )

    def to_row(self, as_of: date, change_type: str) -> list[str]:
        p, a = self.person, self.address
        contract, pbp, plan_name = PLANS[self.product]
        death = p.true_death if self.death_revealed else None
        values = {
            "MEMBER_ID": self.member_id,
            "MBI": p.mbi,
            "FIRST_NAME": p.first,
            "MIDDLE_INITIAL": p.middle,
            "LAST_NAME": p.last,
            "DOB": fmt_date(p.dob),
            "GENDER": "M" if p.sex == "1" else "F",
            "ADDRESS_LINE_1": a.line1,
            "CITY": a.city,
            "STATE": a.usps,
            "ZIP": a.zip5,
            "COUNTY_FIPS": a.state_fips + a.county_fips,
            "CONTRACT_ID": contract,
            "PBP_ID": pbp,
            "PLAN_NAME": plan_name,
            "PRODUCT_TYPE": self.product,
            "ENROLLMENT_START_DT": fmt_date(self.enroll_start),
            "ENROLLMENT_END_DT": fmt_date(self.enroll_end),
            "PCP_NPI": self.pcp.npi,
            "PCP_TIN": self.pcp.tin,
            "PCP_ATTRIBUTION_START_DT": fmt_date(self.pcp_start),
            "DUAL_FLAG": "Y" if self.dual else "N",
            "LIS_FLAG": "Y" if self.lis else "N",
            "ESRD_FLAG": "Y" if p.esrd else "N",
            "HOSPICE_FLAG": self.hospice,
            "DEATH_DT": fmt_date(death),
            "FILE_AS_OF_DT": fmt_date(month_end(as_of)),
            "RECORD_CHANGE_TYPE": change_type,
        }
        return [values[c] for c in HUM_ELIG_COLUMNS]


@dataclass
class HClaim:
    """One delivered claim version: original, reversal (void) or replacement."""

    claim_id: str
    form: str
    kind: str
    member: Member
    from_dt: date
    to_dt: date
    processed_month: date
    freq: str
    status: str
    header: dict[str, str]
    lines: list[dict]  # line fields; amounts as floats
    capitated: bool = False
    cap_eligible: bool = False  # rendered by the member's PCP under a capitated arrangement
    pcp_rendered: bool = False
    late: bool = False
    actioned: bool = False

    @property
    def paid(self) -> float:
        return round(sum(line["PAID_AMT"] for line in self.lines), 2)

    @property
    def allowed(self) -> float:
        return round(sum(line["ALLOWED_AMT"] for line in self.lines), 2)

    def rows(self) -> list[list[str]]:
        out = []
        for line in self.lines:
            values = dict(self.header)
            for k, v in line.items():
                values[k] = fmt_amt(v) if k in AMOUNT_FIELDS else v
            out.append([values[c] for c in HUM_MEDCLM_COLUMNS])
        return out


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------
class HumanaGenerator:
    """Emits monthly Humana MA deliveries for the MA side of the shared registry."""

    def __init__(self, seed: int, start: date, end: date) -> None:
        self.rng = random.Random(f"humana:{seed}")
        self.registry = Registry(seed, start, end)
        self.months = self.registry.months
        self.start, self.end = self.months[0], self.months[-1]
        self.members: list[Member] = []
        self.next_claim_seq = self.rng.randint(10_000_000, 20_000_000)
        self.originals: list[HClaim] = []
        self.actions: list[tuple[str, HClaim, HClaim, HClaim | None]] = []
        self.member_changes: list[dict] = []
        self.carry: dict[str, float] = defaultdict(float)
        self.n_late = 0
        self.files: dict[date, dict[str, list[list[str]]]] = {}
        pros = self.registry.providers_for(MA, "PROFESSIONAL")
        facs = self.registry.providers_for(MA, "FACILITY")
        self.pcps = [p for p in pros if p.in_network and p.specialty in PCP_SPECIALTIES]
        self.in_network_pros = [p for p in pros if p.in_network]
        self.oon_pros = [p for p in pros if not p.in_network]
        self.attendings = [p for p in self.in_network_pros if p.specialty in ("08", "11", "06")]
        self.hospitals = [f for f in facs if f.specialty == "HOSPITAL" and f.in_network]
        self.oon_hospitals = [f for f in facs if f.specialty == "HOSPITAL" and not f.in_network]
        self.snfs = [f for f in facs if f.specialty == "SNF"]
        self.ma_by_first_month: dict[date, list[Person]] = defaultdict(list)
        for p in self.registry.persons:
            first = self.registry.first_delivery(p, MA)
            if first is not None:
                self.ma_by_first_month[first].append(p)

    def _weighted(self, pairs: list[tuple[str, int]]) -> str:
        return weighted(self.rng, pairs)

    def _claim_id(self, processed: date) -> str:
        self.next_claim_seq += self.rng.randint(1, 30)
        return f"{processed.year % 100:02d}{self.next_claim_seq:010d}"

    def _quota(self, name: str, share: float) -> bool:
        """Deterministic share: True on roughly `share` of calls (carry-forward)."""
        self.carry[name] += share
        if self.carry[name] >= 1:
            self.carry[name] -= 1
            return True
        return False

    # -- members ------------------------------------------------------------
    def _enroll(self, person: Person, month: date) -> Member:
        r = self.rng
        span = person.spans_for(MA)[0]
        dual = person.dual != "NA"
        if dual:
            product = "D-SNP" if r.random() < 0.8 else "HMO"
        else:
            product = "HMO" if r.random() < 0.62 else "PPO"
        member = Member(
            person=person,
            member_id=person.member_id,
            first_month=month,
            spans=person.spans_for(MA),
            enroll_start=span.start,
            enroll_end=None,
            product=product,
            pcp=self._choose_pcp(),
            pcp_start=span.start,
            dual=dual,
            lis=dual or r.random() < 0.08,
            address=person.address_as_of(month),
        )
        if person.true_death:
            death_month = date(person.true_death.year, person.true_death.month, 1)
            member.death_reveal_month = min(add_months(death_month, r.choice([0, 1])), self.end)
        self.members.append(member)
        return member

    def _choose_pcp(self, exclude: Provider | None = None) -> Provider:
        pool = [p for p in self.pcps if p != exclude]
        return self.rng.choice(pool)

    def _elig_month(self, month: date, initial: bool) -> list[list[str]]:
        rows: list[list[str]] = []
        touched: set[str] = set()

        def emit(m: Member, change: str, record_type: str, detail: str) -> None:
            if m.frozen:
                m.address = m.person.address_as_of(month)
            rows.append(m.to_row(month, record_type))
            touched.add(m.member_id)
            self.member_changes.append(
                {
                    "delivery_month": month_key(month),
                    "MEMBER_ID": m.member_id,
                    "MBI": m.person.mbi,
                    "change_type": change,
                    "details": detail,
                }
            )

        for person in self.ma_by_first_month.get(month, []):
            reason = (
                "initial roster"
                if initial
                else ("switched from FFS" if person.spans[0].payer == FFS else "new enrollee")
            )
            emit(self._enroll(person, month), "NEW", "NEW", reason)
        if initial:
            return rows

        for m in self.members:  # registry disenrollments (to FFS or another plan)
            if m.termed or m.member_id in touched:
                continue
            for span in m.spans:
                if span.end is not None and span.end + timedelta(days=1) == month:
                    m.enroll_end, m.termed = span.end, True
                    to_ffs = m.person.span_overlapping(FFS, month, month) is not None
                    reason = "switched to FFS" if to_ffs else "disenrolled to another plan"
                    emit(m, "DISENROLL", "TERM", f"{reason}; coverage ends {span.end}")
        for m in self.members:
            if m.death_reveal_month == month and not m.death_revealed and not m.termed:
                m.death_revealed, m.termed = True, True
                death = m.person.true_death
                m.enroll_end = month_end(death)
                m.hospice = "Y" if self.rng.random() < 0.5 else "N"
                emit(m, "DEATH", "TERM", f"death date {death.isoformat()}")
        for m in self.members:  # registry-planned moves of switchers while in MA
            if m.member_id in touched or m.termed or not m.in_ma(month):
                continue
            if any(when == month for when, _ in m.person.moves):
                old_zip = m.address.zip5
                m.address = m.person.address_as_of(month)
                emit(m, "ADDRESS", "UPDATE", f"ZIP {old_zip} -> {m.address.zip5}")

        target = self.rng.uniform(*ELIG_MONTHLY_TARGET)
        size = csv_line_bytes(HUM_ELIG_COLUMNS) + sum(csv_line_bytes(r) for r in rows)
        candidates = [
            m
            for m in self.members
            if m.member_id not in touched
            and m.first_month < month
            and not m.termed
            and not m.frozen
            and m.in_ma(month)
            and not (m.person.true_death and m.person.true_death < add_months(month, 1))
        ]
        for _attempt in range(300):
            if not candidates:
                break
            m = candidates.pop(self.rng.randrange(len(candidates)))
            if size + csv_line_bytes(m.to_row(month, "UPDATE")) + 8 > target:
                break
            change, detail = self._apply_change(m, month)
            size += csv_line_bytes(m.to_row(month, "UPDATE"))
            emit(m, change, "UPDATE", detail)
        return rows

    def _apply_change(self, m: Member, month: date) -> tuple[str, str]:
        """Mutate one attribute of a (non-switcher) member; returns (type, detail)."""
        r = self.rng
        kind = self._weighted([("PCP", 45), ("ADDRESS", 35), ("DUAL_LIS", 20)])
        if kind == "DUAL_LIS" and m.product == "D-SNP":
            kind = "PCP"  # D-SNP members must stay dual-eligible
        if kind == "PCP":
            old = m.pcp
            m.pcp, m.pcp_start = self._choose_pcp(exclude=old), month
            return "PCP", f"PCP {old.npi} -> {m.pcp.npi}"
        if kind == "ADDRESS":
            old_zip = m.address.zip5
            m.address = moved_address(r, m.address)
            return "ADDRESS", f"ZIP {old_zip} -> {m.address.zip5}"
        before = f"dual={'Y' if m.dual else 'N'} lis={'Y' if m.lis else 'N'}"
        if not m.dual and r.random() < 0.5:
            m.dual, m.lis = True, True
        elif m.dual:
            m.dual, m.lis = False, r.random() < 0.5
        else:
            m.lis = not m.lis
        after = f"dual={'Y' if m.dual else 'N'} lis={'Y' if m.lis else 'N'}"
        return "DUAL_LIS", f"{before} -> {after}"

    # -- claim helpers ------------------------------------------------------
    def _processing_dates(self, month: date) -> tuple[date, date]:
        """(RECEIVED_DT, PROCESSED_DT); processed always inside the delivery month."""
        processed = month + timedelta(days=self.rng.randint(2, month_end(month).day - 1))
        return processed - timedelta(days=self.rng.randint(1, 14)), processed

    def _lag_months(self, late: bool) -> int:
        if late:
            return self.rng.randint(4, 12)
        return self.rng.choices([0, 1, 2, 3], weights=[22, 40, 25, 13])[0]

    def _service_window(
        self, m: Member, svc_month: date, latest_to: date, los: int
    ) -> tuple[date, date] | None:
        """Random (from, to) in svc_month inside one MA span, before death and receipt."""
        span = m.person.span_overlapping(MA, svc_month, month_end(svc_month))
        if span is None:
            return None
        earliest = max(svc_month, span.start)
        if span.end is not None:
            latest_to = min(latest_to, span.end)
        if m.person.true_death:
            latest_to = min(latest_to, m.person.true_death)
        latest_from = min(month_end(svc_month), latest_to - timedelta(days=los))
        if latest_from < earliest:
            return None
        from_dt = earliest + timedelta(days=self.rng.randint(0, (latest_from - earliest).days))
        return from_dt, from_dt + timedelta(days=los)

    def _line(self, num: int, from_dt: date, to_dt: date, **kw) -> dict:
        line = {
            "CLAIM_LINE_NUM": str(num),
            "LINE_FROM_DT": fmt_date(from_dt),
            "LINE_TO_DT": fmt_date(to_dt),
            "REVENUE_CD": "",
            "PROCEDURE_CD": "",
            "MODIFIER_1": "",
            "UNITS": "1",
            "BILLED_AMT": 0.0,
            "ALLOWED_AMT": 0.0,
            "PAID_AMT": 0.0,
            "COPAY_AMT": 0.0,
            "COINSURANCE_AMT": 0.0,
            "DEDUCTIBLE_AMT": 0.0,
        }
        line.update(kw)
        return line

    @staticmethod
    def _settle(line: dict, copay: float = 0.0, coins_rate: float = 0.0, ded: float = 0.0) -> None:
        """Apply member cost sharing (capped at allowed) and set PAID = ALLOWED - cost share."""
        allowed = line["ALLOWED_AMT"]
        copay = round(min(copay, allowed), 2)
        ded = round(min(ded, allowed - copay), 2)
        coins = round((allowed - copay - ded) * coins_rate, 2)
        line["COPAY_AMT"], line["DEDUCTIBLE_AMT"], line["COINSURANCE_AMT"] = copay, ded, coins
        line["PAID_AMT"] = round(allowed - copay - ded - coins, 2)

    def _apportion(self, lines: list[dict], total_allowed: float) -> None:
        """Split a claim-level allowed amount across lines in proportion to billed."""
        billed_total = sum(line["BILLED_AMT"] for line in lines)
        remaining = round(total_allowed, 2)
        for i, line in enumerate(lines):
            if i == len(lines) - 1:
                line["ALLOWED_AMT"] = round(remaining, 2)
            else:
                share = round(total_allowed * line["BILLED_AMT"] / billed_total, 2)
                line["ALLOWED_AMT"] = share
                remaining -= share

    def _header(
        self,
        m: Member,
        claim_id: str,
        form: str,
        from_dt: date,
        to_dt: date,
        received: date,
        processed: date,
        dx: list[str],
        **kw,
    ) -> dict[str, str]:
        contract, pbp, _ = PLANS[m.product]
        header = {c: "" for c in HUM_MEDCLM_COLUMNS}
        header.update(
            {
                "CLAIM_ID": claim_id,
                "CLAIM_STATUS": STATUS_PAID,
                "CLAIM_FREQ_CD": FREQ_ORIGINAL,
                "CLAIM_FORM_TYPE": form,
                "MEMBER_ID": m.member_id,
                "MBI": m.person.mbi,
                "CONTRACT_ID": contract,
                "PBP_ID": pbp,
                "SERVICE_FROM_DT": fmt_date(from_dt),
                "SERVICE_TO_DT": fmt_date(to_dt),
                "CAPITATED_FLAG": "N",
                "ICD_VERSION": "0",
                "RECEIVED_DT": fmt_date(received),
                "PROCESSED_DT": fmt_date(processed),
            }
        )
        for col, code in zip(DX_COLUMNS, dx[:12], strict=False):
            header[col] = code
        header.update(kw)
        return header

    # -- new originals ------------------------------------------------------
    def _new_claim(self, month: date) -> HClaim | None:
        r = self.rng
        kind = r.choices(list(CLAIM_KIND_WEIGHTS), weights=list(CLAIM_KIND_WEIGHTS.values()))[0]
        received, processed = self._processing_dates(month)
        members = [m for m in self.members if m.first_month <= month]
        weights = [m.person.weight for m in members]
        # Steer the late share of DELIVERED originals to the target (rejected claims
        # are never counted), with a little randomness so late claims are not periodic.
        late = self.n_late < LATE_ARRIVING_SHARE * (len(self.originals) + 1) * r.uniform(0.9, 1.1)
        drg = None
        for _ in range(60):
            m = r.choices(members, weights=weights)[0]
            if kind == "IP":
                drg = r.choices(DRGS, weights=[d[6] for d in DRGS])[0]
                los = r.randint(*drg[3])
            elif kind == "SNF":
                los = r.randint(8, 35)
            else:
                los = 0
            svc_month = add_months(month, -self._lag_months(late))
            window = self._service_window(m, svc_month, received - timedelta(days=1), los)
            if window:
                break
        else:
            return None
        from_dt, to_dt = window
        claim_id = self._claim_id(processed)
        build = {"PROF": self._professional, "IP": self._inpatient, "SNF": self._snf}.get(
            kind, self._outpatient
        )
        claim = build(m, claim_id, kind, from_dt, to_dt, received, processed, drg)
        claim.processed_month, claim.late = month, late
        if any(line["PAID_AMT"] for line in claim.lines):
            paid_dt = processed + timedelta(days=r.randint(1, 7))
            claim.header["PAID_DT"] = fmt_date(paid_dt)
        return claim

    @staticmethod
    def _capitate(claim: HClaim) -> None:
        claim.capitated = True
        claim.header["CAPITATED_FLAG"] = "Y"
        claim.header["PAID_DT"] = ""
        for line in claim.lines:
            line["PAID_AMT"] = 0.0

    def _deny(self, claim: HClaim) -> None:
        claim.status = STATUS_DENIED
        claim.header["CLAIM_STATUS"] = STATUS_DENIED
        claim.header["PAID_DT"] = ""
        for line in claim.lines:
            for k in AMOUNT_FIELDS:
                if k != "BILLED_AMT":
                    line[k] = 0.0

    def _professional(self, m, claim_id, kind, from_dt, to_dt, received, processed, _drg):
        r = self.rng
        cs = COST_SHARE[m.product]
        if r.random() < PCP_RENDERS_SHARE:
            pro = m.pcp
        elif r.random() < OON_PROFESSIONAL_SHARE / (1 - PCP_RENDERS_SHARE):
            pro = r.choice(self.oon_pros)
        else:
            pro = r.choice(self.in_network_pros)
        codes = visit_lines(r, pro.specialty)
        if codes[0][0] == "G0439":
            dx = [Z_ANNUAL_WELLNESS] + pick_dx(r, m.person.conditions, m.person.conditions[0], 2)
        else:
            dx = pick_dx(r, m.person.conditions, r.choice(m.person.conditions), r.choice([1, 2, 3]))
        pos = "20" if not pro.in_network else "11"
        factor = YEAR_FEE_FACTOR.get(from_dt.year, 1.0)
        lines = []
        for n, (hcpcs, mdfr) in enumerate(codes, start=1):
            allowed = round(HCPCS_FEES[hcpcs][0] * factor * r.uniform(1.00, 1.05), 2)
            line = self._line(
                n,
                from_dt,
                to_dt,
                PROCEDURE_CD=hcpcs,
                MODIFIER_1=mdfr,
                ALLOWED_AMT=allowed,
                BILLED_AMT=round(allowed * r.uniform(1.6, 2.8), 2),
            )
            copay = coins = 0.0
            if hcpcs.startswith("99"):
                if not pro.in_network:
                    copay = cs["urgent"]
                else:
                    copay = cs["pcp"] if pro.specialty in PCP_SPECIALTIES else cs["spec"]
            elif hcpcs in ("71046", "20610"):
                coins = cs["rad_coins"]
            self._settle(line, copay=copay, coins_rate=coins)
            lines.append(line)
        header = self._header(
            m,
            claim_id,
            FORM_PROFESSIONAL,
            from_dt,
            to_dt,
            received,
            processed,
            dx,
            PLACE_OF_SERVICE=pos,
            BILLING_NPI=pro.npi,
            BILLING_TIN=pro.tin,
            RENDERING_NPI=pro.npi,
            PROVIDER_SPECIALTY=pro.specialty,
            IN_NETWORK_FLAG="Y" if pro.in_network else "N",
        )
        return HClaim(
            claim_id,
            FORM_PROFESSIONAL,
            kind,
            m,
            from_dt,
            to_dt,
            processed,
            FREQ_ORIGINAL,
            STATUS_PAID,
            header,
            lines,
            cap_eligible=pro == m.pcp and (pro.group, m.product) in CAPITATION,
            pcp_rendered=pro == m.pcp,
        )

    def _facility_header(
        self, m, claim_id, kind, fac, from_dt, to_dt, received, processed, dx, **kw
    ):
        return self._header(
            m,
            claim_id,
            FORM_INSTITUTIONAL,
            from_dt,
            to_dt,
            received,
            processed,
            dx,
            BILL_TYPE=BILL_TYPE_PREFIX[kind] + FREQ_ORIGINAL,
            BILLING_NPI=fac.npi,
            BILLING_TIN=fac.tin,
            ATTENDING_NPI=self.rng.choice(self.attendings).npi,
            FACILITY_CCN=fac.ccn,
            IN_NETWORK_FLAG="Y" if fac.in_network else "N",
            **kw,
        )

    def _outpatient(self, m, claim_id, kind, from_dt, to_dt, received, processed, _drg):
        r = self.rng
        cs = COST_SHARE[m.product]
        if kind == "ER":
            fac = r.choice(self.oon_hospitals if r.random() < OON_ER_SHARE else self.hospitals)
            codes = [self._weighted([("99283", 35), ("99284", 45), ("99285", 20)])]
            codes += ["80053"] if r.random() < 0.5 else []
            codes += ["71046"] if r.random() < 0.4 else []
            codes += ["93000"] if r.random() < 0.2 else []
            principal = (
                self._weighted(ACUTE_OP_DX) if r.random() < 0.7 else r.choice(m.person.conditions)
            )
        else:
            fac = r.choice(self.hospitals)
            codes = ["G0463"]
            codes += ["36415", "80053"] if r.random() < 0.4 else []
            codes += ["71046"] if r.random() < 0.15 else []
            codes += ["93000"] if r.random() < 0.1 and len(codes) < 4 else []
            principal = r.choice(m.person.conditions)
        dx = pick_dx(r, m.person.conditions, principal, r.choice([1, 2, 3, 4]))
        factor = YEAR_FEE_FACTOR.get(from_dt.year, 1.0)
        lines = []
        for n, code in enumerate(codes[:4], start=1):
            fee, rev = OP_FEES[code]
            allowed = round(fee * factor * r.uniform(1.0, 1.05), 2)
            line = self._line(
                n,
                from_dt,
                to_dt,
                REVENUE_CD=rev,
                PROCEDURE_CD=code,
                ALLOWED_AMT=allowed,
                BILLED_AMT=round(allowed * r.uniform(2.5, 4.0), 2),
            )
            copay = coins = ded = 0.0
            if rev == "0450":
                copay = cs["er"]
            elif rev == "0510":
                copay = cs["op_visit"]
                if r.random() < cs["ded_prob"]:
                    ded = float(r.randint(50, 150))
            elif rev == "0320":
                coins = cs["rad_coins"]
            self._settle(line, copay=copay, coins_rate=coins, ded=ded)
            lines.append(line)
        header = self._facility_header(
            m, claim_id, kind, fac, from_dt, to_dt, received, processed, dx, DISCHARGE_STATUS="01"
        )
        return HClaim(
            claim_id,
            FORM_INSTITUTIONAL,
            kind,
            m,
            from_dt,
            to_dt,
            processed,
            FREQ_ORIGINAL,
            STATUS_PAID,
            header,
            lines,
        )

    def _inpatient(self, m, claim_id, kind, from_dt, to_dt, received, processed, drg):
        r = self.rng
        cs = COST_SHARE[m.product]
        drg_cd, principal, admit_dx, _los, pay_rng, admit_type, _w = drg
        los = max((to_dt - from_dt).days, 1)
        fac = r.choice(self.hospitals)
        lines = [
            self._line(
                1,
                from_dt,
                to_dt,
                REVENUE_CD="0120",
                UNITS=str(los),
                BILLED_AMT=round(los * r.uniform(2200, 3200), 2),
            )
        ]
        extra = [("0250", (800, 4000)), ("0300", (300, 1500))]
        if admit_type == "1" and r.random() < 0.6:
            extra.append(("0450", (1500, 3500)))
        if drg_cd in ("470", "481"):
            extra.append(("0360", (8000, 15000)))
        for rev, (lo, hi) in extra[:3]:
            lines.append(
                self._line(
                    len(lines) + 1,
                    from_dt,
                    to_dt,
                    REVENUE_CD=rev,
                    BILLED_AMT=round(r.uniform(lo, hi), 2),
                )
            )
        allowed = round(r.uniform(*pay_rng) * r.uniform(1.0, 1.06), 2)
        billed = sum(line["BILLED_AMT"] for line in lines)
        if billed < allowed * 2.2:  # chargemaster always well above the payment
            scale = allowed * r.uniform(2.2, 3.0) / billed
            for line in lines:
                line["BILLED_AMT"] = round(line["BILLED_AMT"] * scale, 2)
        self._apportion(lines, allowed)
        for i, line in enumerate(lines):
            self._settle(line, copay=cs["ip_day"] * min(los, IP_COPAY_DAYS) if i == 0 else 0.0)
        dx = pick_dx(r, m.person.conditions, principal, r.randint(3, 8))
        header = self._facility_header(
            m,
            claim_id,
            kind,
            fac,
            from_dt,
            to_dt,
            received,
            processed,
            dx,
            ADMIT_DT=fmt_date(from_dt),
            DISCHARGE_DT=fmt_date(to_dt),
            ADMIT_TYPE=admit_type,
            ADMIT_SOURCE="1" if admit_type == "3" else r.choice("12"),
            DISCHARGE_STATUS=self._weighted([("01", 60), ("06", 20), ("03", 15), ("62", 5)]),
            DRG_CD=drg_cd,
        )
        if admit_dx not in dx:
            header[DX_COLUMNS[min(len(dx), 11)]] = admit_dx
        return HClaim(
            claim_id,
            FORM_INSTITUTIONAL,
            kind,
            m,
            from_dt,
            to_dt,
            processed,
            FREQ_ORIGINAL,
            STATUS_PAID,
            header,
            lines,
        )

    def _snf(self, m, claim_id, kind, from_dt, to_dt, received, processed, _drg):
        r = self.rng
        cs = COST_SHARE[m.product]
        los = max((to_dt - from_dt).days, 1)
        fac = r.choice(self.snfs)
        lines = [
            self._line(
                1,
                from_dt,
                to_dt,
                REVENUE_CD="0120",
                UNITS=str(los),
                BILLED_AMT=round(los * r.uniform(700, 900), 2),
            ),
            self._line(
                2,
                from_dt,
                to_dt,
                REVENUE_CD="0420",
                PROCEDURE_CD="97110",
                UNITS=str(r.randint(los // 2, los)),
                BILLED_AMT=round(r.uniform(900, 2500), 2),
            ),
            self._line(
                3,
                from_dt,
                to_dt,
                REVENUE_CD="0430",
                PROCEDURE_CD="97530",
                UNITS=str(r.randint(los // 3, los)),
                BILLED_AMT=round(r.uniform(600, 1800), 2),
            ),
        ]
        allowed = round(los * r.uniform(480, 600), 2)
        billed = sum(line["BILLED_AMT"] for line in lines)
        if billed < allowed * 1.5:
            scale = allowed * r.uniform(1.5, 2.0) / billed
            for line in lines:
                line["BILLED_AMT"] = round(line["BILLED_AMT"] * scale, 2)
        self._apportion(lines, allowed)
        for i, line in enumerate(lines):
            copay = cs["snf_day"] * max(0, los - SNF_FREE_DAYS) if i == 0 else 0.0
            self._settle(line, copay=copay)
        principal = self._weighted([("M1711", 2), ("I5022", 2), ("J189", 1), ("S72001A", 2)])
        dx = pick_dx(r, m.person.conditions, principal, r.randint(3, 6))
        header = self._facility_header(
            m,
            claim_id,
            kind,
            fac,
            from_dt,
            to_dt,
            received,
            processed,
            dx,
            ADMIT_DT=fmt_date(from_dt),
            DISCHARGE_DT=fmt_date(to_dt),
            ADMIT_TYPE="3",
            ADMIT_SOURCE="4",
            DISCHARGE_STATUS=self._weighted([("01", 70), ("06", 30)]),
        )
        return HClaim(
            claim_id,
            FORM_INSTITUTIONAL,
            kind,
            m,
            from_dt,
            to_dt,
            processed,
            FREQ_ORIGINAL,
            STATUS_PAID,
            header,
            lines,
        )

    # -- reversals and replacements -----------------------------------------
    def _version(
        self,
        original: HClaim,
        month: date,
        claim_id: str,
        status: str,
        freq: str,
        lines: list[dict],
    ) -> HClaim:
        received, processed = self._processing_dates(month)
        orig_processed = date.fromisoformat(original.header["PROCESSED_DT"])
        received = max(received, orig_processed + timedelta(days=1))
        header = dict(original.header)
        header.update(
            {
                "CLAIM_ID": claim_id,
                "ORIG_CLAIM_ID": original.claim_id,
                "CLAIM_STATUS": status,
                "CLAIM_FREQ_CD": freq,
                "RECEIVED_DT": fmt_date(received),
                "PROCESSED_DT": fmt_date(processed),
                "PAID_DT": "",
            }
        )
        if original.form == FORM_INSTITUTIONAL:
            header["BILL_TYPE"] = original.header["BILL_TYPE"][:2] + freq
        version = HClaim(
            claim_id,
            original.form,
            original.kind,
            original.member,
            original.from_dt,
            original.to_dt,
            month,
            freq,
            status,
            header,
            lines,
        )
        if any(line["PAID_AMT"] for line in lines):
            header["PAID_DT"] = fmt_date(processed + timedelta(days=self.rng.randint(1, 7)))
        return version

    def _reversal(self, original: HClaim, month: date) -> HClaim:
        lines = []
        for line in original.lines:
            new = dict(line)
            for k in AMOUNT_FIELDS:
                new[k] = -line[k]
            lines.append(new)
        return self._version(original, month, original.claim_id, STATUS_REVERSED, FREQ_VOID, lines)

    def _replacement(self, original: HClaim, month: date) -> HClaim:
        r = self.rng
        while True:
            f = r.choice([r.uniform(0.80, 0.95), r.uniform(1.05, 1.15)])
            lines = []
            for line in original.lines:
                new = dict(line)
                new["ALLOWED_AMT"] = round(line["ALLOWED_AMT"] * f, 2)
                rate = line["COINSURANCE_AMT"] / max(
                    line["ALLOWED_AMT"] - line["COPAY_AMT"] - line["DEDUCTIBLE_AMT"], 0.01
                )
                self._settle(
                    new, copay=line["COPAY_AMT"], coins_rate=rate, ded=line["DEDUCTIBLE_AMT"]
                )
                lines.append(new)
            if abs(sum(x["PAID_AMT"] for x in lines) - original.paid) >= 0.01:
                break
        return self._version(
            original, month, self._claim_id(month), STATUS_PAID, FREQ_REPLACEMENT, lines
        )

    def _actions_for_month(self, month: date) -> list[HClaim]:
        """Voids (reversal only) and replacements (reversal + new claim) of earlier originals."""
        prev = add_months(month, -1)
        volume = sum(1 for c in self.originals if c.processed_month == prev)
        pool = [
            c
            for c in self.originals
            if not c.actioned
            and not c.capitated
            and c.status == STATUS_PAID
            and c.paid > 0
            and add_months(month, -ACTION_LOOKBACK_MONTHS) <= c.processed_month < month
        ]
        out: list[HClaim] = []
        for kind, share in (("REPLACE", REPLACE_SHARE), ("VOID", VOID_SHARE)):
            self.carry[kind] += share * volume * self.rng.uniform(0.8, 1.2)
            n = int(self.carry[kind])
            self.carry[kind] -= n
            for _ in range(n):
                if not pool:
                    break
                original = pool.pop(self.rng.randrange(len(pool)))
                original.actioned = True
                reversal = self._reversal(original, month)
                replacement = self._replacement(original, month) if kind == "REPLACE" else None
                self.actions.append((kind, original, reversal, replacement))
                out.append(reversal)
                if replacement:
                    out.append(replacement)
        return out

    def _claims_month(self, month: date, initial: bool) -> list[list[str]]:
        target = self.rng.uniform(*(CLM_INITIAL_TARGET if initial else CLM_MONTHLY_TARGET))
        floor = CLM_INITIAL_FLOOR if initial else CLM_MONTHLY_FLOOR
        cap = CLM_INITIAL_HARD_CAP if initial else CLM_HARD_CAP
        rows: list[list[str]] = []
        size = csv_line_bytes(HUM_MEDCLM_COLUMNS)
        stream = [] if initial else self._actions_for_month(month)
        rejects = 0
        while rejects < MAX_FILL_REJECTS:
            claim = stream.pop(0) if stream else self._new_claim(month)
            if claim is None:
                break
            new_rows = claim.rows()
            add = sum(csv_line_bytes(r) for r in new_rows)
            # Over target: stop once at the floor; below it, accept up to the hard cap
            # (multi-line inpatient claims are large), otherwise draw another claim.
            if claim.freq == FREQ_ORIGINAL and size + add > target:
                if size >= floor:
                    break
                if size + add > cap:
                    rejects += 1
                    continue
            # Capitation and denials are decided only for claims that make it into the
            # file, so the quotas track delivered claims (those rows are never longer).
            if claim.freq == FREQ_ORIGINAL:
                if claim.pcp_rendered:
                    self.carry["cap"] += CAPITATED_SHARE
                    if self.carry["cap"] >= 1 and claim.cap_eligible:
                        self.carry["cap"] -= 1
                        self._capitate(claim)
                        new_rows = claim.rows()
                self.carry["deny"] += DENY_SHARE
                if self.carry["deny"] >= 1 and not claim.capitated:
                    self.carry["deny"] -= 1
                    self._deny(claim)
                    new_rows = claim.rows()
                self.n_late += claim.late
            size += add
            rows.extend(new_rows)
            if claim.freq == FREQ_ORIGINAL:
                self.originals.append(claim)
        return rows

    # -- driver -------------------------------------------------------------
    def run(self) -> None:
        for i, month in enumerate(self.months):
            initial = i == 0
            self.files[month] = {
                "HUM_ELIG": self._elig_month(month, initial),
                "HUM_MEDCLM": self._claims_month(month, initial),
            }

    def write_files(self, output_dir: Path) -> dict[tuple[str, str], int]:
        remove_stale_csvs(output_dir, pattern="*/HUM_*.csv")
        sizes = {}
        for month, files in self.files.items():
            folder = output_dir / month_key(month)
            folder.mkdir(parents=True, exist_ok=True)
            for file_type in ("HUM_ELIG", "HUM_MEDCLM"):
                path = folder / hum_file_name(file_type, month)
                write_csv(path, HUM_FILE_COLUMNS[file_type], files[file_type])
                sizes[(month_key(month), file_type)] = path.stat().st_size
        return sizes

    def write_answer_key(self, key_dir: Path, crossover_dir: Path) -> None:
        """Expected ETL results derived from the simulation (not by re-reading the CSVs)."""
        remove_stale_csvs(key_dir, pattern="*.csv")
        key_dir.mkdir(parents=True, exist_ok=True)

        latest: dict[str, tuple[list[str], str, str]] = {}
        for month, files in self.files.items():
            for row in files["HUM_ELIG"]:
                first = latest[row[0]][1] if row[0] in latest else month_key(month)
                latest[row[0]] = (row, first, month_key(month))
        write_csv(
            key_dir / "final_members.csv",
            HUM_ELIG_COLUMNS[:-1] + ["FIRST_DELIVERY_MONTH", "LAST_DELIVERY_MONTH"],
            [row[:-1] + [first, last] for _, (row, first, last) in sorted(latest.items())],
        )

        # Final paid per claim family (keyed by the original CLAIM_ID).
        final = {c.claim_id: (0.0 if c.status == STATUS_DENIED else c.paid) for c in self.originals}
        for kind, original, _rev, repl in self.actions:
            final[original.claim_id] = repl.paid if kind == "REPLACE" else 0.0
        by_service: dict[str, dict[str, float]] = defaultdict(lambda: {"I": 0.0, "P": 0.0})
        for c in self.originals:
            b = by_service[month_key(c.from_dt)]
            b[c.form] = round(b[c.form] + final[c.claim_id], 2)
        write_csv(
            key_dir / "net_paid_by_service_month.csv",
            ["service_month", "institutional_net_paid", "professional_net_paid", "total_net_paid"],
            [
                [m, fmt_amt(v["I"]), fmt_amt(v["P"]), fmt_amt(v["I"] + v["P"])]
                for m, v in sorted(by_service.items())
            ],
        )

        by_processed = {month_key(m): {"I": 0.0, "P": 0.0} for m in self.months}
        versions = list(self.originals)
        for _k, _o, rev, repl in self.actions:
            versions += [rev] + ([repl] if repl else [])
        for v in versions:
            b = by_processed[month_key(v.processed_month)]
            b[v.form] = round(b[v.form] + v.paid, 2)
        write_csv(
            key_dir / "net_paid_by_processed_month.csv",
            ["processed_month", "institutional_paid", "professional_paid", "total_paid"],
            [
                [m, fmt_amt(v["I"]), fmt_amt(v["P"]), fmt_amt(v["I"] + v["P"])]
                for m, v in by_processed.items()
            ],
        )

        cap: dict[str, list[float]] = defaultdict(lambda: [0, 0, 0.0])
        for c in self.originals:
            if c.capitated:
                b = cap[month_key(c.from_dt)]
                b[0] += 1
                b[1] += len(c.lines)
                b[2] = round(b[2] + c.allowed, 2)
        write_csv(
            key_dir / "capitated_encounters_by_month.csv",
            ["service_month", "capitated_claims", "capitated_lines", "capitated_allowed_amt"],
            [[m, str(v[0]), str(v[1]), fmt_amt(v[2])] for m, v in sorted(cap.items())],
        )

        write_csv(
            key_dir / "claim_actions.csv",
            [
                "action_type",
                "original_claim_id",
                "MEMBER_ID",
                "CLAIM_FORM_TYPE",
                "original_processed_month",
                "original_paid",
                "action_processed_month",
                "replacement_claim_id",
                "replacement_paid",
                "final_paid",
            ],
            [
                [
                    kind,
                    o.claim_id,
                    o.member.member_id,
                    o.form,
                    month_key(o.processed_month),
                    fmt_amt(o.paid),
                    month_key(rev.processed_month),
                    repl.claim_id if repl else "",
                    fmt_amt(repl.paid) if repl else "",
                    fmt_amt(final[o.claim_id]),
                ]
                for kind, o, rev, repl in self.actions
            ],
        )
        write_csv(
            key_dir / "member_changes.csv",
            ["delivery_month", "MEMBER_ID", "MBI", "change_type", "details"],
            [
                [c["delivery_month"], c["MEMBER_ID"], c["MBI"], c["change_type"], c["details"]]
                for c in self.member_changes
            ],
        )

        prof = [c for c in self.originals if c.form == FORM_PROFESSIONAL]
        pcp_rendered = [c for c in prof if c.pcp_rendered]
        summary = {
            "members": len(latest),
            "deaths": sum(1 for m in self.members if m.death_revealed),
            "original_claims": len(self.originals),
            "institutional_original_claims": len(self.originals) - len(prof),
            "professional_original_claims": len(prof),
            "replacements": sum(1 for k, *_ in self.actions if k == "REPLACE"),
            "voids": sum(1 for k, *_ in self.actions if k == "VOID"),
            "denied": sum(1 for c in self.originals if c.status == STATUS_DENIED),
            "capitated_claims": sum(1 for c in self.originals if c.capitated),
            "late_arriving_originals": sum(1 for c in self.originals if c.late),
            "pcp_rendered_professional_claims": len(pcp_rendered),
            "institutional_total_net_paid": fmt_amt(sum(v["I"] for v in by_service.values())),
            "professional_total_net_paid": fmt_amt(sum(v["P"] for v in by_service.values())),
            "net_paid_rule": (
                "Claim family = original CLAIM_ID. A reversal (status R, freq 8) repeats the "
                "original CLAIM_ID with negated amounts; a replacement (freq 7) has a new "
                "CLAIM_ID and ORIG_CLAIM_ID = the original. Voided -> 0; replaced -> the "
                "replacement's PAID_AMT; denied -> 0; otherwise the original's PAID_AMT. "
                "Equivalently SUM(PAID_AMT) over every row. Service month = month of the "
                "original SERVICE_FROM_DT."
            ),
        }
        (key_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        self.registry.write_crossover(crossover_dir)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=42, help="random seed (default 42)")
    parser.add_argument("--start", default="2023-01", help="first month YYYY-MM (initial load)")
    parser.add_argument("--end", default="2025-12", help="last month YYYY-MM")
    parser.add_argument("--output-dir", type=Path, default=HERE / "output" / "humana")
    parser.add_argument("--answer-key-dir", type=Path, default=HERE / "answer_key" / "humana")
    parser.add_argument("--crossover-dir", type=Path, default=HERE / "answer_key")
    args = parser.parse_args(argv)

    gen = HumanaGenerator(args.seed, parse_month(args.start), parse_month(args.end))
    gen.run()
    sizes = gen.write_files(args.output_dir)
    gen.write_answer_key(args.answer_key_dir, args.crossover_dir)
    print(f"seed={args.seed} months={len(gen.months)} output={args.output_dir}")
    print_size_summary(sizes, ("HUM_ELIG", "HUM_MEDCLM"))


if __name__ == "__main__":
    main()
