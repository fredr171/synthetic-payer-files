"""Shared constants and helpers for the synthetic CCLF generator and validator.

Column lists follow the field names supplied in the project brief (a subset of the
CMS CCLF Information Packet layouts, in packet order). RECORD_CHANGE_TYPE on CCLF8
is a synthetic helper column, not a real CCLF field.
"""

from __future__ import annotations

import contextlib
import csv
import io
import re
from datetime import date, timedelta
from pathlib import Path

KB = 1024

CCLF8_COLUMNS = [
    "BENE_MBI_ID", "BENE_HIC_NUM", "BENE_FIPS_STATE_CD", "BENE_FIPS_CNTY_CD", "BENE_ZIP_CD",
    "BENE_DOB", "BENE_SEX_CD", "BENE_RACE_CD", "BENE_AGE", "BENE_MDCR_STUS_CD",
    "BENE_DUAL_STUS_CD", "BENE_DEATH_DT", "BENE_RNG_BGN_DT", "BENE_RNG_END_DT",
    "BENE_1ST_NAME", "BENE_MIDL_NAME", "BENE_LAST_NAME", "BENE_ORGNL_ENTLMT_RSN_CD",
    "BENE_ENTLMT_BUYIN_IND", "BENE_PART_A_ENRLMT_BGN_DT", "BENE_PART_B_ENRLMT_BGN_DT",
    "BENE_LINE_1_ADR", "GEO_ZIP_PLC_NAME", "GEO_USPS_STATE_CD", "GEO_ZIP5_CD",
    "RECORD_CHANGE_TYPE",
]  # fmt: skip

CCLF1_COLUMNS = [
    "CUR_CLM_UNIQ_ID", "PRVDR_OSCAR_NUM", "BENE_MBI_ID", "BENE_HIC_NUM", "CLM_TYPE_CD",
    "CLM_FROM_DT", "CLM_THRU_DT", "CLM_BILL_FAC_TYPE_CD", "CLM_BILL_CLSFCTN_CD",
    "PRNCPL_DGNS_CD", "ADMTG_DGNS_CD", "CLM_MDCR_NPMT_RSN_CD", "CLM_PMT_AMT",
    "CLM_NCH_PRMRY_PYR_CD", "PRVDR_FAC_FIPS_ST_CD", "BENE_PTNT_STUS_CD", "DGNS_DRG_CD",
    "CLM_OP_SRVC_TYPE_CD", "FAC_PRVDR_NPI_NUM", "ATNDG_PRVDR_NPI_NUM", "CLM_ADJSMT_TYPE_CD",
    "CLM_EFCTV_DT", "CLM_IDR_LD_DT", "CLM_ADMSN_TYPE_CD", "CLM_ADMSN_SRC_CD",
    "CLM_BILL_FREQ_CD", "DGNS_PRCDR_ICD_IND", "CLM_MDCR_INSTNL_TOT_CHRG_AMT", "CLM_CNTL_NUM",
    "CLM_ORG_CNTL_NUM",
]  # fmt: skip

CCLF4_COLUMNS = [
    "CUR_CLM_UNIQ_ID", "BENE_MBI_ID", "BENE_HIC_NUM", "CLM_TYPE_CD", "CLM_PROD_TYPE_CD",
    "CLM_VAL_SQNC_NUM", "CLM_DGNS_CD", "PRVDR_OSCAR_NUM", "CLM_FROM_DT", "CLM_THRU_DT",
    "CLM_POA_IND", "DGNS_PRCDR_ICD_IND",
]  # fmt: skip

