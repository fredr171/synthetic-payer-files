"""Clinical reference data shared by the CCLF (FFS) and Humana (MA) generators.

ICD-10-CM codes are billable codes stored without the decimal point, as in CMS files.
Fee amounts approximate 2023 Medicare national rates; everything else is synthetic.
"""

from __future__ import annotations

import random

# Chronic ICD-10-CM codes with selection weights.
CHRONIC_DX = [
    ("I10", 22), ("E785", 18), ("E119", 14), ("E1122", 5), ("E1165", 5), ("I5022", 4),
    ("I509", 4), ("J449", 7), ("N1830", 3), ("N1831", 3), ("N1832", 2), ("F32A", 4),
    ("M1711", 4), ("M1712", 3), ("I4891", 5), ("I2510", 6), ("E039", 5), ("E669", 4),
    ("G309", 2), ("F0390", 2), ("M5450", 3), ("K5900", 2), ("R2681", 2),
]  # fmt: skip
ACUTE_OP_DX = [("R079", 3), ("N390", 3), ("R0602", 2), ("R531", 2), ("J189", 1)]
ESRD_DX = "N186"
Z_ANNUAL_WELLNESS = "Z0000"

# MS-DRG -> (principal dx, admitting dx, LOS range, payment range, admission type, weight)
DRGS = [
    ("291", "I5022", "R0602", (3, 7), (9500, 14500), "1", 14),
    ("292", "I509", "R0602", (2, 5), (6800, 9800), "1", 8),
    ("193", "J189", "R0602", (3, 7), (8500, 13000), "1", 9),
    ("194", "J189", "R0602", (2, 5), (6000, 8500), "1", 7),
    ("871", "A419", "R531", (4, 9), (11500, 19500), "1", 10),
    ("872", "A419", "R531", (3, 6), (6800, 9500), "1", 6),
    ("190", "J441", "R0602", (3, 6), (7200, 10500), "1", 7),
    ("470", "M1711", "M1711", (1, 3), (11000, 14500), "3", 7),
    ("065", "I639", "R531", (3, 6), (7800, 11000), "1", 5),
    ("690", "N390", "R531", (2, 4), (5000, 7000), "1", 6),
    ("683", "N179", "R531", (3, 5), (6500, 9000), "1", 5),
    ("638", "E1165", "R531", (2, 4), (5600, 8000), "1", 4),
    ("481", "S72001A", "S72001A", (4, 7), (14500, 19500), "1", 4),
]

# Professional HCPCS -> (2023 national allowed amount (approx), federal TOS, Medicare paid share)
HCPCS_FEES = {
    "99213": (92.00, "1", 0.80),
    "99214": (130.00, "1", 0.80),
    "99203": (110.00, "1", 0.80),
    "99204": (170.00, "1", 0.80),
    "G0439": (135.00, "1", 1.00),  # annual wellness visit: no coinsurance
    "36415": (3.00, "5", 1.00),  # labs paid at 100%
    "80053": (10.50, "5", 1.00),
    "85025": (7.80, "5", 1.00),
    "83036": (9.70, "5", 1.00),
    "93000": (16.50, "5", 0.80),
    "71046": (31.00, "4", 0.80),
    "20610": (65.00, "2", 0.80),
}
LAB_OR_PREVENTIVE = {"G0439", "36415", "80053", "85025", "83036"}
YEAR_FEE_FACTOR = {2022: 1.01, 2023: 1.00, 2024: 0.98, 2025: 0.97}


def weighted(rng: random.Random, pairs: list[tuple[str, int]]) -> str:
    """Weighted choice from (value, weight) pairs."""
    return rng.choices([p[0] for p in pairs], weights=[p[1] for p in pairs])[0]


def pick_dx(rng: random.Random, conditions: list[str], principal: str, count: int) -> list[str]:
    """Principal dx first, then the member's chronic conditions, then other chronic codes."""
    dx = [principal]
    pool = [c for c in conditions if c != principal]
    rng.shuffle(pool)
    while len(dx) < count and pool:
        dx.append(pool.pop())
    while len(dx) < count:
        extra = weighted(rng, CHRONIC_DX)
        if extra not in dx:
            dx.append(extra)
    return dx


def visit_lines(rng: random.Random, spclty: str) -> list[tuple[str, str]]:
    """(HCPCS, modifier) list for one professional encounter, 1-4 lines."""
    if spclty == "30":
        return [("71046", "")]
    if spclty == "20":
        return [("99213", "25"), ("20610", "")] if rng.random() < 0.6 else [("99213", "")]
    if spclty == "06":
        em = weighted(rng, [("99214", 60), ("99213", 30), ("99204", 10)])
        return [(em, "")] + ([("93000", "")] if rng.random() < 0.6 else [])
    if rng.random() < 0.12:
        lines = [("G0439", "")]
    else:
        em = weighted(rng, [("99213", 50), ("99214", 35), ("99203", 8), ("99204", 7)])
        lines = [(em, "")]
    if rng.random() < 0.40:
        lines.append(("36415", ""))
        lines.append((weighted(rng, [("80053", 50), ("83036", 30), ("85025", 20)]), ""))
    elif rng.random() < 0.12:
        lines.append(("93000", ""))
    return lines[:4]
