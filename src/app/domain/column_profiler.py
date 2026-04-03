"""Compute sample-based column profiles from Genie query results.

Profiles are computed from sampled rows (not the full dataset).
Each profile includes sample_size so consumers know the basis.
"""
import re
from collections import Counter

_NUMERIC_TYPES = {"INT", "BIGINT", "SMALLINT", "TINYINT", "FLOAT", "DOUBLE", "DECIMAL", "LONG"}


def _looks_temporal(values: list[str], threshold: float = 0.6) -> bool:
    """Check if a list of string values look like dates/months/years."""
    temporal_patterns = [
        re.compile(r'^\d{4}[-/]\d{1,2}([-/]\d{1,2})?$'),  # 2024-01, 2024/3/15
        re.compile(r'^\d{4}年\d{1,2}月'),                    # 2024年1月
        re.compile(r'^(Q[1-4]|[1-4]Q)\s*\d{4}$', re.I),     # Q1 2024
        re.compile(r'^\d{1,2}月$'),                           # 1月, 12月
        re.compile(r'^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)', re.I),
    ]
    if not values:
        return False
    matches = sum(1 for v in values if any(p.search(str(v)) for p in temporal_patterns))
    return matches / len(values) >= threshold


def _infer_semantic_role(col_name: str, type_name: str, profile_entry: dict) -> str:
    """Infer semantic role from column metadata and profile statistics."""
    dtype = profile_entry.get("dtype", "")

    # 1. Time detection
    if type_name.upper() in ("TIMESTAMP", "DATE"):
        return "time"
    if re.search(r'(?i)(date|month|year|quarter|period|_dt$|_at$)', col_name):
        return "time"

    # 2. Ordinal bin detection
    if re.search(r'(?i)(_bin|_band|_range|_bucket|_tier)', col_name):
        return "ordinal_bin"
    if dtype == "categorical":
        top_vals = profile_entry.get("top_values", [])
        if top_vals:
            bin_pattern = re.compile(r'\d+\s*[-~]\s*\d+')
            bin_count = sum(1 for v in top_vals if bin_pattern.search(str(v.get("value", ""))))
            if bin_count / len(top_vals) > 0.5:
                return "ordinal_bin"

    # 3. Share detection
    if dtype == "numeric":
        stats = profile_entry.get("stats", {})
        min_val = stats.get("min")
        max_val = stats.get("max")
        if min_val is not None and max_val is not None:
            if min_val >= 0 and max_val <= 1:
                if re.search(r'(?i)(ratio|rate|share|pct|percent|割合|構成比)', col_name):
                    return "share"

    # 4-6. Default roles
    if dtype == "numeric":
        return "measure"
    if dtype == "categorical":
        unique_count = profile_entry.get("unique_count", 0)
        if unique_count < 30:
            return "dimension"
        return "high_cardinality_dimension"

    return "dimension"  # fallback


def compute_column_profile(columns: list[dict], rows: list[list]) -> list[dict]:
    """Compute per-column profile from Genie result sample data.

    Args:
        columns: Column metadata from Genie [{"name": str, "type_name": str}, ...].
        rows: Sampled data rows (list of lists, values are strings or None).

    Returns:
        List of column profile dicts with sample_size indicating basis.
    """
    if not columns:
        return []

    n_rows = len(rows)
    profiles = []

    for col_idx, col_meta in enumerate(columns):
        col_name = col_meta.get("name", f"col_{col_idx}")
        type_name = col_meta.get("type_name", "STRING").upper()

        values = [row[col_idx] if col_idx < len(row) else None for row in rows]
        non_null = [v for v in values if v is not None and str(v).strip() != ""]
        null_count = n_rows - len(non_null)
        null_rate = null_count / n_rows if n_rows > 0 else 0

        is_numeric = type_name in _NUMERIC_TYPES
        if not is_numeric and non_null:
            parsed = 0
            for v in non_null:
                try:
                    float(v)
                    parsed += 1
                except (ValueError, TypeError):
                    pass
            is_numeric = parsed / len(non_null) > 0.8 if non_null else False

        if is_numeric:
            numeric_vals = []
            for v in non_null:
                try:
                    numeric_vals.append(float(v))
                except (ValueError, TypeError):
                    pass

            if numeric_vals:
                sorted_vals = sorted(numeric_vals)
                n = len(sorted_vals)
                mean = sum(sorted_vals) / n
                mid = n // 2
                median = (sorted_vals[mid] + sorted_vals[~mid]) / 2
                variance = sum((x - mean) ** 2 for x in sorted_vals) / n if n > 1 else 0
                stddev = variance ** 0.5
                stats = {
                    "min": sorted_vals[0],
                    "max": sorted_vals[-1],
                    "mean": round(mean, 4),
                    "median": round(median, 4),
                    "stddev": round(stddev, 4),
                }
            else:
                stats = {"min": None, "max": None, "mean": None, "median": None, "stddev": None}

            entry = {
                "name": col_name,
                "dtype": "numeric",
                "null_rate": null_rate,
                "unique_count": len(set(numeric_vals)) if numeric_vals else 0,
                "sample_size": n_rows,
                "stats": stats,
            }
            entry["semantic_role"] = _infer_semantic_role(col_name, type_name, entry)
            profiles.append(entry)
        else:
            str_vals = [str(v) for v in non_null]
            counter = Counter(str_vals)
            top_values = [{"value": val, "count": cnt} for val, cnt in counter.most_common(5)]
            entry = {
                "name": col_name,
                "dtype": "categorical",
                "null_rate": null_rate,
                "unique_count": len(counter),
                "sample_size": n_rows,
                "top_values": top_values,
            }
            entry["semantic_role"] = _infer_semantic_role(col_name, type_name, entry)
            profiles.append(entry)

    # Upgrade categorical columns that look temporal
    time_columns = [p["name"] for p in profiles if p.get("semantic_role") == "time"]
    for p in profiles:
        if p.get("dtype") == "categorical" and p["name"] not in time_columns:
            top_vals = p.get("top_values", [])
            if top_vals and _looks_temporal([v["value"] for v in top_vals]):
                time_columns.append(p["name"])
                p["semantic_role"] = "time"

    # Compute multi-series capability
    has_temporal = len(time_columns) > 0
    multi_series_candidates = [
        p["name"] for p in profiles
        if p.get("dtype") == "categorical"
        and p.get("semantic_role") not in ("time", "high_cardinality_dimension")
        and 2 <= p.get("unique_count", 0) <= 8
    ]
    can_form_multi_series = has_temporal and len(multi_series_candidates) > 0

    for p in profiles:
        p["has_temporal_axis"] = has_temporal
        p["candidate_time_columns"] = time_columns
        p["can_form_multi_series"] = can_form_multi_series

    return profiles