CCLF5_COLUMNS = [
    "CUR_CLM_UNIQ_ID", "CLM_LINE_NUM", "BENE_MBI_ID", "BENE_HIC_NUM", "CLM_TYPE_CD",
    "CLM_FROM_DT", "CLM_THRU_DT", "RNDRG_PRVDR_TYPE_CD", "RNDRG_PRVDR_FIPS_ST_CD",
    "CLM_PRVDR_SPCLTY_CD", "CLM_FED_TYPE_SRVC_CD", "CLM_POS_CD", "CLM_LINE_FROM_DT",
    "CLM_LINE_THRU_DT", "CLM_LINE_HCPCS_CD", "CLM_LINE_CVRD_PD_AMT", "CLM_LINE_PRMRY_PYR_CD",
    "CLM_LINE_DGNS_CD", "CLM_RNDRG_PRVDR_TAX_NUM", "RNDRG_PRVDR_NPI_NUM", "CLM_CARR_PMT_DNL_CD",
    "CLM_PRCSG_IND_CD", "CLM_ADJSMT_TYPE_CD", "CLM_EFCTV_DT", "CLM_IDR_LD_DT", "CLM_CNTL_NUM",
    "CLM_LINE_ALOWD_CHRG_AMT", "CLM_LINE_SRVC_UNIT_QTY", "HCPCS_1_MDFR_CD", "CLM_DGNS_1_CD",
    "CLM_DGNS_2_CD", "CLM_DGNS_3_CD", "DGNS_PRCDR_ICD_IND",
]  # fmt: skip

FILE_COLUMNS = {
    "CCLF8": CCLF8_COLUMNS,
    "CCLF1": CCLF1_COLUMNS,
    "CCLF4": CCLF4_COLUMNS,
    "CCLF5": CCLF5_COLUMNS,
}

# Size targets in bytes: (min, max). The first delivered month is the initial load.
SIZE_RANGE_MONTHLY = (3 * KB, 5 * KB)
SIZE_RANGE_INITIAL = {
    "CCLF8": (15 * KB, 25 * KB),
    "CCLF1": (8 * KB, 12 * KB),
    "CCLF4": (8 * KB, 12 * KB),
    "CCLF5": (8 * KB, 12 * KB),
}

ADJ_ORIGINAL, ADJ_CANCEL, ADJ_ADJUSTMENT = "0", "1", "2"

# MBI character classes (CMS MBI format spec): letters exclude S, L, O, I, B, Z.
MBI_ALPHA = "ACDEFGHJKMNPQRTUVWXY"
MBI_REGEX = re.compile(
    rf"^[1-9][{MBI_ALPHA}][{MBI_ALPHA}0-9][0-9][{MBI_ALPHA}][{MBI_ALPHA}0-9][0-9][{MBI_ALPHA}][{MBI_ALPHA}][0-9][0-9]$"
)


def luhn_check_digit(payload: str) -> int:
    """Return the Luhn check digit for a numeric payload string."""
    total = 0
    for i, ch in enumerate(reversed(payload)):
        d = int(ch)
        if i % 2 == 0:  # rightmost payload digit is doubled (check digit goes to its right)
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - total % 10) % 10


def npi_is_valid(npi: str) -> bool:
    """NPI check: 10 digits, Luhn over the '80840' prefix + first 9 digits."""
    return len(npi) == 10 and npi.isdigit() and luhn_check_digit("80840" + npi[:9]) == int(npi[9])


