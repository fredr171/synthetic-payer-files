"""Tests for the synthetic Humana MA generator and the cross-payer validator (repo root).

One module fixture generates CCLF and Humana for the same seed into tmp_path; each test
copies it before corrupting anything. Negative tests prove the validators catch each
class of defect rather than passing everything.
"""

from __future__ import annotations

import csv
import hashlib
import shutil
import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_DIR))

import generate_cclf  # noqa: E402
import generate_humana  # noqa: E402
import validate_crosspayer  # noqa: E402
import validate_humana  # noqa: E402

SEED = "7"


def _generate(root: Path, seed: str = SEED) -> Path:
    out, key = root / "output", root / "answer_key"
    generate_cclf.main(["--seed", seed, "--output-dir", str(out), "--answer-key-dir", str(key)])
    generate_humana.main([
        "--seed", seed, "--output-dir", str(out / "humana"),
        "--answer-key-dir", str(key / "humana"), "--crossover-dir", str(key),
    ])  # fmt: skip
    return root


@pytest.fixture(scope="module")
def generated(tmp_path_factory) -> Path:
    return _generate(tmp_path_factory.mktemp("payers"))


@pytest.fixture
def copy(generated, tmp_path) -> Path:
    for name in ("output", "answer_key"):
        shutil.copytree(generated / name, tmp_path / name)
    return tmp_path


def _rewrite(path: Path, transform) -> None:
    """Apply transform(header, rows) -> rows to a CSV in place."""
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        header, rows = next(reader), list(reader)
    rows = transform(header, rows)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def _humana_failed(root: Path) -> list[str]:
    v = validate_humana.HumanaValidator(root / "output" / "humana", root / "answer_key" / "humana")
    v.run()
    return [c.name for c in v.checks if not c.passed]


def _crosspayer_ok(root: Path) -> bool:
    return validate_crosspayer.run(
        root / "output", root / "output" / "humana", root / "answer_key" / "crossover_members.csv"
    )


def _digest(folder: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(folder.rglob("*.csv")):
        h.update(p.relative_to(folder).as_posix().encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def _crossover(root: Path) -> list[dict[str, str]]:
    with (root / "answer_key" / "crossover_members.csv").open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _files(root: Path, pattern: str) -> list[Path]:
    return sorted((root / "output" / "humana").glob(pattern))


# -- positive ---------------------------------------------------------------
def test_generated_output_passes_all_checks(generated):
    assert _humana_failed(generated) == []
    assert _crosspayer_ok(generated)


def test_72_files_and_15_to_20_switchers(generated):
    assert len(_files(generated, "*/HUM_*.csv")) == 72
    switchers = {r["BENE_MBI_ID"] for r in _crossover(generated)}
    assert 15 <= len(switchers) <= 20
    directions = {r["direction"] for r in _crossover(generated)}
    assert directions == {"FFS_TO_MA", "MA_TO_FFS"}


def test_same_seed_is_byte_identical_and_other_seed_differs(generated, tmp_path):
    again = _generate(tmp_path / "again")
    other = _generate(tmp_path / "other", seed="8")
    assert _digest(again) == _digest(generated)
    assert _digest(other) != _digest(generated)


# -- negative: Humana validator ---------------------------------------------
def test_missing_reversal_is_caught(copy):
    for path in _files(copy, "*/HUM_MEDCLM_*.csv"):
        rows_before = path.read_text().count("\n")

        def drop(header, rows):
            freq = header.index("CLAIM_FREQ_CD")
            return [r for r in rows if r[freq] != "8"]

        _rewrite(path, drop)
        if path.read_text().count("\n") != rows_before:
            break
    assert "reversals negate an earlier original; replacements point to one" in _humana_failed(copy)


def test_paid_on_denied_line_is_caught(copy):
    for path in _files(copy, "*/HUM_MEDCLM_*.csv"):
        changed = []

        def pay(header, rows, changed=changed):
            status, paid = header.index("CLAIM_STATUS"), header.index("PAID_AMT")
            for r in rows:
                if r[status] == "D" and not changed:
                    r[paid] = "12.34"
                    changed.append(r)
            return rows

        _rewrite(path, pay)
        if changed:
            break
    assert "amounts: BILLED > ALLOWED >= PAID; cost share = ALLOWED - PAID" in _humana_failed(copy)


def test_claim_after_termination_is_caught(copy):
    """Pull an enrollment end date back a year: that member's claims fall outside coverage."""
    claimed = set()
    for path in _files(copy, "*/HUM_MEDCLM_*.csv"):
        with path.open(encoding="utf-8") as fh:
            claimed |= {r["MEMBER_ID"] for r in csv.DictReader(fh)}

    def term_early(header, rows):
        mid, start = header.index("MEMBER_ID"), header.index("ENROLLMENT_START_DT")
        for r in rows:
            if r[mid] in claimed:
                r[start] = "2026-01-01"
        return rows

    for path in _files(copy, "*/HUM_ELIG_*.csv"):
        _rewrite(path, term_early)
    assert "claims inside an enrollment span; none after death" in _humana_failed(copy)


# -- negative: cross-payer validator ----------------------------------------
def test_overlapping_coverage_is_caught(copy):
    """Start an FFS->MA switcher's MA enrollment a month before FFS ends."""
    switch = next(r for r in _crossover(copy) if r["direction"] == "FFS_TO_MA")

    def overlap(header, rows):
        mid, start = header.index("MEMBER_ID"), header.index("ENROLLMENT_START_DT")
        for r in rows:
            if r[mid] == switch["MEMBER_ID"] and r[start] == switch["effective_date"]:
                y, m, _ = (int(x) for x in r[start].split("-"))
                r[start] = f"{y - 1 if m == 1 else y}-{12 if m == 1 else m - 1:02d}-01"
        return rows

    for path in _files(copy, "*/HUM_ELIG_*.csv"):
        _rewrite(path, overlap)
    assert not _crosspayer_ok(copy)


def test_demographic_mismatch_is_caught(copy):
    mbi = _crossover(copy)[0]["BENE_MBI_ID"]

    def new_dob(header, rows):
        m, dob = header.index("MBI"), header.index("DOB")
        for r in rows:
            if r[m] == mbi:
                r[dob] = "1940-01-01"
        return rows

    for path in _files(copy, "*/HUM_ELIG_*.csv"):
        _rewrite(path, new_dob)
    assert not _crosspayer_ok(copy)
