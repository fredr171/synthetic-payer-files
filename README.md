# synthetic-payer-files

[![CI](https://github.com/fredr171/synthetic-payer-files/actions/workflows/ci.yml/badge.svg)](https://github.com/fredr171/synthetic-payer-files/actions/workflows/ci.yml)

Seeded, fully synthetic payer test files (CMS CCLF + a fictional Medicare Advantage payer) with answer keys. They
feed two lakehouses that build the same pipeline on different platforms:
[claims-lakehouse-dbt-duckdb](https://github.com/fredr171/claims-lakehouse-dbt-duckdb) (AWS) and
[claims-lakehouse-databricks-azure](https://github.com/fredr171/claims-lakehouse-databricks-azure)
(Azure + Databricks). Each pins a tagged release of this package for its end-to-end tests.

Two generators:

- **`generate-cclf`**: CMS **CCLF** (Claim and Claim Line Feed) monthly deliveries
  for Medicare fee-for-service (FFS) beneficiaries.
- **`generate-ma-payer`**: eligibility and medical-claims extracts from a fictional
  **Medicare Advantage** (MA) payer.

Both draw their people from one shared member registry, so beneficiaries who switch
between FFS and MA keep the same MBI, name, DOB and address in both feeds, with no
overlapping coverage. Use them to practice incremental ETL: change capture,
late-arriving facts, claim reversals and adjustments, referential integrity, and
cross-payer member matching.

Nothing here is real: names, addresses, MBIs, HICNs, member IDs, NPIs, CCNs, TINs,
contract IDs and plan names are all generated. Any match to a real person, provider,
facility or plan is coincidental.

| File | Content | Grain |
|---|---|---|
| CCLF8 | Beneficiary demographics | one row per beneficiary per delivery in which it is new or changed |
| CCLF1 | Part A claim header | one row per claim version |
| CCLF4 | Part A diagnosis codes | 1–6 rows per CCLF1 claim version |
| CCLF5 | Part B physician claim lines | 1–4 lines per claim version |
| MAP_ELIG | MA member eligibility | one row per member per delivery in which it is new, changed or terminated |
| MAP_MEDCLM | MA medical claims (UB-04 and CMS-1500) | one row per claim line per claim version |

## Repository layout

```
src/synthetic_payer_files/
├── generate_cclf.py, validate_cclf.py, cclf_common.py
├── generate_ma_payer.py, validate_ma_payer.py, ma_payer_common.py
├── validate_crosspayer.py              FFS <-> MA consistency checks
├── member_registry.py                  shared people, coverage spans, switchers, providers
└── clinical_reference.py               shared diagnosis codes, DRGs, HCPCS fees
output/cms/YYYY-MM/CCLF*_YYYYMM.csv     CCLF files (seed 42)
output/ma_payer/YYYY-MM/MAP_*.csv       MA payer files (seed 42)
answer_key/                             CCLF expected results + crossover_members.csv
answer_key/ma_payer/                    MA payer expected results
tests/                                  test_cclf_generator.py, test_ma_payer_generator.py
```

## Install and regenerate

```bash
python -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev]"   # Windows path
generate-cclf   --seed 42 --start 2023-01 --end 2025-12
generate-ma-payer --seed 42 --start 2023-01 --end 2025-12
validate-cclf
validate-ma-payer
validate-crosspayer
pytest
```

The commands are console scripts; `python -m synthetic_payer_files.generate_cclf` works too.
Default paths are relative to the working directory, so run them from the repo root.
Standard library only (no Faker). Same seed + months = byte-identical output
(covered by the tests). **Use the same `--seed`, `--start` and `--end` for both
generators**, or the switchers will not line up. Options:

- both: `--seed`, `--start`, `--end` (YYYY-MM)
- `generate-cclf`: `--output-dir` (default `output/cms/`), `--answer-key-dir` (default
  `answer_key/`)
- `generate-ma-payer`: `--output-dir` (default `output/ma_payer/`), `--answer-key-dir`
  (default `answer_key/ma_payer/`), `--crossover-dir` (default `answer_key/`)

Re-running deletes and replaces that generator's CSVs only. Both write the same
`answer_key/crossover_members.csv`.

## Versioning

Releases are git tags (`v1.0.0`, ...). The lakehouse installs one with
`pip install "synthetic-payer-files @ git+https://github.com/fredr171/synthetic-payer-files@v2.0.0"`.
Any change to seeded output (a new draw, a new field, a different distribution) is a new
**minor** or **major** version, never a re-tag: the lakehouse's tests hardcode seed-42
counts and its S3 deliveries came from a specific release.

## Shared member registry and payer switchers

`member_registry.py` builds every person once from the seed (stream
`registry:<seed>`), with a list of coverage spans `(payer, start, end)`:

- FFS population: 118 at the start, 7–10 newly attributed per month.
- MA population: 88 at the start, 2–3 new enrollees per month (4–7 each
  January). About 6% leave the MA payer for another MA plan each January 1.
- **Switchers** (15–20 people; 17 for seed 42), listed in
  `answer_key/crossover_members.csv` (`BENE_MBI_ID, MEMBER_ID, direction,
  effective_date, enrollment_period`):
  - most go **FFS → MA on January 1** (Annual Election Period) of 2024 or 2025;
  - 3 switch **mid-year** (effective February–April 1; MA Open Enrollment / SEP);
  - 3 go **MA → FFS on January 1** (2 are round trips: FFS → MA → FFS).
- Coverage never overlaps: the old span ends the day before the new one starts. Each
  generator only produces claims inside its own payer's spans.
- Switchers do not die or get random demographic updates. Their address moves are
  planned in the registry, so both feeds report the same address at every handoff.
- Providers come from one pool. Shared hospitals, SNF and professionals appear in both
  feeds; HHA and hospice are FFS-only; a few MA-only providers (a primary-care
  group and a cardiology group) plus one out-of-network hospital and one
  out-of-network urgent-care physician appear only in MA payer claims.

---

# CCLF (Medicare FFS)

## Format

- `output/cms/YYYY-MM/CCLF{8,1,4,5}_YYYYMM.csv`: 36 months × 4 files = 144 files.
- CSV with header row, comma-delimited, UTF-8 (ASCII in practice), LF line endings,
  empty string = missing. Real CCLF is fixed-width; CSV is intentional here.
- Dates `YYYY-MM-DD`, amounts with exactly 2 decimals (negative for cancellations).
- ICD-10-CM codes are stored **without the decimal point** (`E119`, `N1831`, `F32A`),
  as in real CMS claims files. Add the dot in your staging layer if you want it.
- `DGNS_PRCDR_ICD_IND = 0` (ICD-10) everywhere.
- Sizes: monthly files ~3.4–4.4 KB (range 3–5 KB). The first month is the initial
  load: CCLF8 ~18 KB, claims files ~9–10 KB. The generator tunes row counts to a byte
  budget per file and prints the actual sizes at the end.
- Column names and order follow the lists in the project brief, which are a subset of
  the CCLF Information Packet layouts. They were not checked against the packet
  itself, so expect differences from production CCLF files.

## Incremental load semantics

Each monthly file holds only what CMS "loaded" that month.

### CCLF8 (beneficiaries)

- **Initial month**: the starting population (118 beneficiaries), all `NEW`.
- **Later months**: newly attributed beneficiaries and switchers arriving from MA
  (`NEW`), plus changed records for existing ones (`UPDATE`): address/ZIP change, dual
  status change, Part B enrollment, death date added, FFS coverage ended or resumed.
- Every `UPDATE` is a **full row with current values**, never a partial update. The
  latest row per `BENE_MBI_ID` is the current state. `BENE_AGE` is computed as of the
  delivery month, so it moves forward over time even with no other change.
- A beneficiary appears at most once per monthly file.
- `RECORD_CHANGE_TYPE` (`NEW` / `UPDATE`) is a **synthetic helper column, not a real
  CCLF field**. Real CCLF8 sends no change flag; you have to diff against what you
  already hold. Try ignoring the column and deriving it yourself.
- MBIs follow the CMS MBI format and never change (no CCLF9 crosswalk is generated).
- Deaths: `BENE_DEATH_DT` and `BENE_RNG_END_DT` are filled in, in the death month or
  the month after. Dead beneficiaries get no further CCLF8 updates. Late-arriving
  claims with service dates before the death can still show up later.
- **Leaving FFS for MA**: in the month the MA coverage starts, the beneficiary gets an
  `UPDATE` with `BENE_RNG_END_DT` = the last day of FFS coverage (`FFS_END` in
  `member_changes.csv`). No FFS claims are dated after it. A beneficiary returning from
  MA gets an `UPDATE` with a new `BENE_RNG_BGN_DT` and a blank `BENE_RNG_END_DT`
  (`FFS_RETURN`). Someone who starts in MA and moves to FFS arrives as `NEW`.

### Claims (CCLF1 / CCLF4 / CCLF5)

- `CLM_IDR_LD_DT` (claim load date) always falls inside the delivery month. It
  decides which file a claim version lands in.
- About 90% of claims have service dates 0–3 months before the load month. About 10%
  are **late-arriving**, with service dates 4–12 months earlier (realized: 10.2% for seed 42).
- Every claim falls inside an FFS span (`BENE_RNG_BGN_DT`..`BENE_RNG_END_DT`), and
  none is after death or before Part B enrollment (Part B).
- Utilization is concentrated: the top 20% of beneficiaries generate about 80% of claims.

## Cancellations and adjustments

`CLM_ADJSMT_TYPE_CD`: `0` = original, `1` = cancellation, `2` = adjustment.

- About 5% of claim versions are cancellations and about 5% are adjustments. Each one
  targets an original delivered 1–6 months earlier, and each original is actioned at
  most once.
- The action reuses the original's identifying data (beneficiary, provider, service
  dates, diagnoses, lines) and gets a **new `CUR_CLM_UNIQ_ID`** and a new load date.
- **Linkage**: the action carries the **same `CLM_CNTL_NUM`** as its original. On
  CCLF1 it also sets `CLM_ORG_CNTL_NUM` to the original's control number (blank on
  originals). Treat `(file, CLM_CNTL_NUM)` as the claim family key.
- **Cancellation**: amounts are negated (`CLM_PMT_AMT`,
  `CLM_MDCR_INSTNL_TOT_CHRG_AMT`, and each Part B line's paid and allowed amounts), so
  original + cancel nets to zero. Part A `CLM_BILL_FREQ_CD = 8` (void).
- **Adjustment**: a full **replacement** with a changed payment amount, not a delta.
  Part A: `CLM_PMT_AMT` changes. Part B: one line is repriced. Part A
  `CLM_BILL_FREQ_CD = 7` (replacement).
- A naive `SUM()` over all rows is therefore **wrong**: it double-counts adjusted
  claims. The correct rule:

  > Group versions by `(part, CLM_CNTL_NUM)`. Cancelled family → 0. Adjusted family
  > → the adjustment's amount. Otherwise → the original's amount.
  > Part B amount = sum of `CLM_LINE_CVRD_PD_AMT` over the claim's lines.

## Answer key (`answer_key/`)

These files are written from the generator's in-memory simulation, not by re-reading
the CSVs. `validate_cclf.py` then re-derives them from the CSVs and checks that the
two agree.

| File | What your ETL should reproduce |
|---|---|
| `final_members.csv` | Latest state of every beneficiary (all CCLF8 columns except the helper, plus `FIRST_DELIVERY_MONTH`, `LAST_DELIVERY_MONTH`) |
| `net_paid_by_service_month.csv` | Part A / Part B / total net paid by month of the original `CLM_FROM_DT`, after cancellations and adjustments |
| `net_paid_by_load_month.csv` | Net change booked in each delivery month (originals, plus adjustment deltas, minus cancelled amounts). Sums to the same grand total |
| `claim_actions.csv` | Every cancellation/adjustment with its original: IDs, load months, amounts, final amount |
| `member_changes.csv` | Every CCLF8 row's reason (`NEW`, `ADDRESS`, `DUAL`, `PARTB`, `DEATH`, `FFS_END`, `FFS_RETURN`) and a short detail, useful for SCD2 testing |
| `crossover_members.csv` | Every FFS ↔ MA switch (shared with the MA payer generator) |
| `summary.json` | Counts and grand totals |

## Validator (`validate_cclf.py`)

It reads only the CSVs and the answer key, prints PASS/FAIL per check, and exits 1
on any failure. Checks:

- All 144 files present; headers match; date and amount formats
- File sizes within range (initial load and monthly)
- Valid MBIs; `NEW` exactly once and before any `UPDATE`; MBI→HICN stable
- Every claim MBI exists in a CCLF8 delivered the same month or earlier
- Every CCLF4 claim exists in the same month's CCLF1; 1–6 dx rows; sequence 0 =
  `PRNCPL_DGNS_CD`
- `CUR_CLM_UNIQ_ID` unique across all files and months
- `CLM_IDR_LD_DT` inside the delivery month; every NPI passes the Luhn check
- Cancellations/adjustments link to exactly one earlier original with the same
  identifying data; cancel = −original; adjustment ≠ original; shares 3–7%
- Claims inside an FFS span; none after death
- Late-arriving share 7–13%; utilization concentration
- Answer key files match a recomputation from the CSVs

---

# Fictional Medicare Advantage payer

> **The payer and its field names are invented.** These columns are modeled on typical MA payer eligibility/claims extracts and on the
> APCD Common Data Layout (CDL), so they look like what a payer sends. They are not any
> real payer's specification. Contract IDs (`H9101`, `H9102`), plan names and member IDs
> are fake.

## Format

- `output/ma_payer/YYYY-MM/MAP_{ELIG,MEDCLM}_YYYYMM.csv`: 36 months × 2 files = 72 files.
- Same CSV conventions as CCLF: header row, comma-delimited, LF, empty = missing,
  dates `YYYY-MM-DD`, amounts with 2 decimals, ICD-10 codes without the dot,
  `ICD_VERSION = 0`.
- Sizes: monthly files ~4.1–5.4 KB (range 4–6 KB). Initial load: MAP_ELIG ~17 KB
  (range 15–25 KB), MAP_MEDCLM ~12 KB (range 10–15 KB). Sizes print at the end.
- Products: `HMO` (H9101-001), `D-SNP` (H9101-003, dual-eligible members only) and
  `PPO` (H9102-002). Duals are mostly in the D-SNP.

## MAP_ELIG (eligibility)

- `MEMBER_ID` is `H` + 9 digits, 1:1 with `MBI` (the same MBI as in CCLF for
  switchers).
- **Initial month**: the starting roster (88 members), all `NEW`.
- **Later months**: new enrollees and switchers from FFS (`NEW`), changed members
  (`UPDATE`) and terminations (`TERM`). Each row is a **full row with current
  values**; at most one row per member per month.
- Change types (reason in `answer_key/ma_payer/member_changes.csv`): PCP change
  (`PCP_NPI`, `PCP_TIN`, `PCP_ATTRIBUTION_START_DT`), address change, dual/LIS change
  (D-SNP members stay dual), disenrollment, death.
- **Disenrollment** (`TERM`): delivered in the month after coverage ends, with
  `ENROLLMENT_END_DT` = the last covered day (to FFS, or to another MA plan).
- **Death** (`TERM`): `DEATH_DT` filled in, `ENROLLMENT_END_DT` = end of the death
  month, delivered in the death month or the month after. Nothing is delivered for a
  member after a `TERM`.
- `RECORD_CHANGE_TYPE` is a synthetic helper column, as in CCLF8.

## MAP_MEDCLM (medical claims)

- **One row per claim line**; header fields repeat on every line. The key is
  `(CLAIM_ID, CLAIM_LINE_NUM, CLAIM_FREQ_CD)`.
- Delivered by **`PROCESSED_DT` month**; `RECEIVED_DT` precedes it and the service
  dates precede both. About 10% of originals are late-arriving (service 4–12 months
  before processing).
- `CLAIM_FORM_TYPE` `I` (UB-04: inpatient `BILL_TYPE 11x` with DRG, outpatient/ER
  `13x`, SNF `21x`, with revenue codes and `FACILITY_CCN`) or `P` (CMS-1500:
  `PLACE_OF_SERVICE`, `RENDERING_NPI`, `PROVIDER_SPECIALTY`). Fields that do not apply
  to the form are blank.
- Up to 12 diagnosis codes (`DX_CODE_1..12`); `DX_CODE_1` is the principal.
- Amounts: `BILLED_AMT > ALLOWED_AMT >= PAID_AMT`, and `COPAY + COINSURANCE +
  DEDUCTIBLE = ALLOWED − PAID` (except capitated lines, below). Cost sharing follows
  the product: copays per visit type, per-day inpatient copay for days 1–5, SNF days
  21+, PPO radiology coinsurance and deductibles; D-SNP members pay $0.
- Claims fall only inside the member's MA enrollment and never after death.
- Out-of-network claims (`IN_NETWORK_FLAG = N`): some ER visits at a non-contracted
  hospital, and urgent-care visits (POS 20).

## Reversals, replacements, denials and capitation

`CLAIM_FREQ_CD`: `1` = original, `7` = replacement, `8` = void/reversal.
`CLAIM_STATUS`: `P` = paid, `D` = denied, `R` = reversed.

- **Replaced (~5% of originals)**: in one later month, a **reversal** (same
  `CLAIM_ID`, status `R`, freq `8`, every amount negated, `ORIG_CLAIM_ID` = the
  original) plus a **replacement** (new `CLAIM_ID`, freq `7`, `ORIG_CLAIM_ID` = the
  original, repriced so `PAID_AMT` changes).
- **Voided (~3%)**: the reversal only.
- **Denied (~3%)**: status `D`, `ALLOWED_AMT = PAID_AMT = 0`, billed kept.
- Institutional `BILL_TYPE` ends in the frequency code (`111`, `117`, `118`).
- Unlike CCLF, the reversal makes a **plain `SUM(PAID_AMT)` over every row correct**:
  original + reversal nets to zero and the replacement adds the new amount. To report
  by claim, group by family: the original `CLAIM_ID` = `ORIG_CLAIM_ID` on reversals
  and replacements.
- **Capitation**: about 30% of professional claims rendered by the member's own PCP
  are capitated encounters (`CAPITATED_FLAG = Y`, `PAID_AMT = 0`, `ALLOWED_AMT`
  populated, no `PAID_DT`). This applies only to HMO and D-SNP members whose PCP
  group holds a capitated arrangement. Capitated and denied claims are never reversed.

## Field mapping to CCLF

| Concept | CCLF | MA payer |
|---|---|---|
| Medicare beneficiary ID | `BENE_MBI_ID` | `MBI` (plus payer `MEMBER_ID`) |
| Name, DOB | `BENE_1ST_NAME`, `BENE_MIDL_NAME`, `BENE_LAST_NAME`, `BENE_DOB` | `FIRST_NAME`, `MIDDLE_INITIAL`, `LAST_NAME`, `DOB` |
| Sex | `BENE_SEX_CD` (`1`/`2`) | `GENDER` (`M`/`F`) |
| Address | `BENE_LINE_1_ADR`, `GEO_ZIP_PLC_NAME`, `GEO_USPS_STATE_CD`, `GEO_ZIP5_CD` | `ADDRESS_LINE_1`, `CITY`, `STATE`, `ZIP` |
| County | `BENE_FIPS_STATE_CD` + `BENE_FIPS_CNTY_CD` | `COUNTY_FIPS` (5 digits) |
| Coverage span | `BENE_RNG_BGN_DT`, `BENE_RNG_END_DT` | `ENROLLMENT_START_DT`, `ENROLLMENT_END_DT` |
| Dual status | `BENE_DUAL_STUS_CD` (code, `NA` = not dual) | `DUAL_FLAG` (`Y`/`N`), `LIS_FLAG` |
| Death | `BENE_DEATH_DT` | `DEATH_DT` (+ `TERM` row) |
| Change flag (synthetic) | `RECORD_CHANGE_TYPE` `NEW`/`UPDATE` | `RECORD_CHANGE_TYPE` `NEW`/`UPDATE`/`TERM` |
| Claim ID | `CUR_CLM_UNIQ_ID` (new per version) | `CLAIM_ID` (reversal reuses it; replacement is new) |
| Claim family key | `CLM_CNTL_NUM` (+ `CLM_ORG_CNTL_NUM` on CCLF1) | original `CLAIM_ID` = `ORIG_CLAIM_ID` |
| Version type | `CLM_ADJSMT_TYPE_CD` `0`/`1`/`2` | `CLAIM_FREQ_CD` `1`/`8`/`7` + `CLAIM_STATUS` |
| Delivery date | `CLM_IDR_LD_DT` | `PROCESSED_DT` |
| Institutional vs professional | separate files (CCLF1/4 vs CCLF5) | `CLAIM_FORM_TYPE` `I`/`P` in one file |
| Bill type | `CLM_BILL_FAC_TYPE_CD` + `CLM_BILL_CLSFCTN_CD` + `CLM_BILL_FREQ_CD` | `BILL_TYPE` (3 digits) |
| Service dates | `CLM_FROM_DT`, `CLM_THRU_DT` | `SERVICE_FROM_DT`, `SERVICE_TO_DT` (+ line dates) |
| Facility | `PRVDR_OSCAR_NUM`, `FAC_PRVDR_NPI_NUM` | `FACILITY_CCN`, `BILLING_NPI` |
| Rendering / attending | `RNDRG_PRVDR_NPI_NUM` (CCLF5), `ATNDG_PRVDR_NPI_NUM` (CCLF1) | `RENDERING_NPI`, `ATTENDING_NPI` |
| Provider TIN | `CLM_RNDRG_PRVDR_TAX_NUM` (CCLF5) | `BILLING_TIN` |
| DRG | `DGNS_DRG_CD` | `DRG_CD` |
| Diagnoses | CCLF4 rows (`CLM_VAL_SQNC_NUM`), `PRNCPL_DGNS_CD` | `DX_CODE_1..12` on every line |
| Procedure | `CLM_LINE_HCPCS_CD` (CCLF5) | `PROCEDURE_CD`, `REVENUE_CD` |
| Paid amount | `CLM_PMT_AMT` (CCLF1), `CLM_LINE_CVRD_PD_AMT` (CCLF5) | `PAID_AMT` (per line) |
| Allowed / charges | `CLM_LINE_ALOWD_CHRG_AMT` (CCLF5), `CLM_MDCR_INSTNL_TOT_CHRG_AMT` | `ALLOWED_AMT`, `BILLED_AMT` |
| Member cost share | (not in these layouts) | `COPAY_AMT`, `COINSURANCE_AMT`, `DEDUCTIBLE_AMT` |
| Plan / network / capitation | (FFS: n/a) | `CONTRACT_ID`, `PBP_ID`, `IN_NETWORK_FLAG`, `CAPITATED_FLAG` |
| Attributed PCP | (ACO attribution, not in CCLF8) | `PCP_NPI`, `PCP_TIN`, `PCP_ATTRIBUTION_START_DT` |

## Answer key (`answer_key/ma_payer/`)

| File | What your ETL should reproduce |
|---|---|
| `final_members.csv` | Latest state of every member (all MAP_ELIG columns except the helper, plus `FIRST_DELIVERY_MONTH`, `LAST_DELIVERY_MONTH`) |
| `net_paid_by_service_month.csv` | Institutional / professional / total net paid by the original's service month, after reversals and replacements |
| `net_paid_by_processed_month.csv` | Paid booked in each delivery month (sum of every row's `PAID_AMT`) |
| `capitated_encounters_by_month.csv` | Capitated claims, lines and allowed amount by service month |
| `claim_actions.csv` | Every void and replacement with its original, amounts and final paid |
| `member_changes.csv` | Every MAP_ELIG row's reason (`NEW`, `PCP`, `ADDRESS`, `DUAL_LIS`, `DISENROLL`, `DEATH`) and a detail |
| `summary.json` | Counts and grand totals |

## Validators

`validate_ma_payer.py` (output/ma_payer vs answer_key/ma_payer) checks: files, headers and
formats; sizes; MEMBER_ID/MBI format and 1:1; `NEW` first and nothing after `TERM`;
plan/contract consistency and D-SNP members being dual; claims only for delivered
members, inside an enrollment span and not after death; a unique claim-line key;
reversal/replacement linkage (negation, same month, paid changed) and the action
shares; form-type field rules; amount and cost-share arithmetic; date ordering; the
late share; capitation only on PCP-rendered claims at ~30%; out-of-network presence;
and every answer key file. It recomputes net paid as a plain `SUM(PAID_AMT)`, a
different method from the generator's claim-family logic.

`validate_crosspayer.py` (CCLF output vs MA payer output) checks: 15–20 switchers with
valid directions and dates; that the only MBIs in both feeds are the listed switchers;
no overlapping FFS/MA coverage, with the observed switches matching the crossover
list; no CCLF claims during MA coverage and no MA payer claims during FFS coverage; and
the same name/DOB/sex everywhere, plus the same address and dual status in each
switch month.

---

## Reference data used

- Providers: 6 facilities (2 shared hospitals, SNF, HHA, hospice, plus 1
  out-of-network MA hospital; Ohio CCNs) and 13 professionals across 6 TINs, all
  Luhn-valid and fake. CCLF uses the 14 FFS NPIs.
- Claim types (CCLF1): outpatient 40, inpatient 60 (with MS-DRG and length of stay),
  SNF 20, swing bed 30, home health 10, hospice 50. CCLF5 uses claim type 71.
- HCPCS: 99213, 99214, 99203, 99204, G0439, 36415, 80053, 85025, 83036, 93000,
  71046, 20610, at approximate national allowed amounts. CCLF paid is 80% of allowed;
  labs and the annual wellness visit are paid at 100% (no coinsurance). The MA payer adds
  ER levels 99283–99285 and G0463 clinic visits on the facility side.
- Population: ~10% under 65 (disabled, `BENE_ORGNL_ENTLMT_RSN_CD=1`, some ESRD=2),
  ~15% dual-eligible, a few ESRD, ~3% deaths.

## Tuning

Population, switcher and provider parameters live at the top of `member_registry.py`.
Byte targets and claim shares live at the top of `generate_cclf.py` and
`generate_ma_payer.py`. After any change, run all three validators across several seeds.
The tests run seed 7; a 30-seed sweep of all three validators passed when this was
written.

## AI assistance

I designed this project and directed and verified the work. I wrote the code with AI
assistance (Claude Code). Every generated file is checked by the three validators and
the test suite, and CI regenerates seed 42 and confirms it matches the committed files.

## License

[MIT](LICENSE)
