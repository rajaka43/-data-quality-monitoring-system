"""
dq_engine.py — Automated Data Quality Checks & Logic
=====================================================
Core validation engine for the DQMS.  Accepts a pandas DataFrame and
runs a full battery of DQ checks across three dimensions:

  Completeness  → null / missing value detection
  Uniqueness    → duplicate row / key detection
  Validity      → format regex & numeric bounds checking

Outputs a single standardised metadata dictionary (DQReport) that the
Streamlit UI and the SQLite observability store both consume.

Architecture:
  DataQualityEngine          ← public entry-point class
    │
    ├── clean_data()         ← CALL FIRST.  Remediates self.df in-place and
    │                           returns (cleaned_df, CleanSummary).  After this
    │                           call self.df reflects the cleaned state.
    │
    ├── run()                ← CALL SECOND.  Runs all DQ checks against
    │                           self.df (which is now the cleaned frame when
    │                           the two-phase flow is used) and returns a
    │                           DQReport whose KPIs, alerts, and scores all
    │                           reflect the post-clean dataset.
    │
    ├── _check_completeness  ← operates on self.df
    ├── _check_uniqueness    ← operates on self.df
    ├── _check_validity      ← operates on self.df
    ├── _compute_health      ← weighted health score (0–100)
    └── _build_alerts        ← threshold-based alert generation

  ObservabilityStore         ← SQLite-backed run history
    ├── save_run             ← persist a DQReport
    └── load_history         ← retrieve trend data for charts

Recommended call sequence in app.py
-------------------------------------
  engine = DataQualityEngine(raw_df, dataset_name=filename)
  cleaned_df, clean_summary = engine.clean_data()   # mutates self.df
  report = engine.run()                             # scored on cleaned data
  store.save_run(report)
"""

import hashlib
import json
import sqlite3
import traceback
from datetime import datetime
from typing import Any

import pandas as pd

from config import (
    COLUMN_TYPE_HINTS,
    CRITICAL_COLUMN_SUBSTRINGS,
    DATE_ISO_REGEX,
    EMAIL_REGEX,
    NUMERIC_BOUNDS,
    PHONE_REGEX,
    SEVERITY,
    THRESHOLDS,
    URL_REGEX,
    ZIP_REGEX,
)

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------
DQReport     = dict[str, Any]
CleanSummary = dict[str, Any]


# ---------------------------------------------------------------------------
# 1. HELPER UTILITIES
# ---------------------------------------------------------------------------

def _is_critical_column(col: str) -> bool:
    """Return True if the column name contains a critical-key substring."""
    col_lower = col.lower()
    return any(sub in col_lower for sub in CRITICAL_COLUMN_SUBSTRINGS)


def _detect_validation_type(col: str) -> str | None:
    """
    Map a column name to a validation type via COLUMN_TYPE_HINTS.
    Returns None if no hint matches → column skips format/bounds check.
    """
    col_lower = col.lower()
    for substring, vtype in COLUMN_TYPE_HINTS:
        if substring in col_lower:
            return vtype
    return None


def _validate_value(value: Any, vtype: str) -> bool:
    """
    Validate a single non-null value against a known validation type.
    Returns True if valid, False otherwise.
    """
    str_val = str(value).strip()

    if vtype == "email":
        return bool(EMAIL_REGEX.match(str_val))
    if vtype == "phone":
        return bool(PHONE_REGEX.match(str_val))
    if vtype == "url":
        return bool(URL_REGEX.match(str_val))
    if vtype == "date_iso":
        return bool(DATE_ISO_REGEX.match(str_val))
    if vtype == "zip":
        return bool(ZIP_REGEX.match(str_val))
    if vtype in NUMERIC_BOUNDS:
        try:
            num = float(value)
            bounds = NUMERIC_BOUNDS[vtype]
            return bounds["min"] <= num <= bounds["max"]
        except (ValueError, TypeError):
            return False
    return True  # Unknown type → assume valid


# ---------------------------------------------------------------------------
# 2. MAIN DQ ENGINE
# ---------------------------------------------------------------------------

