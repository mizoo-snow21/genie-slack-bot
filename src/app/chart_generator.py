"""
Chart generator using Plotly for rich visualization of query results.
Automatically detects the best chart type based on data shape.
"""
import logging
from typing import List, Optional, Tuple

import plotly.graph_objects as go

logger = logging.getLogger(__name__)

# Color palette (Set2-inspired, no px dependency)
_COLORS = ["#66c2a5", "#fc8d62", "#8da0cb", "#e78ac3", "#a6d854", "#ffd92f", "#e5c494", "#b3b3b3"]

LAYOUT_DEFAULTS = dict(
    template="plotly_white",
    font=dict(family="Inter, Helvetica, Arial, sans-serif", size=13),
    margin=dict(l=60, r=30, t=50, b=60),
    width=800,
    height=480,
    colorway=_COLORS,
)


def generate_chart(
    column_names: List[str],
    data_array: List[list],
    column_types: List[dict],
) -> Optional[bytes]:
    """
    Generate a chart image (PNG bytes) from query result data.

    Returns None if data is not suitable for charting (e.g. single value, too many text columns).
    """
    if not data_array or not column_names:
        return None

    num_cols, cat_cols, date_cols = _classify_columns(column_names, data_array, column_types)

    # Need at least 1 numeric column and 2+ rows to chart
    if not num_cols or len(data_array) < 2:
        return None

    try:
        fig = _pick_and_build(column_names, data_array, num_cols, cat_cols, date_cols)
        if fig is None:
            return None

        fig.update_layout(**LAYOUT_DEFAULTS)
        return fig.to_image(format="png", scale=2)
    except Exception as e:
        logger.error(f"Chart generation failed: {e}", exc_info=True)
        return None


def _classify_columns(
    column_names: List[str],
    data_array: List[list],
    column_types: List[dict],
) -> Tuple[List[int], List[int], List[int]]:
    """Classify column indices into numeric, categorical, and date types."""
    num_cols = []
    cat_cols = []
    date_cols = []

    numeric_type_names = {"INT", "LONG", "FLOAT", "DOUBLE", "DECIMAL", "SHORT", "BYTE", "BIGINT", "SMALLINT", "TINYINT"}
    date_type_names = {"DATE", "TIMESTAMP", "TIMESTAMP_NTZ"}

    for i, col in enumerate(column_types):
        type_name = col.get("type_name", "").upper()
        if type_name in numeric_type_names:
            num_cols.append(i)
        elif type_name in date_type_names:
            date_cols.append(i)
        else:
            # Fallback: try to parse as number from actual data
            if _column_is_numeric(data_array, i):
                num_cols.append(i)
            else:
                cat_cols.append(i)

    return num_cols, cat_cols, date_cols


def _column_is_numeric(data_array: List[list], col_idx: int) -> bool:
    """Check if a column's values are all numeric."""
    count = 0
    for row in data_array:
        val = row[col_idx]
        if val is None:
            continue
        try:
            float(str(val))
            count += 1
        except (ValueError, TypeError):
            return False
    return count > 0


def _get_col_values(data_array: List[list], col_idx: int, as_float: bool = False) -> list:
    """Extract column values from data array."""
    vals = []
    for row in data_array:
        v = row[col_idx]
        if as_float and v is not None:
            try:
                v = float(str(v))
            except (ValueError, TypeError):
                pass
        vals.append(v)
    return vals


def _pick_and_build(
    column_names: List[str],
    data_array: List[list],
    num_cols: List[int],
    cat_cols: List[int],
    date_cols: List[int],
) -> Optional[go.Figure]:
    """Pick chart type and build the figure."""
    n_rows = len(data_array)

    # --- Time series: date column + numeric column(s) → line chart ---
    if date_cols and num_cols:
        date_idx = date_cols[0]
        x = _get_col_values(data_array, date_idx)
        fig = go.Figure()
        for ni in num_cols[:5]:  # max 5 series
            y = _get_col_values(data_array, ni, as_float=True)
            fig.add_trace(go.Scatter(
                x=x, y=y,
                mode="lines+markers",
                name=column_names[ni],
                line=dict(width=2.5),
                marker=dict(size=6),
            ))
        fig.update_layout(
            xaxis_title=column_names[date_idx],
            yaxis_title="Value" if len(num_cols) > 1 else column_names[num_cols[0]],
        )
        return fig

    # --- 1 categorical + 1 numeric → bar chart ---
    if len(cat_cols) >= 1 and len(num_cols) >= 1:
        cat_idx = cat_cols[0]
        num_idx = num_cols[0]
        labels = _get_col_values(data_array, cat_idx)
        values = _get_col_values(data_array, num_idx, as_float=True)

        # Pie chart if few categories and looks like proportions
        if 2 <= n_rows <= 8 and all(v is not None and v >= 0 for v in values):
            total = sum(v for v in values if v)
            if total > 0:
                fig = go.Figure(go.Pie(
                    labels=labels,
                    values=values,
                    textinfo="label+percent",
                    textposition="outside",
                    hole=0.35,
                ))
                fig.update_layout(
                    title=dict(text=column_names[num_idx], x=0.5),
                    showlegend=False,
                )
                return fig

        # Horizontal bar for long labels, vertical otherwise
        max_label_len = max((len(str(l)) for l in labels), default=0)
        if max_label_len > 15 or n_rows > 12:
            fig = go.Figure(go.Bar(
                x=values, y=labels,
                orientation="h",
                marker=dict(
                    color=values,
                    colorscale="Tealgrn",
                    line=dict(width=0.5, color="white"),
                ),
            ))
            fig.update_layout(
                xaxis_title=column_names[num_idx],
                yaxis=dict(autorange="reversed"),
                height=max(480, n_rows * 32),
            )
        else:
            fig = go.Figure(go.Bar(
                x=labels, y=values,
                marker=dict(
                    color=values,
                    colorscale="Tealgrn",
                    line=dict(width=0.5, color="white"),
                ),
                text=[f"{v:,.0f}" if isinstance(v, (int, float)) else str(v) for v in values],
                textposition="outside",
            ))
            fig.update_layout(
                xaxis_title=column_names[cat_idx],
                yaxis_title=column_names[num_idx],
            )
        return fig

    # --- Multiple numeric columns, no categorical → grouped bar or scatter ---
    if len(num_cols) >= 2 and not cat_cols:
        x_idx = num_cols[0]
        y_idx = num_cols[1]
        x = _get_col_values(data_array, x_idx, as_float=True)
        y = _get_col_values(data_array, y_idx, as_float=True)
        fig = go.Figure(go.Scatter(
            x=x, y=y,
            mode="markers",
            marker=dict(size=10, opacity=0.7, line=dict(width=1, color="white")),
        ))
        fig.update_layout(
            xaxis_title=column_names[x_idx],
            yaxis_title=column_names[y_idx],
        )
        return fig

    return None
