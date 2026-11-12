"""Seeded, fully synthetic payer delivery files: CMS CCLF (Medicare FFS) and a fictional Medicare
Advantage (MA) payer.

Modules: generate_cclf / validate_cclf, generate_ma_payer / validate_ma_payer,
validate_crosspayer, plus the shared member_registry and clinical_reference.
Standard library only; the same seed and months give byte-identical output.
"""

__version__ = "2.0.0"