class DataQualityEngine:
    """
    Orchestrates data cleaning and DQ checks for an uploaded DataFrame.

    Two-phase usage (recommended):
        engine               = DataQualityEngine(raw_df, "file.csv")
        cleaned_df, summary  = engine.clean_data()   # phase 1: clean
        report               = engine.run()           # phase 2: score clean data

    Single-phase usage (raw report only):
        engine  = DataQualityEngine(raw_df, "file.csv")
        report  = engine.run()
    """

    def __init__(self, df: pd.DataFrame, dataset_name: str = "unknown"):
        # self.df is the engine's working copy.
        # clean_data() updates self.df in-place so run() always sees the
        # most recent state, whether raw or cleaned.
        self.df           = df.copy()
        self._raw_df      = df.copy()   # immutable original kept for raw preview
        self.dataset_name = dataset_name
        self.run_id       = self._generate_run_id()
        self.timestamp    = datetime.utcnow().isoformat(timespec="seconds") + "Z"

    # ------------------------------------------------------------------
    # Convenience property — always reflects current working-copy shape
    # ------------------------------------------------------------------
    @property
    def n_rows(self) -> int:
        return len(self.df)

    @property
    def n_cols(self) -> int:
        return len(self.df.columns)

    # ------------------------------------------------------------------
    # PHASE 1: Data Cleaning — AUTO-REMEDIATION
    # ------------------------------------------------------------------

    def clean_data(self) -> tuple[pd.DataFrame, CleanSummary]:
        """
        Automatically clean the DataFrame and update self.df in-place.

        After this method returns, every subsequent call to run() or any
        private _check_*() method will operate on the cleaned data, ensuring
        that KPIs, alerts, charts, and the downloaded CSV are all consistent.

        Four ordered passes
        ────────────────────
        Pass 1 — Duplicates     : drop exact-match duplicate rows (keep first)
        Pass 2 — Critical nulls : drop rows where any critical column is null
        Pass 3 — Non-critical nulls: impute numerics with median; categoricals
                                     with mode or "Unknown"
        Pass 4 — Invalid formats: null invalid format values then re-impute;
                                  clamp out-of-bounds numerics to column median

        Returns
        ────────
        (cleaned_df, summary)
          cleaned_df — the fully remediated DataFrame (same object as self.df)
          summary    — CleanSummary dict with per-action audit log for the UI
        """
        # ── Snapshot raw stats BEFORE cleaning (for the summary diff) ──
        # We inspect self.df as it currently stands (may be raw or already
        # partially cleaned if clean_data is called again).
        raw_snapshot = self.df.copy()
        rows_start   = len(raw_snapshot)

        # Run DQ checks on the snapshot to inform cleaning decisions.
        # We call private methods directly on the snapshot so self.df is not
        # touched until we are ready to replace it atomically at the end.
        snap_engine              = _SnapshotChecker(raw_snapshot)
        completeness_pre         = snap_engine.check_completeness()
        uniqueness_pre           = snap_engine.check_uniqueness()
        validity_pre             = snap_engine.check_validity()

        # Working copy — all mutation happens here
        cleaned      = raw_snapshot.copy()
        actions: list[dict] = []

        # ── PASS 1: Drop semantic duplicate rows ────────────────────────
        # Semantic duplicates = rows identical on all NON-ID/key content
        # columns, even if their ID values differ (e.g. EmployeeID 101 vs 111
        # for the same person).  We also apply the NaN sentinel so rows that
        # share NaN in the same column are still compared as equal.
        _SENTINEL    = "__DQMS_NULL__"
        id_cols      = [c for c in cleaned.columns if _is_critical_column(c)]
        content_cols = [c for c in cleaned.columns if c not in id_cols]
        dedup_cols   = content_cols if content_cols else list(cleaned.columns)

        filled_mask = cleaned[dedup_cols].fillna(_SENTINEL).duplicated(keep="first")
        dup_count   = int(filled_mask.sum())
        if dup_count > 0:
            cleaned = cleaned[~filled_mask].reset_index(drop=True)
            actions.append({
                "pass":     1,
                "category": "Duplicates",
                "action":   "Dropped duplicate rows",
                "affected": dup_count,
                "detail":   "Kept first occurrence of each duplicate group",
            })

        # ── PASS 2: Drop rows with nulls in critical columns ───────────
        critical_cols_with_nulls = [
            c["column"]
            for c in completeness_pre["per_column"]
            if c["is_critical"] and c["null_count"] > 0
        ]
        rows_before_crit_drop = len(cleaned)
        if critical_cols_with_nulls:
            # Only dropna on columns that still exist (guard against edge cases)
            valid_crit_cols = [c for c in critical_cols_with_nulls if c in cleaned.columns]
            if valid_crit_cols:
                cleaned = cleaned.dropna(subset=valid_crit_cols).reset_index(drop=True)
                dropped = rows_before_crit_drop - len(cleaned)
                if dropped > 0:
                    actions.append({
                        "pass":     2,
                        "category": "Critical Nulls",
                        "action":   "Dropped rows with nulls in critical columns",
                        "affected": dropped,
                        "detail":   f"Columns: {', '.join(valid_crit_cols)}",
                    })

        # ── PASS 3: Impute non-critical nulls ──────────────────────────
        for col_stat in completeness_pre["per_column"]:
            col = col_stat["column"]
            if col not in cleaned.columns:
                continue
            if col_stat["is_critical"]:
                continue  # already handled in Pass 2

            null_count = int(cleaned[col].isna().sum())  # recount on cleaned frame
            if null_count == 0:
                continue

            series = cleaned[col]
            if pd.api.types.is_numeric_dtype(series):
                fill_val = series.median()
                if pd.isna(fill_val):
                    fill_val = series.mean()
                if pd.isna(fill_val):
                    fill_val = 0.0
                cleaned[col] = series.fillna(fill_val)
                actions.append({
                    "pass":     3,
                    "category": "Null Imputation",
                    "action":   f"Filled {null_count} nulls with median",
                    "affected": null_count,
                    "detail":   f"Column '{col}' → median = {fill_val:.4g}",
                })
            else:
                mode_s   = series.mode(dropna=True)
                fill_val = str(mode_s.iloc[0]) if not mode_s.empty else "Unknown"
                cleaned[col] = series.fillna(fill_val)
                actions.append({
                    "pass":     3,
                    "category": "Null Imputation",
                    "action":   f"Filled {null_count} nulls with mode/Unknown",
                    "affected": null_count,
                    "detail":   f"Column '{col}' → fill = '{fill_val}'",
                })

        # ── PASS 4: Remediate invalid-format / out-of-bounds values ────
        for col_stat in validity_pre["per_column"]:
            col   = col_stat["column"]
            vtype = col_stat["validation_type"]

            if col not in cleaned.columns:
                continue

            # Re-evaluate validity on the current cleaned frame
            non_null    = cleaned[col].dropna()
            invalid_idx = non_null.index[
                ~non_null.apply(lambda v: _validate_value(v, vtype))
            ]
            invalid_count = len(invalid_idx)

            if invalid_count == 0:
                continue

            if vtype in ("email", "phone", "url", "date_iso", "zip"):
                # Cannot safely synthesise a valid value → null then re-impute
                cleaned.loc[invalid_idx, col] = pd.NA
                actions.append({
                    "pass":     4,
                    "category": "Validity Fix",
                    "action":   f"Replaced {invalid_count} invalid values with NaN",
                    "affected": invalid_count,
                    "detail":   f"Column '{col}' ({vtype}) — nulled for safety",
                })
                # Re-impute the newly introduced NaNs
                remaining_nulls = int(cleaned[col].isna().sum())
                if remaining_nulls > 0:
                    series = cleaned[col]
                    if pd.api.types.is_numeric_dtype(series):
                        fill_val = series.median()
                        if pd.isna(fill_val):
                            fill_val = 0.0
                        cleaned[col] = series.fillna(fill_val)
                    else:
                        mode_s   = series.mode(dropna=True)
                        fill_val = str(mode_s.iloc[0]) if not mode_s.empty else "Unknown"
                        cleaned[col] = series.fillna(fill_val)
                    actions.append({
                        "pass":     4,
                        "category": "Validity Fix",
                        "action":   f"Re-imputed {remaining_nulls} newly-nulled values",
                        "affected": remaining_nulls,
                        "detail":   f"Column '{col}' → fill = '{fill_val}'",
                    })

            elif vtype in NUMERIC_BOUNDS:
                bounds  = NUMERIC_BOUNDS[vtype]
                num_col = pd.to_numeric(cleaned[col], errors="coerce")
                oob_idx = num_col[(num_col < bounds["min"]) | (num_col > bounds["max"])].index
                if len(oob_idx) > 0:
                    cleaned.loc[oob_idx, col] = pd.NA
                    num_col2  = pd.to_numeric(cleaned[col], errors="coerce")
                    fill_val  = num_col2.median()
                    if pd.isna(fill_val):
                        fill_val = (bounds["min"] + bounds["max"]) / 2
                    cleaned[col] = num_col2.fillna(fill_val)
                    actions.append({
                        "pass":     4,
                        "category": "Validity Fix",
                        "action":   f"Clamped {len(oob_idx)} out-of-bounds values to median",
                        "affected": len(oob_idx),
                        "detail":   (
                            f"Column '{col}' ({vtype}) bounds "
                            f"[{bounds['min']}, {bounds['max']}] → fill = {fill_val:.4g}"
                        ),
                    })

        # ── Atomically replace self.df with the cleaned frame ──────────
        # This is the key step: from this point forward every call to
        # _check_completeness / _check_uniqueness / _check_validity /
        # run() operates on the cleaned data.
        cleaned = cleaned.reset_index(drop=True)
        self.df = cleaned

        # ── Build summary ───────────────────────────────────────────────
        rows_end     = len(cleaned)
        rows_removed = rows_start - rows_end
        cells_fixed  = sum(a["affected"] for a in actions if a["pass"] in (3, 4))

        summary: CleanSummary = {
            "rows_before":               rows_start,
            "rows_after":                rows_end,
            "rows_removed":              rows_removed,
            "rows_removed_pct":          round(rows_removed / rows_start * 100, 2) if rows_start else 0,
            "cells_imputed":             cells_fixed,
            "duplicate_rows_removed":    dup_count,
            "critical_null_rows_removed": rows_before_crit_drop - rows_end,
            "actions":                   actions,
            "action_count":              len(actions),
            "passes_run":                sorted({a["pass"] for a in actions}),
        }

        return self.df, summary

    # ------------------------------------------------------------------
    # PHASE 2: DQ Report — always scores self.df as it currently stands
    # ------------------------------------------------------------------

    def run(self) -> DQReport:
        """
        Execute the full DQ pipeline against self.df and return a report.

        When called after clean_data(), self.df is the cleaned frame, so
        all KPIs, alerts, charts, and scores reflect the post-clean state.
        When called on a fresh engine without clean_data(), it scores raw data.
        """
        try:
            completeness = self._check_completeness()
            uniqueness   = self._check_uniqueness()
            validity     = self._check_validity()
            health_score = self._compute_health(completeness, uniqueness, validity)
            alerts       = self._build_alerts(completeness, uniqueness, validity, health_score)

            report: DQReport = {
                "run_id":        self.run_id,
                "timestamp":     self.timestamp,
                "dataset_name":  self.dataset_name,
                "total_rows":    self.n_rows,
                "total_columns": self.n_cols,
                "columns":       list(self.df.columns),
                "completeness":  completeness,
                "uniqueness":    uniqueness,
                "validity":      validity,
                "health_score":  health_score,
                "overall_pass":  health_score >= THRESHOLDS["health_warning"],
                "alerts":        alerts,
                "alert_count":   len(alerts),
                "critical_alert_count": sum(
                    1 for a in alerts if a["severity"] == SEVERITY["critical"]
                ),
                "status": (
                    "CRITICAL" if health_score < THRESHOLDS["health_critical"] else
                    "WARNING"  if health_score < THRESHOLDS["health_warning"]  else
                    "HEALTHY"
                ),
            }
            return report

        except Exception as exc:
            return {
                "run_id":       self.run_id,
                "timestamp":    self.timestamp,
                "dataset_name": self.dataset_name,
                "error":        str(exc),
                "traceback":    traceback.format_exc(),
                "health_score": 0,
                "status":       "ERROR",
                "alerts": [{
                    "severity":  SEVERITY["critical"],
                    "dimension": "System",
                    "column":    "N/A",
                    "message":   f"Engine error: {exc}",
                    "value":     None,
                    "threshold": None,
                    "pass":      False,
                }],
                "alert_count":          1,
                "critical_alert_count": 1,
                "overall_pass":         False,
            }

    # ------------------------------------------------------------------
    # DIMENSION 1 — Completeness  (operates on self.df)
    # ------------------------------------------------------------------

    def _check_completeness(self) -> dict:
        per_column  = []
        total_cells = self.n_rows * self.n_cols if self.n_cols > 0 else 1
        total_nulls = 0

        for col in self.df.columns:
            null_count  = int(self.df[col].isna().sum())
            null_frac   = null_count / self.n_rows if self.n_rows > 0 else 0.0
            complete    = 1.0 - null_frac
            is_critical = _is_critical_column(col)
            threshold   = 0.0 if is_critical else THRESHOLDS["max_null_fraction"]
            col_pass    = null_frac <= threshold

            per_column.append({
                "column":        col,
                "null_count":    null_count,
                "null_fraction": round(null_frac, 4),
                "completeness":  round(complete, 4),
                "is_critical":   is_critical,
                "threshold":     threshold,
                "pass":          col_pass,
            })
            total_nulls += null_count

        overall_null_frac = total_nulls / total_cells if total_cells else 0.0
        overall_score     = round(1.0 - overall_null_frac, 4)

        return {
            "per_column":    per_column,
            "overall_score": overall_score,
            "total_nulls":   int(total_nulls),
            "pass":          all(c["pass"] for c in per_column),
        }

    # ------------------------------------------------------------------
    # DIMENSION 2 — Uniqueness  (operates on self.df)
    # ------------------------------------------------------------------

    def _check_uniqueness(self) -> dict:
        # ── Semantic duplicate detection ────────────────────────────────
        # Two rows that share the same real-world data but differ only in
        # their ID/key column are SEMANTIC duplicates — they represent the
        # same entity entered twice.  A plain df.duplicated() across all
        # columns misses them because the ID values differ.
        #
        # Strategy:
        #   1. Split columns into "identity" (ID/key) vs "content" (everything else).
        #   2. Detect duplicates on CONTENT columns only, using a NaN sentinel
        #      so that rows sharing NaN in the same position are still caught
        #      (pandas NaN != NaN would otherwise hide them).
        #   3. Fall back to full-row deduplication when every column is an ID
        #      column (edge case: tables with only key columns).
        _SENTINEL = "__DQMS_NULL__"

        id_cols      = [c for c in self.df.columns if _is_critical_column(c)]
        content_cols = [c for c in self.df.columns if c not in id_cols]
        dedup_cols   = content_cols if content_cols else list(self.df.columns)

        df_filled = self.df[dedup_cols].fillna(_SENTINEL)
        dup_mask  = df_filled.duplicated(keep="first")
        dup_count = int(dup_mask.sum())
        dup_frac  = dup_count / self.n_rows if self.n_rows > 0 else 0.0
        overall_unique = 1.0 - dup_frac

        # Per-column uniqueness for ID/key columns (their values must be unique)
        per_column = []
        for col in id_cols:
            non_null      = self.df[col].dropna()
            unique_vals   = non_null.nunique()
            total_vals    = len(non_null)
            col_dup_frac  = 1.0 - (unique_vals / total_vals) if total_vals > 0 else 0.0
            col_pass      = col_dup_frac <= THRESHOLDS["max_duplicate_fraction"]
            per_column.append({
                "column":             col,
                "unique_count":       int(unique_vals),
                "total_non_null":     int(total_vals),
                "duplicate_fraction": round(col_dup_frac, 4),
                "pass":               col_pass,
            })

        row_pass = dup_frac <= THRESHOLDS["max_duplicate_fraction"]
        col_pass = all(c["pass"] for c in per_column) if per_column else True

        return {
            "duplicate_row_count":    dup_count,
            "duplicate_row_fraction": round(dup_frac, 4),
            "overall_score":          round(overall_unique, 4),
            "per_column":             per_column,
            "dedup_columns":          dedup_cols,   # exposed for UI/report
            "pass":                   row_pass and col_pass,
        }

    # ------------------------------------------------------------------
    # DIMENSION 3 — Validity  (operates on self.df)
    # ------------------------------------------------------------------

    def _check_validity(self) -> dict:
        per_column      = []
        validity_scores = []

        for col in self.df.columns:
            vtype = _detect_validation_type(col)
            if vtype is None:
                continue

            non_null = self.df[col].dropna()
            total    = len(non_null)
            if total == 0:
                per_column.append({
                    "column": col, "validation_type": vtype,
                    "valid_count": 0, "invalid_count": 0,
                    "validity_fraction": 1.0, "invalid_examples": [], "pass": True,
                })
                validity_scores.append(1.0)
                continue

            valid_mask    = non_null.apply(lambda v: _validate_value(v, vtype))
            valid_count   = int(valid_mask.sum())
            invalid_count = total - valid_count
            val_frac      = valid_count / total
            col_pass      = val_frac >= THRESHOLDS["min_validity_fraction"]

            per_column.append({
                "column":            col,
                "validation_type":   vtype,
                "valid_count":       valid_count,
                "invalid_count":     int(invalid_count),
                "validity_fraction": round(val_frac, 4),
                "invalid_examples":  non_null[~valid_mask].head(5).astype(str).tolist(),
                "pass":              col_pass,
            })
            validity_scores.append(val_frac)

        overall_score = (
            round(sum(validity_scores) / len(validity_scores), 4)
            if validity_scores else 1.0
        )
        return {
            "per_column":      per_column,
            "overall_score":   overall_score,
            "columns_checked": len(per_column),
            "pass":            all(c["pass"] for c in per_column) if per_column else True,
        }

    # ------------------------------------------------------------------
    # SCORING
    # ------------------------------------------------------------------

    def _compute_health(self, completeness: dict, uniqueness: dict, validity: dict) -> int:
        """Weighted health score: Completeness 45%, Uniqueness 30%, Validity 25%."""
        score = (
            completeness["overall_score"] * 0.45 +
            uniqueness["overall_score"]   * 0.30 +
            validity["overall_score"]     * 0.25
        ) * 100
        return max(0, min(100, int(round(score))))

    # ------------------------------------------------------------------
    # ALERTS
    # ------------------------------------------------------------------

    def _build_alerts(
        self, completeness: dict, uniqueness: dict, validity: dict, health_score: int
    ) -> list[dict]:
        alerts: list[dict] = []

        # Completeness alerts
        for c in completeness["per_column"]:
            if not c["pass"]:
                alerts.append({
                    "severity":  SEVERITY["critical"] if c["is_critical"] else SEVERITY["warning"],
                    "dimension": "Completeness",
                    "column":    c["column"],
                    "message": (
                        f"Column '{c['column']}' has {c['null_fraction']*100:.1f}% null values "
                        f"(threshold: {c['threshold']*100:.0f}%)"
                    ),
                    "value":     c["null_fraction"],
                    "threshold": c["threshold"],
                    "pass":      False,
                })

        # Uniqueness alerts
        if not uniqueness["pass"]:
            dup_pct = uniqueness["duplicate_row_fraction"] * 100
            alerts.append({
                "severity":  SEVERITY["critical"] if dup_pct > 10 else SEVERITY["warning"],
                "dimension": "Uniqueness",
                "column":    "ALL ROWS",
                "message": (
                    f"{uniqueness['duplicate_row_count']} duplicate rows detected "
                    f"({dup_pct:.1f}% of dataset)"
                ),
                "value":     uniqueness["duplicate_row_fraction"],
                "threshold": THRESHOLDS["max_duplicate_fraction"],
                "pass":      False,
            })
        for c in uniqueness["per_column"]:
            if not c["pass"]:
                alerts.append({
                    "severity":  SEVERITY["critical"],
                    "dimension": "Uniqueness",
                    "column":    c["column"],
                    "message": (
                        f"Critical column '{c['column']}' has "
                        f"{c['duplicate_fraction']*100:.1f}% duplicate values"
                    ),
                    "value":     c["duplicate_fraction"],
                    "threshold": THRESHOLDS["max_duplicate_fraction"],
                    "pass":      False,
                })

        # Validity alerts
        for c in validity["per_column"]:
            if not c["pass"]:
                examples = ", ".join(c["invalid_examples"][:3])
                alerts.append({
                    "severity":  SEVERITY["warning"],
                    "dimension": "Validity",
                    "column":    c["column"],
                    "message": (
                        f"Column '{c['column']}' ({c['validation_type']}) has "
                        f"{c['invalid_count']} invalid values. Examples: {examples}"
                    ),
                    "value":     c["validity_fraction"],
                    "threshold": THRESHOLDS["min_validity_fraction"],
                    "pass":      False,
                })

        # Overall health alert
        if health_score < THRESHOLDS["health_critical"]:
            alerts.insert(0, {
                "severity":  SEVERITY["critical"],
                "dimension": "Overall",
                "column":    "DATASET",
                "message": (
                    f"Dataset health score is critically low: {health_score}/100. "
                    "Immediate remediation required before pipeline use."
                ),
                "value":     health_score,
                "threshold": THRESHOLDS["health_critical"],
                "pass":      False,
            })

        return alerts

    # ------------------------------------------------------------------
    # UTILITIES
    # ------------------------------------------------------------------

    def _generate_run_id(self) -> str:
        raw = f"{self.dataset_name}-{datetime.utcnow().isoformat()}"
        return hashlib.md5(raw.encode()).hexdigest()[:12].upper()


