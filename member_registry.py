"""Shared synthetic member registry for the CCLF (Medicare FFS) and Humana (MA) generators.

Both generators build the same Registry from (seed, start, end), so every person has one
MBI, name, DOB, address history and coverage timeline across payers. The registry decides
WHO is covered by WHICH payer WHEN (coverage spans, switches, deaths); each generator
decides what its own files say about it (claims, payer-specific changes, delivery lags).

Coverage rule: a person's FFS and MA spans never overlap. Switchers move between FFS and
Humana MA on the 1st of a month (Jan 1 = Annual Enrollment Period; Feb-Apr = MA Open
Enrollment Period / special enrollment). Switchers have frozen dual status and addresses
apart from registry-planned moves, so both payers report identical demographics.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from cclf_common import (
    MBI_ALPHA,
    add_months,
    fmt_date,
    luhn_check_digit,
    month_end,
    month_range,
    write_csv,
)
from clinical_reference import CHRONIC_DX, ESRD_DX, weighted

FFS, MA = "FFS", "MA"

# ---------------------------------------------------------------------------
# Population volumes
# ---------------------------------------------------------------------------
FFS_INITIAL_POPULATION = 118
FFS_NEW_PER_MONTH = (7, 10)
MA_INITIAL_POPULATION = 88
MA_NEW_PER_MONTH = (2, 3)
MA_NEW_IN_JANUARY = (4, 7)  # AEP enrollments from other plans
MA_DISENROLL_PROBABILITY = 0.06  # leave Humana for another MA plan (effective Jan 1)
DEATH_PROBABILITY = 0.03
NO_PART_B_SHARE = 0.06  # FFS only: MA requires Parts A and B
DUAL_SHARE = 0.15
UNDER_65_SHARE = 0.10
# Switch plan: AEP switches per year (first, second, ...), mid-year, round trips, MA->FFS.
AEP_SWITCHES_PER_YEAR = [7, 6]
MID_YEAR_SWITCHES = 3
ROUND_TRIPS = 2  # FFS -> MA at the first AEP, back to FFS at the next
MA_TO_FFS_ONE_WAY = 1
SWITCHER_MOVE_SHARE = 0.35

# ---------------------------------------------------------------------------
# Demographic pools (synthetic)
# ---------------------------------------------------------------------------
FIRST_NAMES_F = [
    "MARY", "PATRICIA", "LINDA", "BARBARA", "ELIZABETH", "JENNIFER", "SUSAN", "MARGARET",
    "DOROTHY", "NANCY", "KAREN", "BETTY", "HELEN", "SANDRA", "DONNA", "CAROL", "RUTH",
    "SHARON", "JOYCE", "JUDITH", "JANET", "GLORIA", "JEAN", "ALICE", "DIANE", "PEGGY",
]  # fmt: skip
FIRST_NAMES_M = [
    "JAMES", "ROBERT", "JOHN", "MICHAEL", "WILLIAM", "DAVID", "RICHARD", "CHARLES",
    "JOSEPH", "THOMAS", "DONALD", "GEORGE", "KENNETH", "STEVEN", "EDWARD", "RONALD",
    "GARY", "LARRY", "FRANK", "RAYMOND", "GERALD", "WALTER", "HAROLD", "CARL", "ROGER",
]  # fmt: skip
LAST_NAMES = [
    "SMITH", "JOHNSON", "WILLIAMS", "BROWN", "JONES", "MILLER", "DAVIS", "WILSON",
    "ANDERSON", "TAYLOR", "THOMAS", "MOORE", "MARTIN", "JACKSON", "THOMPSON", "WHITE",
    "HARRIS", "CLARK", "LEWIS", "ROBINSON", "WALKER", "YOUNG", "ALLEN", "KING", "WRIGHT",
    "HILL", "SCOTT", "GREEN", "ADAMS", "BAKER", "NELSON", "CARTER", "MITCHELL", "ROBERTS",
    "TURNER", "PHILLIPS", "CAMPBELL", "PARKER", "EVANS", "EDWARDS", "COLLINS", "STEWART",
]  # fmt: skip
STREETS = [
    "MAPLE", "OAK", "ELM", "CEDAR", "PINE", "WALNUT", "CHESTNUT", "PARK", "LAKE", "HILL",
    "RIDGE", "MEADOW", "SPRING", "MILL", "CHURCH", "MAIN", "HIGH", "BROAD", "SUMMIT",
    "ORCHARD", "WILLOW", "BIRCH", "SYCAMORE", "HICKORY",
]  # fmt: skip
STREET_SUFFIXES = ["ST", "AVE", "RD", "DR", "LN", "CT", "BLVD"]

# (city, state FIPS, county FIPS, USPS state, [zip5...], weight)
PLACES = [
    ("COLUMBUS", "39", "049", "OH", ["43204", "43211", "43229"], 20),
    ("CLEVELAND", "39", "035", "OH", ["44102", "44105", "44111"], 18),
    ("CINCINNATI", "39", "061", "OH", ["45205", "45211", "45238"], 15),
    ("DAYTON", "39", "113", "OH", ["45406", "45410"], 10),
    ("AKRON", "39", "153", "OH", ["44305", "44312"], 9),
    ("TOLEDO", "39", "095", "OH", ["43605", "43614"], 9),
    ("YOUNGSTOWN", "39", "099", "OH", ["44511"], 5),
    ("ZANESVILLE", "39", "119", "OH", ["43701"], 4),
    ("NAPLES", "12", "021", "FL", ["34102"], 1),
    ("FORT MYERS", "12", "071", "FL", ["33901"], 1),
    ("MESA", "04", "013", "AZ", ["85201"], 1),
]
DUAL_CODES = [("02", 45), ("04", 15), ("08", 20), ("01", 12), ("03", 8)]

# ---------------------------------------------------------------------------
# Provider pool: shared practices bill both payers; some are Humana-only or out of network
# ---------------------------------------------------------------------------
# (CCN, role, payers, Humana in-network)
FACILITIES = [
    ("360412", "HOSPITAL", (FFS, MA), True),
    ("360588", "HOSPITAL", (FFS, MA), True),
    ("365731", "SNF", (FFS, MA), True),
    ("367402", "HHA", (FFS,), True),
    ("361574", "HOSPICE", (FFS,), True),
    ("360733", "HOSPITAL", (MA,), False),  # out-of-network hospital (emergencies)
]
# (specialty, TIN index, payers, Humana in-network). TINs 0-2 are shared groups.
PROFESSIONALS = [
    ("08", 0, (FFS, MA), True), ("08", 0, (FFS, MA), True), ("08", 1, (FFS, MA), True),
    ("11", 0, (FFS, MA), True), ("11", 1, (FFS, MA), True), ("50", 0, (FFS, MA), True),
    ("06", 1, (FFS, MA), True), ("30", 2, (FFS, MA), True), ("20", 2, (FFS, MA), True),
    ("08", 3, (MA,), True), ("11", 3, (MA,), True),  # Humana-only primary care group
    ("06", 4, (MA,), True),  # Humana-only cardiology group
    ("08", 5, (MA,), False),  # out-of-network urgent care physician
]  # fmt: skip
N_TINS = 6


@dataclass(frozen=True)
class Address:
    line1: str
    city: str
    state_fips: str
    county_fips: str
    usps: str
    zip5: str
    zip4: str


@dataclass(frozen=True)
class Span:
    payer: str
    start: date
    end: date | None  # inclusive; None = open-ended

    def covers(self, d: date) -> bool:
        return self.start <= d and (self.end is None or d <= self.end)


@dataclass(frozen=True)
class Provider:
    npi: str
    kind: str  # "FACILITY" or "PROFESSIONAL"
    specialty: str  # CMS specialty code (professionals) or role (facilities)
    tin: str
    ccn: str  # facilities only
    payers: tuple[str, ...]
    in_network: bool  # Humana network status
    group: int  # TIN group index for professionals (see PROFESSIONALS); -1 for facilities


@dataclass(frozen=True)
class Switch:
    mbi: str
    member_id: str
    direction: str  # FFS_TO_MA or MA_TO_FFS
    effective: date
    period: str  # AEP or OEP_SEP


@dataclass
class Person:
    mbi: str
    hic: str
    dob: date
    sex: str  # 1 = male, 2 = female (CMS)
    race: str
    first: str
    middle: str
    last: str
    mdcr_stus: str
    orig_entl: str
    part_a_bgn: date
    part_b_bgn: date | None
    dual: str  # CMS dual status code or "NA"
    weight: float  # utilization weight (heavy-tailed)
    conditions: list[str]
    address: Address
    spans: list[Span] = field(default_factory=list)
    member_id: str | None = None  # Humana MEMBER_ID
    true_death: date | None = None
    switcher: bool = False
    moves: list[tuple[date, Address]] = field(default_factory=list)  # switchers only

    def address_as_of(self, month: date) -> Address:
        """Address after applying registry-planned moves effective by `month`."""
        addr = self.address
        for when, new in self.moves:
            if when <= month:
                addr = new
        return addr

    def spans_for(self, payer: str) -> list[Span]:
        return [s for s in self.spans if s.payer == payer]

    def span_overlapping(self, payer: str, lo: date, hi: date) -> Span | None:
        """The payer span that overlaps [lo, hi], if any."""
        for s in self.spans_for(payer):
            if s.start <= hi and (s.end is None or s.end >= lo):
                return s
        return None

    @property
    def esrd(self) -> bool:
        return self.mdcr_stus in ("11", "21", "31") or self.orig_entl in ("2", "3")


def random_address(rng: random.Random) -> Address:
    """A new synthetic street address in a weighted-random place."""
    place = rng.choices(PLACES, weights=[p[5] for p in PLACES])[0]
    return Address(
        line1=f"{rng.randint(100, 9899)} {rng.choice(STREETS)} {rng.choice(STREET_SUFFIXES)}",
        city=place[0],
        state_fips=place[1],
        county_fips=place[2],
        usps=place[3],
        zip5=rng.choice(place[4]),
        zip4=f"{rng.randint(0, 9999):04d}",
    )


def moved_address(rng: random.Random, current: Address) -> Address:
    """60%: move within the same city; otherwise anywhere in the place pool."""
    if rng.random() >= 0.6:
        return random_address(rng)
    place = next(p for p in PLACES if p[0] == current.city)
    return Address(
        line1=f"{rng.randint(100, 9899)} {rng.choice(STREETS)} {rng.choice(STREET_SUFFIXES)}",
        city=current.city,
        state_fips=current.state_fips,
        county_fips=current.county_fips,
        usps=current.usps,
        zip5=rng.choice(place[4]),
        zip4=f"{rng.randint(0, 9999):04d}",
    )


class Registry:
    """Deterministic person registry for one (seed, start, end)."""

    def __init__(self, seed: int, start: date, end: date) -> None:
        self.rng = random.Random(f"registry:{seed}")
        self.months = month_range(start, end)
        self.start, self.end = self.months[0], self.months[-1]
        self.persons: list[Person] = []
        self.switches: list[Switch] = []
        self._mbis: set[str] = set()
        self._hics: set[str] = set()
        self._member_ids: set[str] = set()
        self.providers = self._build_providers()
        self._build_ffs_population()
        self._build_ma_population()
        self._plan_switches()
        self._plan_deaths()
        self._plan_ma_disenrollments()
        self._plan_switcher_moves()

    # -- identifiers --------------------------------------------------------
    def _npi(self, used: set[str]) -> str:
        while True:
            base = self.rng.choice("12") + "".join(self.rng.choices("0123456789", k=8))
            npi = base + str(luhn_check_digit("80840" + base))
            if npi not in used:
                used.add(npi)
                return npi

    def _build_providers(self) -> list[Provider]:
        used: set[str] = set()
        # 99-prefix EINs: clearly synthetic. Facilities get their own TINs.
        tins = ["99" + "".join(self.rng.choices("0123456789", k=7)) for _ in range(N_TINS)]
        out = [
            Provider(self._npi(used), "FACILITY", role, "98" + "".join(
                self.rng.choices("0123456789", k=7)), ccn, payers, in_net, -1)
            for ccn, role, payers, in_net in FACILITIES
        ]  # fmt: skip
        out += [
            Provider(self._npi(used), "PROFESSIONAL", sp, tins[ti], "", payers, in_net, ti)
            for sp, ti, payers, in_net in PROFESSIONALS
        ]
        return out

    def providers_for(self, payer: str, kind: str) -> list[Provider]:
        return [p for p in self.providers if payer in p.payers and p.kind == kind]

    def _mbi(self) -> str:
        r, alnum = self.rng, MBI_ALPHA + "0123456789"
        while True:
            mbi = (
                r.choice("123456789") + r.choice(MBI_ALPHA) + r.choice(alnum)
                + r.choice("0123456789") + r.choice(MBI_ALPHA) + r.choice(alnum)
                + r.choice("0123456789") + r.choice(MBI_ALPHA) + r.choice(MBI_ALPHA)
                + r.choice("0123456789") + r.choice("0123456789")
            )  # fmt: skip
            if mbi not in self._mbis:
                self._mbis.add(mbi)
                return mbi

    def _hic(self) -> str:
        while True:  # 9xx area numbers are never issued as SSNs: clearly synthetic
            hic = "9" + "".join(self.rng.choices("0123456789", k=8)) + "A"
            if hic not in self._hics:
                self._hics.add(hic)
                return hic

    def _member_id(self) -> str:
        while True:
            mid = "H" + "".join(self.rng.choices("0123456789", k=9))
            if mid not in self._member_ids:
                self._member_ids.add(mid)
                return mid

    # -- people -------------------------------------------------------------
    def _new_person(self, ref: date, initial: bool, require_part_b: bool) -> Person:
        """A synthetic Medicare beneficiary, aged as of `ref` (first day of a month)."""
        r = self.rng
        under_65 = r.random() < UNDER_65_SHARE
        if under_65:
            age = r.randint(38, 64)
            esrd = r.random() < 0.2
            orig_entl, mdcr_stus = ("2", "31") if esrd else ("1", "20")
        else:
            if initial:
                age = 65 + int(r.triangular(0, 31, 2))
            else:
                age = r.choice([65, 65, 65, 66, 67, 70, 72, 75, 78, 82])
            age = min(age, 95)
            orig_entl = "0"
            mdcr_stus = "11" if r.random() < 0.03 else "10"
        dob = ref - timedelta(days=int(age * 365.25) + r.randint(1, 364))
        if under_65:
            part_a = add_months(ref, -r.randint(6, 120))
        else:
            part_a = date(dob.year + 65, dob.month, 1)
        part_b = part_a if require_part_b or r.random() >= NO_PART_B_SHARE else None
        sex = r.choice("12")
        n_cond = r.choices([1, 2, 3, 4, 5], weights=[20, 30, 25, 15, 10])[0]
        conditions: list[str] = []
        while len(conditions) < n_cond:
            dx = weighted(r, CHRONIC_DX)
            if dx not in conditions:
                conditions.append(dx)
        if orig_entl == "2" or mdcr_stus == "11":
            conditions = [ESRD_DX] + conditions[:3]
        person = Person(
            mbi=self._mbi(),
            hic=self._hic(),
            dob=dob,
            sex=sex,
            race=weighted(
                r, [("1", 72), ("2", 14), ("5", 6), ("4", 3), ("3", 3), ("6", 1), ("0", 1)]
            ),
            first=r.choice(FIRST_NAMES_M if sex == "1" else FIRST_NAMES_F),
            middle=r.choice("ABCDEFGHJKLMNPRSTW") if r.random() < 0.8 else "",
            last=r.choice(LAST_NAMES),
            mdcr_stus=mdcr_stus,
            orig_entl=orig_entl,
            part_a_bgn=part_a,
            part_b_bgn=part_b,
            dual=weighted(r, DUAL_CODES) if r.random() < DUAL_SHARE else "NA",
            weight=r.lognormvariate(0, 1.4),
            conditions=conditions,
            address=random_address(r),
        )
        self.persons.append(person)
        return person

    def _build_ffs_population(self) -> None:
        """FFS beneficiaries: the initial attributed population, then monthly attributions."""
        for _ in range(FFS_INITIAL_POPULATION):
            p = self._new_person(self.start, initial=True, require_part_b=False)
            p.spans.append(Span(FFS, date(2022, 1, 1), None))
        for month in self.months[1:]:
            for _ in range(self.rng.randint(*FFS_NEW_PER_MONTH)):
                p = self._new_person(month, initial=False, require_part_b=False)
                p.spans.append(Span(FFS, month, None))

    def _build_ma_population(self) -> None:
        """Humana MA members: initial roster (enrolled before the window), then new enrollees."""
        for _ in range(MA_INITIAL_POPULATION):
            p = self._new_person(self.start, initial=True, require_part_b=True)
            candidate = date(self.rng.randint(2016, self.start.year), 1, 1)
            start = max(candidate, date(p.part_b_bgn.year, p.part_b_bgn.month, 1))
            p.spans.append(Span(MA, min(start, self.start), None))
            p.member_id = self._member_id()
        for month in self.months[1:]:
            lo_hi = MA_NEW_IN_JANUARY if month.month == 1 else MA_NEW_PER_MONTH
            for _ in range(self.rng.randint(*lo_hi)):
                p = self._new_person(month, initial=False, require_part_b=True)
                p.spans.append(Span(MA, month, None))
                p.member_id = self._member_id()

    def _plan_switches(self) -> None:
        """Pick FFS<->MA switchers and rewrite their coverage spans."""
        r = self.rng
        years = list(range(self.start.year + 1, self.end.year + 1))
        if not years:
            return
        ffs_pool = [
            p for p in self.persons
            if p.spans[0].payer == FFS and p.spans[0].start < self.start and p.part_b_bgn
        ]  # fmt: skip
        plan: list[tuple[date, str]] = []
        for year, n in zip(years, AEP_SWITCHES_PER_YEAR, strict=False):
            plan += [(date(year, 1, 1), "AEP")] * n
        for _ in range(MID_YEAR_SWITCHES):
            d = date(r.choice(years), r.choice([2, 3, 4]), 1)
            if d <= self.end:
                plan.append((d, "OEP_SEP"))
        chosen = r.sample(ffs_pool, len(plan))
        first_aep = [i for i, (d, k) in enumerate(plan) if k == "AEP" and d.year == years[0]]
        round_trips = set(first_aep[:ROUND_TRIPS]) if len(years) > 1 else set()
        for i, (p, (eff, period)) in enumerate(zip(chosen, plan, strict=True)):
            p.switcher, p.member_id = True, self._member_id()
            p.spans = [Span(FFS, p.spans[0].start, eff - timedelta(days=1))]
            self.switches.append(Switch(p.mbi, p.member_id, "FFS_TO_MA", eff, period))
            if i in round_trips:
                back = date(eff.year + 1, 1, 1)
                p.spans += [Span(MA, eff, back - timedelta(days=1)), Span(FFS, back, None)]
                self.switches.append(Switch(p.mbi, p.member_id, "MA_TO_FFS", back, "AEP"))
            else:
                p.spans.append(Span(MA, eff, None))
        ma_pool = [
            p for p in self.persons if p.spans[0].payer == MA and p.spans[0].start < self.start
        ]
        for p in r.sample(ma_pool, MA_TO_FFS_ONE_WAY):
            eff = date(r.choice(years), 1, 1)
            p.switcher = True
            p.spans = [Span(MA, p.spans[0].start, eff - timedelta(days=1)), Span(FFS, eff, None)]
            self.switches.append(Switch(p.mbi, p.member_id, "MA_TO_FFS", eff, "AEP"))
        self.switches.sort(key=lambda s: (s.effective, s.mbi))

    def first_delivery(self, p: Person, payer: str) -> date | None:
        """Month the payer first delivers this person (window start for earlier spans)."""
        spans = p.spans_for(payer)
        if not spans:
            return None
        s = spans[0].start
        return max(date(s.year, s.month, 1), self.start)

    def _plan_deaths(self) -> None:
        """True death dates (non-switchers only), at least 2 months after first delivery."""
        latest_month = add_months(self.end, -1)
        for p in self.persons:
            if p.switcher or self.rng.random() >= DEATH_PROBABILITY:
                continue
            first = self.first_delivery(p, p.spans[0].payer)
            earliest = add_months(first, 2)
            if earliest > latest_month:
                continue
            span = (month_end(latest_month) - earliest).days
            p.true_death = earliest + timedelta(days=self.rng.randint(0, span))

    def _plan_ma_disenrollments(self) -> None:
        """Some MA-only members leave for another MA plan at an AEP (they leave the data)."""
        years = list(range(self.start.year + 1, self.end.year + 1))
        for p in self.persons:
            if p.switcher or p.true_death or p.spans[0].payer != MA:
                continue
            if self.rng.random() >= MA_DISENROLL_PROBABILITY:
                continue
            eligible = [y for y in years if date(y, 1, 1) > add_months(p.spans[0].start, 6)]
            if eligible:
                eff = date(self.rng.choice(eligible), 1, 1)
                p.spans = [Span(MA, p.spans[0].start, eff - timedelta(days=1))]

    def _plan_switcher_moves(self) -> None:
        """A few switchers move once; never in a month they switch (one row per month)."""
        for p in self.persons:
            if not p.switcher or self.rng.random() >= SWITCHER_MOVE_SHARE:
                continue
            switch_months = {s.start for s in p.spans}
            months = [m for m in self.months[1:] if m not in switch_months]
            when = self.rng.choice(months)
            p.moves.append((when, moved_address(self.rng, p.address)))

    # -- answer key ---------------------------------------------------------
    def write_crossover(self, key_dir: Path) -> None:
        """answer_key/crossover_members.csv: one row per switch event."""
        key_dir.mkdir(parents=True, exist_ok=True)
        write_csv(
            key_dir / "crossover_members.csv",
            ["BENE_MBI_ID", "MEMBER_ID", "direction", "effective_date", "enrollment_period"],
            [
                [s.mbi, s.member_id, s.direction, fmt_date(s.effective), s.period]
                for s in self.switches
            ],
        )
