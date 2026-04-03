"""Deterministic finding cards from step results.

Used by report_renderer only — not passed to evaluate or narrative LLMs.
Generates structured summaries with confidence, caveats, and key numbers.
"""


def build_finding_card(
    question: str,
    description: str,
    column_profile: list[dict],
    sample_rows: list[list],
    row_count: int,
    sample_size: int,
    is_truncated: bool = False,
) -> dict:
    """Build a deterministic finding card from step results.

    Returns:
        Dict with keys: claim, confidence, caveats, key_numbers
    """
    # Find primary dimension and measure columns
    dim_col = None
    measure_col = None
    measure_idx = None
    dim_idx = None

    for i, p in enumerate(column_profile):
        if measure_col is None and p.get("dtype") == "numeric":
            measure_col = p
            measure_idx = i
        if dim_col is None and p.get("dtype") == "categorical":
            dim_col = p
            dim_idx = i

    # --- Claim ---
    claim = description or question
    if dim_col and measure_col and sample_rows:
        try:
            # Find the top value
            best_row = None
            best_val = None
            for row in sample_rows:
                if measure_idx < len(row) and row[measure_idx] is not None:
                    try:
                        val = float(row[measure_idx])
                        if best_val is None or val > best_val:
                            best_val = val
                            best_row = row
                    except (ValueError, TypeError):
                        pass
            if best_row and dim_idx is not None and dim_idx < len(best_row):
                top_label = str(best_row[dim_idx])
                claim = f"{description}. Top: {top_label} ({measure_col['name']} = {best_val})"
        except Exception:
            pass

    # --- Confidence ---
    if row_count >= 30 and sample_size >= 30 and not is_truncated:
        confidence = "high"
    elif row_count >= 10 and sample_size >= 10:
        confidence = "medium"
    else:
        confidence = "low"

    # --- Caveats ---
    caveats = []
    if sample_size < 10:
        caveats.append(f"Small sample size (n={sample_size})")
    if row_count == 1:
        caveats.append("Single data point")
    if is_truncated and row_count > sample_size * 5:
        caveats.append(f"Heavily truncated — showing {sample_size} of {row_count} rows")
    elif is_truncated:
        caveats.append(f"Showing {sample_size} of {row_count} rows")

    # Check null rates
    for p in column_profile:
        null_rate = p.get("null_rate", 0)
        if null_rate and null_rate > 0.2:
            caveats.append(f"High null rate in {p['name']} ({null_rate:.0%})")

    # Check extreme variance
    for p in column_profile:
        if p.get("dtype") == "numeric":
            stats = p.get("stats", {})
            min_val = stats.get("min")
            max_val = stats.get("max")
            if min_val is not None and max_val is not None and min_val > 0:
                if max_val / min_val > 100:
                    caveats.append(f"Extreme variance in {p['name']} (max/min > 100x)")

    # --- Key Numbers ---
    key_numbers = []
    if dim_col and measure_col and sample_rows and dim_idx is not None and measure_idx is not None:
        scored = []
        for row in sample_rows:
            if measure_idx < len(row) and dim_idx < len(row):
                try:
                    val = float(row[measure_idx])
                    label = str(row[dim_idx])
                    scored.append((label, val))
                except (ValueError, TypeError):
                    pass
        scored.sort(key=lambda x: -x[1])
        for label, val in scored[:3]:
            key_numbers.append({
                "label": label,
                "value": val,
                "column": measure_col["name"],
            })

    return {
        "claim": claim,
        "confidence": confidence,
        "caveats": caveats,
        "key_numbers": key_numbers,
    }