# ---------------------------------------------------------------------------
# 3. INTERNAL SNAPSHOT CHECKER
#    Used by clean_data() to inspect a frame without touching self.df.
#    Not part of the public API.
# ---------------------------------------------------------------------------

class _SnapshotChecker:
    """
    Lightweight read-only wrapper that runs DQ checks against an arbitrary
    DataFrame snapshot.  Used exclusively by DataQualityEngine.clean_data()
    so that pre-cleaning DQ stats can be computed without mutating self.df.
    """

    def __init__(self, df: pd.DataFrame):
        self.df     = df
        self.n_rows = len(df)
        self.n_cols = len(df.columns)

    def check_completeness(self) -> dict:
        per_column  = []
        total_cells = max(self.n_rows * self.n_cols, 1)
        total_nulls = 0
        for col in self.df.columns:
            null_count  = int(self.df[col].isna().sum())
            null_frac   = null_count / self.n_rows if self.n_rows > 0 else 0.0
            is_critical = _is_critical_column(col)
            threshold   = 0.0 if is_critical else THRESHOLDS["max_null_fraction"]
            per_column.append({
                "column":        col,
                "null_count":    null_count,
                "null_fraction": round(null_frac, 4),
                "completeness":  round(1.0 - null_frac, 4),
                "is_critical":   is_critical,
                "threshold":     threshold,
                "pass":          null_frac <= threshold,
            })
            total_nulls += null_count
        overall_null_frac = total_nulls / total_cells
        return {
            "per_column":    per_column,
            "overall_score": round(1.0 - overall_null_frac, 4),
            "total_nulls":   int(total_nulls),
            "pass":          all(c["pass"] for c in per_column),
        }

    def check_uniqueness(self) -> dict:
        # Mirror the semantic duplicate logic from DataQualityEngine._check_uniqueness:
        # compare only content (non-ID) columns so rows with differing IDs but
        # identical data are correctly flagged. NaN sentinel prevents NaN!=NaN misses.
        _SENTINEL    = "__DQMS_NULL__"
        id_cols      = [c for c in self.df.columns if _is_critical_column(c)]
        content_cols = [c for c in self.df.columns if c not in id_cols]
        dedup_cols   = content_cols if content_cols else list(self.df.columns)

        df_filled = self.df[dedup_cols].fillna(_SENTINEL)
        dup_count = int(df_filled.duplicated(keep="first").sum())
        dup_frac  = dup_count / self.n_rows if self.n_rows > 0 else 0.0
        return {
            "duplicate_row_count":    dup_count,
            "duplicate_row_fraction": round(dup_frac, 4),
            "overall_score":          round(1.0 - dup_frac, 4),
            "per_column":             [],
            "dedup_columns":          dedup_cols,
            "pass":                   dup_frac <= THRESHOLDS["max_duplicate_fraction"],
        }

    def check_validity(self) -> dict:
        per_column      = []
        validity_scores = []
        for col in self.df.columns:
            vtype = _detect_validation_type(col)
            if vtype is None:
                continue
            non_null = self.df[col].dropna()
            total    = len(non_null)
            if total == 0:
                per_column.append({
                    "column": col, "validation_type": vtype,
                    "valid_count": 0, "invalid_count": 0,
                    "validity_fraction": 1.0, "invalid_examples": [], "pass": True,
                })
                validity_scores.append(1.0)
                continue
            valid_mask    = non_null.apply(lambda v: _validate_value(v, vtype))
            valid_count   = int(valid_mask.sum())
            invalid_count = total - valid_count
            val_frac      = valid_count / total
            per_column.append({
                "column":            col,
                "validation_type":   vtype,
                "valid_count":       valid_count,
                "invalid_count":     int(invalid_count),
                "validity_fraction": round(val_frac, 4),
                "invalid_examples":  non_null[~valid_mask].head(5).astype(str).tolist(),
                "pass":              val_frac >= THRESHOLDS["min_validity_fraction"],
            })
            validity_scores.append(val_frac)
        return {
            "per_column":      per_column,
            "overall_score":   round(sum(validity_scores)/len(validity_scores), 4) if validity_scores else 1.0,
            "columns_checked": len(per_column),
            "pass":            all(c["pass"] for c in per_column) if per_column else True,
        }


