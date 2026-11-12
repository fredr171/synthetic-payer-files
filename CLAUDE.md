# synthetic-payer-files

Seeded synthetic payer delivery files: CMS CCLF (Medicare FFS) and a fictional Medicare
Advantage (MA) payer, plus answer keys. Consumed by the claims-lakehouse-dbt-duckdb and
claims-lakehouse-databricks-azure repos, which pin a release tag. See README.md.

| Path | What |
|---|---|
| `src/synthetic_payer_files/` | CCLF: `generate_cclf`, `validate_cclf`, `cclf_common`. MA payer: `generate_ma_payer`, `validate_ma_payer`, `ma_payer_common`. Shared: `member_registry` (people, coverage spans, switchers, providers), `clinical_reference`, `validate_crosspayer`. |
| `output/cms/`, `output/ma_payer/` | Seed-42 files, 2023-01..2025-12 (144 + 72 CSVs) |
| `answer_key/`, `answer_key/ma_payer/` | Expected results; `answer_key/crossover_members.csv` shared |
| `tests/` | pytest (seed 7 runs + negative tests that corrupt output) |

## Rules

- Standard library only; output must stay deterministic for a given seed. Never
  iterate over sets when making random choices.
- Everything is synthetic: no real names, addresses, identifiers, PHI or PII. The MA payer is
  fictional and its field names are invented; never model them on any real payer's files.
- Both generators share the registry: always run them with the same `--seed`,
  `--start` and `--end`. Each uses its own rng stream (`seed`, `registry:<seed>`,
  `ma_payer:<seed>`), so changing one generator's draws must not alter the other's
  output. A registry change alters both.
- After any change: regenerate both (`generate-cclf --seed 42`, `generate-ma-payer --seed 42`
  from the repo root), run `validate-cclf`, `validate-ma-payer` and `validate-crosspayer`, and
  sweep several seeds; all checks must pass. Run `pytest` and `ruff check .`.
- A change to seed-42 output is a NEW release (bump `version` in pyproject.toml and
  `__version__`, tag `vX.Y.Z`); never move an existing tag. The lakehouse then bumps its
  pin and updates its hardcoded counts (tests/test_dbt_intermediate.py there).
- Setup: `python -m venv .venv`, then `.venv/Scripts/python -m pip install -e ".[dev]"`.