def add_months(d: date, n: int) -> date:
    """First day of the month n months after d's month."""
    idx = d.year * 12 + (d.month - 1) + n
    return date(idx // 12, idx % 12 + 1, 1)


def month_end(d: date) -> date:
    """Last day of d's month."""
    return add_months(d, 1) - timedelta(days=1)


def month_range(start: date, end: date) -> list[date]:
    """First-of-month dates from start to end inclusive."""
    months, cur = [], date(start.year, start.month, 1)
    while cur <= end:
        months.append(cur)
        cur = add_months(cur, 1)
    return months


def parse_month(text: str) -> date:
    """Parse 'YYYY-MM' into the first day of that month."""
    year, month = text.split("-")
    return date(int(year), int(month), 1)


def month_key(d: date) -> str:
    """'YYYY-MM' folder name for a month."""
    return f"{d.year:04d}-{d.month:02d}"


def file_name(file_type: str, d: date) -> str:
    """CCLF{n}_YYYYMM.csv"""
    return f"{file_type}_{d.year:04d}{d.month:02d}.csv"


def csv_line(values: list[str]) -> str:
    """Serialize one CSV record exactly as the writer does (LF terminated)."""
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerow(values)
    return buf.getvalue()


def csv_line_bytes(values: list[str]) -> int:
    """UTF-8 byte length of one serialized CSV record."""
    return len(csv_line(values).encode("utf-8"))


def fmt_amt(x: float) -> str:
    """Amount with exactly 2 decimals."""
    return f"{x:.2f}"


def fmt_date(d: date | None) -> str:
    """YYYY-MM-DD, or empty string for missing."""
    return d.isoformat() if d else ""


def write_csv(path: Path, columns: list[str], rows: list[list[str]]) -> None:
    """UTF-8 CSV, header row, LF line endings."""
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(columns)
        writer.writerows(rows)


def remove_stale_csvs(folder: Path, pattern: str) -> None:
    """Delete previously generated CSVs (files only: OneDrive can lock folders) and
    empty month folders, so a run with a shorter --end leaves no stale months behind."""
    if not folder.exists():
        return
    for path in folder.glob(pattern):
        path.unlink()
    for sub in folder.iterdir():
        if sub.is_dir() and not any(sub.iterdir()):
            with contextlib.suppress(OSError):  # locked by a sync client; harmless
                sub.rmdir()


def print_size_summary(sizes: dict[tuple[str, str], int], types: tuple[str, ...]) -> None:
    """Per-month size table (KB) and min/max per file type (initial month excluded)."""
    months = sorted({m for m, _ in sizes})
    print(f"\n{'month':<8}" + "".join(f"{t:>12}" for t in types))
    for m in months:
        print(f"{m:<8}" + "".join(f"{sizes[(m, t)] / KB:>11.1f}K" for t in types))
    print("\nmonthly files (excluding initial load) min / max KB:")
    for t in types:
        vals = [sizes[(m, t)] / KB for m in months[1:]]
        if vals:
            print(f"  {t}: {min(vals):.1f} / {max(vals):.1f}")
    total = sum(sizes.values())
    print(f"\n{len(sizes)} files, {total / KB:.1f} KB total")


MONTH_DIR_RE = re.compile(r"^\d{4}-\d{2}$")


def month_dirs(folder: Path) -> list[Path]:
    """Delivery month folders (YYYY-MM) directly under folder; other folders are ignored."""
    return sorted(p for p in folder.iterdir() if p.is_dir() and MONTH_DIR_RE.match(p.name))


def coverage_spans(
    rows: list[dict[str, str]], key_col: str, start_col: str, end_col: str
) -> dict[str, list[tuple[date, date | None]]]:
    """Coverage spans per member from rows in delivery order.

    A span is identified by its start date; its end is the end date on the LATEST row
    carrying that start (full-row updates restate the current value)."""
    spans: dict[str, dict[str, str]] = {}
    for row in rows:
        spans.setdefault(row[key_col], {})[row[start_col]] = row[end_col]
    return {
        key: sorted(
            (date.fromisoformat(s), date.fromisoformat(e) if e else None) for s, e in by.items()
        )
        for key, by in spans.items()
    }


def in_any_span(spans: list[tuple[date, date | None]], lo: date, hi: date) -> bool:
    """True if [lo, hi] lies entirely inside one span."""
    return any(s <= lo and (e is None or hi <= e) for s, e in spans)


def overlaps_any_span(spans: list[tuple[date, date | None]], lo: date, hi: date) -> bool:
    """True if [lo, hi] touches any span."""
    return any(s <= hi and (e is None or lo <= e) for s, e in spans)