# ---------------------------------------------------------------------------
# 4. OBSERVABILITY STORE (SQLite-backed run history)
# ---------------------------------------------------------------------------

class ObservabilityStore:
    """
    Persists DQ run reports to a local SQLite database for trend analysis.
    The store is append-only; reports are never mutated after insertion.
    """

    def __init__(self, db_path: str = "dqms_history.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS dq_runs (
                    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id                TEXT NOT NULL,
                    timestamp             TEXT NOT NULL,
                    dataset_name          TEXT,
                    health_score          INTEGER,
                    status                TEXT,
                    total_rows            INTEGER,
                    total_columns         INTEGER,
                    alert_count           INTEGER,
                    critical_alert_count  INTEGER,
                    completeness_score    REAL,
                    uniqueness_score      REAL,
                    validity_score        REAL,
                    report_json           TEXT
                )
            """)
            conn.commit()

    def save_run(self, report: DQReport) -> None:
        try:
            comp = report.get("completeness", {})
            uniq = report.get("uniqueness",   {})
            val  = report.get("validity",     {})
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO dq_runs (
                        run_id, timestamp, dataset_name, health_score, status,
                        total_rows, total_columns, alert_count, critical_alert_count,
                        completeness_score, uniqueness_score, validity_score, report_json
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    report.get("run_id",       "UNKNOWN"),
                    report.get("timestamp",    datetime.utcnow().isoformat()),
                    report.get("dataset_name", "unknown"),
                    report.get("health_score", 0),
                    report.get("status",       "ERROR"),
                    report.get("total_rows",   0),
                    report.get("total_columns", 0),
                    report.get("alert_count",  0),
                    report.get("critical_alert_count", 0),
                    comp.get("overall_score",  0.0),
                    uniq.get("overall_score",  0.0),
                    val.get("overall_score",   0.0),
                    json.dumps(report, default=str),
                ))
                conn.commit()
        except Exception as exc:
            print(f"[ObservabilityStore] Failed to save run: {exc}")

    def load_history(self, limit: int = 50) -> pd.DataFrame:
        try:
            with sqlite3.connect(self.db_path) as conn:
                df = pd.read_sql_query(
                    """
                    SELECT run_id, timestamp, dataset_name, health_score, status,
                           total_rows, alert_count, critical_alert_count,
                           completeness_score, uniqueness_score, validity_score
                    FROM   dq_runs
                    ORDER  BY id DESC
                    LIMIT  ?
                    """,
                    conn, params=(limit,),
                )
            return df.iloc[::-1].reset_index(drop=True)
        except Exception:
            return pd.DataFrame()

    def load_report_json(self, run_id: str) -> DQReport | None:
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute(
                    "SELECT report_json FROM dq_runs WHERE run_id = ? LIMIT 1", (run_id,)
                )
                row = cur.fetchone()
            return json.loads(row[0]) if row else None
        except Exception:
            return None

    def get_run_count(self) -> int:
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute("SELECT COUNT(*) FROM dq_runs")
                return cur.fetchone()[0]
        except Exception:
            return 0
