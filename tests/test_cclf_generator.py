"""Tests for the synthetic CCLF generator (repo root).

Each test generates a full 36-month set into tmp_path (about a second), then runs the
validator. Negative tests corrupt a copy of the output to prove the validator catches
each class of defect rather than passing everything.
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
import validate_cclf  # noqa: E402
from cclf_common import MBI_REGEX, npi_is_valid  # noqa: E402


@pytest.fixture(scope="module")
def generated(tmp_path_factory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("cclf")
    out, key = root / "output", root / "answer_key"
    generate_cclf.main(["--seed", "7", "--output-dir", str(out), "--answer-key-dir", str(key)])
    return out, key


def _copy(generated: tuple[Path, Path], tmp_path: Path) -> tuple[Path, Path]:
    out, key = tmp_path / "output", tmp_path / "answer_key"
    shutil.copytree(generated[0], out)
    shutil.copytree(generated[1], key)
    return out, key


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


def _failed(out: Path, key: Path) -> list[str]:
    v = validate_cclf.Validator(out, key)
    v.run()
    return [c.name for c in v.checks if not c.passed]


def _digest(folder: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(folder.rglob("*.csv")):
        h.update(p.relative_to(folder).as_posix().encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def test_generated_output_passes_all_checks(generated):
    assert _failed(*generated) == []


def test_same_seed_is_byte_identical_and_other_seed_differs(generated, tmp_path):
    again, other = tmp_path / "again", tmp_path / "other"
    generate_cclf.main(["--seed", "7", "--output-dir", str(again),
                        "--answer-key-dir", str(tmp_path / "k1")])  # fmt: skip
    generate_cclf.main(["--seed", "8", "--output-dir", str(other),
                        "--answer-key-dir", str(tmp_path / "k2")])  # fmt: skip
    assert _digest(again) == _digest(generated[0])
    assert _digest(other) != _digest(generated[0])


def test_layout_is_144_files(generated):
    files = sorted(generated[0].rglob("*.csv"))
    assert len(files) == 144
    assert files[0].relative_to(generated[0]).as_posix() == "2023-01/CCLF1_202301.csv"


def test_identifier_formats():
    assert MBI_REGEX.match("1EG4TE5MK73")
    assert not MBI_REGEX.match("1SG4TE5MK73")  # S is not an allowed MBI letter
    assert npi_is_valid("1234567893")  # CMS's published example NPI
    assert not npi_is_valid("1234567890")


def test_validator_catches_missing_beneficiary(generated, tmp_path):
    out, key = _copy(generated, tmp_path)
    _rewrite(out / "2023-01" / "CCLF8_202301.csv", lambda h, rows: rows[1:])
    assert "claim MBIs exist in CCLF8 delivered same month or earlier" in _failed(out, key) or (
        "answer_key/final_members.csv = latest CCLF8 row per beneficiary" in _failed(out, key)
    )


def test_validator_catches_orphan_cancellation(generated, tmp_path):
    out, key = _copy(generated, tmp_path)
    path = out / "2023-01" / "CCLF1_202301.csv"
    # Remove every original from the initial load: later cancels/adjusts become orphans.
    _rewrite(path, lambda h, rows: [])
    _rewrite(out / "2023-01" / "CCLF4_202301.csv", lambda h, rows: [])
    failed = _failed(out, key)
    assert "cancellations/adjustments link to one earlier original (same claim)" in failed


def test_validator_catches_duplicate_claim_id(generated, tmp_path):
    out, key = _copy(generated, tmp_path)
    first_id = None

    def grab(h, rows):
        nonlocal first_id
        first_id = rows[0][0]
        return rows

    def reuse(h, rows):
        rows[0][0] = first_id
        return rows

    _rewrite(out / "2023-01" / "CCLF1_202301.csv", grab)
    _rewrite(out / "2023-02" / "CCLF1_202302.csv", reuse)
    assert "CUR_CLM_UNIQ_ID unique across all files and months" in _failed(out, key)


def test_validator_catches_principal_dx_mismatch(generated, tmp_path):
    out, key = _copy(generated, tmp_path)

    def change(h, rows):
        rows[0][h.index("PRNCPL_DGNS_CD")] = "Z9989"
        return rows

    _rewrite(out / "2024-06" / "CCLF1_202406.csv", change)
    assert "CCLF4: claim in same-month CCLF1, 1-6 dx rows, seq 0 = principal" in _failed(out, key)


def test_validator_catches_wrong_answer_key_total(generated, tmp_path):
    out, key = _copy(generated, tmp_path)

    def bump(h, rows):
        i = h.index("part_a_net_paid")
        rows[3][i] = f"{float(rows[3][i]) + 1:.2f}"
        return rows

    _rewrite(key / "net_paid_by_service_month.csv", bump)
    assert "answer_key/net_paid_by_service_month.csv matches recomputation" in _failed(out, key)
