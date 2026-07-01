"""
config.py — Data Quality Metrics & Standards
=============================================
Central configuration for the Data Quality Monitoring System (DQMS).
All thresholds, business rules, and validation parameters are defined here
so they can be adjusted without touching engine or UI logic.

Architecture note:
  - Threshold constants drive pass/fail logic in dq_engine.py
  - Column-level rules support both generic and domain-specific validation
  - EMAIL_REGEX / DATE_FORMATS are shared across any column flagged as
    'email' or 'date' in COLUMN_TYPE_HINTS
"""

import re

# ---------------------------------------------------------------------------
# 1. GLOBAL HEALTH THRESHOLDS
# ---------------------------------------------------------------------------
# These numeric thresholds define what "acceptable" means for each dimension.
# All values are expressed as fractions (0.0–1.0) unless noted otherwise.

THRESHOLDS = {
    # Completeness: maximum fraction of nulls allowed per column before FAIL
    "max_null_fraction": 0.05,          # 5% null tolerance (critical alert ≥ this)

    # Uniqueness: maximum fraction of duplicate rows in the full dataset
    "max_duplicate_fraction": 0.02,     # 2% duplicate row tolerance

    # Validity: minimum fraction of values that must pass format checks
    "min_validity_fraction": 0.95,      # 95% of format-checked values must be valid

    # Overall health score boundaries (0–100 integer)
    "health_critical": 60,              # Below this → Critical (red)
    "health_warning": 80,               # Below this → Warning (yellow)
    # At or above health_warning → Healthy (green)
}

# ---------------------------------------------------------------------------
# 2. CRITICAL COLUMNS
# ---------------------------------------------------------------------------
# Columns listed here must satisfy uniqueness AND completeness, regardless of
# auto-detected types.  Any column name containing these substrings (case-
# insensitive) is automatically flagged as critical.

CRITICAL_COLUMN_SUBSTRINGS = [
    "id", "key", "uid", "uuid", "email", "phone", "order", "transaction"
]

# ---------------------------------------------------------------------------
# 3. FORMAT PATTERNS FOR VALIDITY CHECKS
# ---------------------------------------------------------------------------

# Standard email regex (RFC 5322 simplified)
EMAIL_REGEX = re.compile(
    r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"
)

# Phone: accepts common formats like +1-800-555-1234, (800)5551234, 8005551234
PHONE_REGEX = re.compile(
    r"^[\+]?[(]?[0-9]{1,4}[)]?[-\s\./0-9]{6,14}$"
)

# URL (http/https)
URL_REGEX = re.compile(
    r"^https?://[^\s/$.?#].[^\s]*$", re.IGNORECASE
)

# ISO date: YYYY-MM-DD
DATE_ISO_REGEX = re.compile(
    r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])$"
)

# Postal / ZIP code (US 5-digit or ZIP+4)
ZIP_REGEX = re.compile(r"^\d{5}(-\d{4})?$")

# ---------------------------------------------------------------------------
# 4. COLUMN TYPE HINT MAPPING
# ---------------------------------------------------------------------------
# Maps column-name substrings (lowercase) → validation type.
# The engine uses this to decide which regex / bounds check to apply.
# Order matters: first match wins.

COLUMN_TYPE_HINTS: list[tuple[str, str]] = [
    ("email",       "email"),
    ("phone",       "phone"),
    ("mobile",      "phone"),
    ("url",         "url"),
    ("website",     "url"),
    ("link",        "url"),
    ("zip",         "zip"),
    ("postal",      "zip"),
    ("date",        "date_iso"),
    ("dob",         "date_iso"),
    ("birth",       "date_iso"),
    ("created_at",  "date_iso"),
    ("updated_at",  "date_iso"),
    ("age",         "age_bounds"),
    ("salary",      "salary_bounds"),
    ("price",       "price_bounds"),
    ("amount",      "price_bounds"),
    ("score",       "score_bounds"),
    ("rating",      "rating_bounds"),
]

# ---------------------------------------------------------------------------
# 5. NUMERIC BOUNDS FOR RANGE VALIDATION
# ---------------------------------------------------------------------------

NUMERIC_BOUNDS = {
    "age_bounds":    {"min": 0,    "max": 120},
    "salary_bounds": {"min": 0,    "max": 10_000_000},
    "price_bounds":  {"min": 0,    "max": 1_000_000},
    "score_bounds":  {"min": 0,    "max": 100},
    "rating_bounds": {"min": 0,    "max": 5},
}

# ---------------------------------------------------------------------------
# 6. ALERT SEVERITY LEVELS
# ---------------------------------------------------------------------------

SEVERITY = {
    "critical": "CRITICAL",   # Immediate action required
    "warning":  "WARNING",    # Review recommended
    "info":     "INFO",       # Informational only
}

# ---------------------------------------------------------------------------
# 7. REPORT METADATA
# ---------------------------------------------------------------------------

REPORT_TITLE    = "Data Governance Framework & Health Report"
REPORT_VERSION  = "v1.0"
SYSTEM_NAME     = "Data Quality Monitoring System (DQMS)"
ORG_NAME        = "Data Engineering Division"
