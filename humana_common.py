"""Layouts and constants for the synthetic Humana Medicare Advantage files.

Field names are invented, modeled on typical MA payer extracts and the APCD Common Data
Layout; Humana's real extract layouts are proprietary. RECORD_CHANGE_TYPE is a synthetic
helper column. Contract IDs, plan names and member IDs are fake.
"""

from __future__ import annotations

import re
from datetime import date

from cclf_common import KB

HUM_ELIG_COLUMNS = [
    "MEMBER_ID", "MBI", "FIRST_NAME", "MIDDLE_INITIAL", "LAST_NAME", "DOB", "GENDER",
    "ADDRESS_LINE_1", "CITY", "STATE", "ZIP", "COUNTY_FIPS", "CONTRACT_ID", "PBP_ID",
    "PLAN_NAME", "PRODUCT_TYPE", "ENROLLMENT_START_DT", "ENROLLMENT_END_DT", "PCP_NPI",
    "PCP_TIN", "PCP_ATTRIBUTION_START_DT", "DUAL_FLAG", "LIS_FLAG", "ESRD_FLAG",
    "HOSPICE_FLAG", "DEATH_DT", "FILE_AS_OF_DT", "RECORD_CHANGE_TYPE",
]  # fmt: skip

DX_COLUMNS = [f"DX_CODE_{i}" for i in range(1, 13)]
HUM_MEDCLM_COLUMNS = [
    "CLAIM_ID", "CLAIM_LINE_NUM", "ORIG_CLAIM_ID", "CLAIM_STATUS", "CLAIM_FREQ_CD",
    "CLAIM_FORM_TYPE", "BILL_TYPE", "MEMBER_ID", "MBI", "CONTRACT_ID", "PBP_ID",
    "SERVICE_FROM_DT", "SERVICE_TO_DT", "ADMIT_DT", "DISCHARGE_DT", "ADMIT_TYPE",
    "ADMIT_SOURCE", "DISCHARGE_STATUS", "DRG_CD", "LINE_FROM_DT", "LINE_TO_DT", "REVENUE_CD",
    "PROCEDURE_CD", "MODIFIER_1", "UNITS", "PLACE_OF_SERVICE", "BILLING_NPI", "BILLING_TIN",
    "RENDERING_NPI", "ATTENDING_NPI", "FACILITY_CCN", "PROVIDER_SPECIALTY", "IN_NETWORK_FLAG",
    "CAPITATED_FLAG", "BILLED_AMT", "ALLOWED_AMT", "PAID_AMT", "COPAY_AMT", "COINSURANCE_AMT",
    "DEDUCTIBLE_AMT", "ICD_VERSION", *DX_COLUMNS, "RECEIVED_DT", "PROCESSED_DT", "PAID_DT",
]  # fmt: skip

HUM_FILE_COLUMNS = {"HUM_ELIG": HUM_ELIG_COLUMNS, "HUM_MEDCLM": HUM_MEDCLM_COLUMNS}

# Line-level fields: everything else on a HUM_MEDCLM row is repeated claim-header data.
LINE_FIELDS = [
    "CLAIM_LINE_NUM", "LINE_FROM_DT", "LINE_TO_DT", "REVENUE_CD", "PROCEDURE_CD", "MODIFIER_1",
    "UNITS", "BILLED_AMT", "ALLOWED_AMT", "PAID_AMT", "COPAY_AMT", "COINSURANCE_AMT",
    "DEDUCTIBLE_AMT",
]  # fmt: skip
AMOUNT_FIELDS = [
    "BILLED_AMT", "ALLOWED_AMT", "PAID_AMT", "COPAY_AMT", "COINSURANCE_AMT", "DEDUCTIBLE_AMT",
]  # fmt: skip

# Size targets in bytes: (min, max). The first delivered month is the initial load.
HUM_SIZE_RANGE_MONTHLY = (4 * KB, 6 * KB)
HUM_SIZE_RANGE_INITIAL = {"HUM_ELIG": (15 * KB, 25 * KB), "HUM_MEDCLM": (10 * KB, 15 * KB)}

FREQ_ORIGINAL, FREQ_REPLACEMENT, FREQ_VOID = "1", "7", "8"
STATUS_PAID, STATUS_DENIED, STATUS_REVERSED = "P", "D", "R"
FORM_INSTITUTIONAL, FORM_PROFESSIONAL = "I", "P"

MEMBER_ID_RE = re.compile(r"^H\d{9}$")

# product -> (CONTRACT_ID, PBP_ID, PLAN_NAME)
PLANS = {
    "HMO": ("H9101", "001", "SYNTH GOLD PLUS HMO"),
    "D-SNP": ("H9101", "003", "SYNTH DUAL HMO D-SNP"),
    "PPO": ("H9102", "002", "SYNTH CHOICE PPO"),
}


def hum_file_name(file_type: str, d: date) -> str:
    """HUM_ELIG_YYYYMM.csv / HUM_MEDCLM_YYYYMM.csv"""
    return f"{file_type}_{d.year:04d}{d.month:02d}.csv"
