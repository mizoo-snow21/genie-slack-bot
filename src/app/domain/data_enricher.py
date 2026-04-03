"""Compute derived columns from Genie query results for chart diversity.

Used internally by chart_generator only — not stored in Delta, not passed to LLMs.
Adds rank, share, and delta-from-mean columns for the primary numeric measure.
"""


def enrich_sample(
    columns: list[dict],
    sample_rows: list[list],
    column_profile: list[dict],
) -> tuple[list[dict], list[list]]:
    """Add derived columns to sample data for chart generation.

    Args:
        columns: Column metadata from Genie.
        sample_rows: Raw data rows.
        column_profile: Column profiles with semantic_role.

    Returns:
        Tuple of (enriched_columns, enriched_rows).
        Original columns/rows are preserved; new columns are appended.
    """
    if not columns or not sample_rows or not column_profile:
        return columns, sample_rows

    # Find primary measure column (first column with semantic_role == "measure" or dtype == "numeric")
    measure_idx = None
    measure_name = None
    for i, p in enumerate(column_profile):
        if p.get("semantic_role") == "measure" or p.get("dtype") == "numeric":
            measure_idx = i
            measure_name = p["name"]
            break

    if measure_idx is None:
        return columns, sample_rows

    # Extract numeric values for the measure column
    values = []
    for row in sample_rows:
        if measure_idx < len(row) and row[measure_idx] is not None:
            try:
                values.append(float(row[measure_idx]))
            except (ValueError, TypeError):
                values.append(None)
        else:
            values.append(None)

    # Check if we can compute share (no negative values)
    non_null_values = [v for v in values if v is not None]
    if not non_null_values:
        return columns, sample_rows

    has_negative = any(v < 0 for v in non_null_values)
    total = sum(non_null_values)
    mean = sum(non_null_values) / len(non_null_values)

    # Compute ranks (descending, 1 = highest)
    sorted_vals = sorted(
        [(i, v) for i, v in enumerate(values) if v is not None],
        key=lambda x: -x[1]
    )
    ranks = {}
    for rank, (idx, _) in enumerate(sorted_vals, 1):
        ranks[idx] = rank

    # Build enriched columns
    new_columns = [{"name": f"{measure_name}_rank", "type_name": "INT"}]
    if not has_negative and total > 0:
        new_columns.append({"name": f"{measure_name}_share", "type_name": "DOUBLE"})
    new_columns.append({"name": f"{measure_name}_delta_mean", "type_name": "DOUBLE"})

    enriched_columns = list(columns) + new_columns

    # Build enriched rows
    enriched_rows = []
    for i, row in enumerate(sample_rows):
        new_cells = []
        # rank
        new_cells.append(str(ranks[i]) if i in ranks else None)
        # share (only if no negatives)
        if not has_negative and total > 0:
            if values[i] is not None:
                new_cells.append(f"{values[i] / total:.4f}")
            else:
                new_cells.append(None)
        # delta_mean
        if values[i] is not None:
            new_cells.append(f"{values[i] - mean:.4f}")
        else:
            new_cells.append(None)

        enriched_rows.append(list(row) + new_cells)

    return enriched_columns, enriched_rows
