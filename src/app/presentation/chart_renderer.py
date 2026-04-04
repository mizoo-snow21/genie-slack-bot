"""Shared chart renderer: spec + DataFrame → PNG bytes.

Single rendering module used by both quick-answer and research modes.
Supports 14 chart types including heatmap, dual_axis, donut, and pie.
"""
import io
import logging
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import seaborn as sns
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# --- Styling (no font setup here — font_init.py is the single source of truth) ---
_PALETTE = "muted"
# set_style BEFORE japanize_matplotlib import so font settings are not overwritten
sns.set_style("whitegrid", {"grid.alpha": 0.3, "axes.spines.top": False, "axes.spines.right": False})
sns.set_palette(_PALETTE)
import japanize_matplotlib  # noqa: F401 — must be AFTER set_style
from presentation.font_init import register_japanese_font
register_japanese_font()  # idempotent: addfont runs once, rcParams re-applied every call

_FIG_W, _FIG_H = 10, 6
_DPI = 160


def render_to_bytes(
    df: pd.DataFrame,
    spec: dict,
    column_profile: list[dict] | None = None,
) -> Optional[bytes]:
    """Render a chart to PNG bytes from a DataFrame and spec dict.

    Args:
        df: Data to chart.
        spec: Chart specification with keys: type, x, y, y2, hue, sort, title.
        column_profile: Optional column profiles for temporal detection.

    Returns:
        PNG bytes or None if rendering fails or is skipped.
    """
    try:
        prepared = _prepare(df.copy(), spec, column_profile)
        if not prepared:
            return None

        fig = _render(prepared, spec)
        if fig is None:
            return None

        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=_DPI, bbox_inches="tight", facecolor="white")
        plt.close(fig)
        buf.seek(0)
        return buf.read()

    except Exception as e:
        logger.warning(f"Chart rendering failed: {e}")
        plt.close("all")
        return None


def _fmt_axis(ax, axis="y"):
    fmt = ticker.FuncFormatter(lambda x, _: f"{x:,.0f}")
    if axis in ("y", "both"):
        ax.yaxis.set_major_formatter(fmt)
        # Force integer ticks when all Y values are whole numbers
        ymin, ymax = ax.get_ylim()
        if ymax - ymin < 20:
            ax.yaxis.set_major_locator(ticker.MaxNLocator(integer=True))
    if axis in ("x", "both"):
        ax.xaxis.set_major_formatter(fmt)


def _prepare(df: pd.DataFrame, spec: dict, column_profile: list[dict] | None = None) -> Optional[dict]:
    """Validate columns, coerce types, detect temporal axes, auto-switch chart types."""
    chart_type = spec.get("type", "bar")
    x = spec.get("x")
    y = spec.get("y")
    y2 = spec.get("y2")
    hue = spec.get("hue")
    title = spec.get("title", "")

    # Skip if LLM decided no chart
    if chart_type in ("none", "skip"):
        return None

    # Validation: most types need both x and y, but some are special
    if not x or x not in df.columns:
        return None
    # histogram only needs x (y can be same as x or absent)
    if chart_type == "histogram":
        y = x  # histogram uses x for distribution
    elif not y or y not in df.columns:
        return None
    if hue and hue not in df.columns:
        hue = None
    if y2 and y2 not in df.columns:
        y2 = None

    # Auto-swap x/y if LLM assigned them backwards
    x_numeric_ratio = pd.to_numeric(df[x], errors="coerce").notna().mean()
    y_numeric_ratio = pd.to_numeric(df[y], errors="coerce").notna().mean()
    if chart_type in ("bar", "hbar", "pie", "donut", "boxplot", "stacked_bar", "grouped_bar"):
        if x_numeric_ratio > 0.8 and y_numeric_ratio < 0.5:
            x, y = y, x
    elif chart_type in ("line", "scatter"):
        if y_numeric_ratio < 0.5 and x_numeric_ratio > 0.5:
            x, y = y, x

    # Coerce y to numeric
    df[y] = pd.to_numeric(df[y], errors="coerce")
    if y2:
        df[y2] = pd.to_numeric(df[y2], errors="coerce")

    if chart_type == "scatter":
        df[x] = pd.to_numeric(df[x], errors="coerce")
        df = df.dropna(subset=[x, y])
    elif chart_type == "histogram":
        df[x] = pd.to_numeric(df[x], errors="coerce")
        df = df.dropna(subset=[x])
    else:
        df = df.dropna(subset=[y])

    if df.empty:
        return None

    # Sort line/area charts by x-axis
    if chart_type in ("line", "multiline", "area"):
        try:
            df[x] = pd.to_datetime(df[x])
            df = df.sort_values(x)
        except (ValueError, TypeError):
            x_as_num = pd.to_numeric(df[x], errors="coerce")
            if x_as_num.notna().mean() > 0.8:
                df = df.sort_values(x, key=lambda s: pd.to_numeric(s, errors="coerce"))
            else:
                df = df.sort_values(x)

    # Clean category column for bar-like charts
    if chart_type in ("bar", "hbar", "pie", "donut", "boxplot", "stacked_bar", "grouped_bar"):
        df[x] = df[x].astype(str).str.strip()
        df = df[df[x].notna() & (df[x] != "") & (df[x] != "nan")]

    if df.empty:
        return None

    # Pie/donut validation
    if chart_type in ("pie", "donut"):
        n_cats = df[x].nunique()
        has_neg = (df[y] < 0).any()
        if n_cats > 7 or n_cats < 2 or has_neg:
            chart_type = "bar"

    # Temporal detection: bar/hbar → line
    num_categories = df[x].nunique()
    if chart_type in ("bar", "hbar") and not hue:
        try:
            pd.to_datetime(df[x].astype(str))
            chart_type = "line"
        except (ValueError, TypeError):
            pass

    if chart_type in ("bar", "hbar") and not hue and column_profile:
        for p in column_profile:
            if p.get("name") == x and p.get("semantic_role") == "time":
                chart_type = "line"
                break

    # Auto hbar for many categories
    if chart_type == "bar":
        avg_label_len = df[x].astype(str).str.len().mean()
        if num_categories > 12 or avg_label_len > 16:
            chart_type = "hbar"

    # Validate hue cardinality
    if hue and hue in df.columns and df[hue].nunique() > 8:
        hue = None

    return {
        "df": df, "chart_type": chart_type,
        "x": x, "y": y, "y2": y2, "hue": hue,
        "title": title, "num_categories": num_categories,
    }


def _annotate(ax, df, chart_type, x_col, y_col):
    """Add annotations to highlight key data points."""
    try:
        if chart_type in ("bar", "hbar"):
            if not df.empty:
                idx = df[y_col].idxmax()
                row = df.loc[idx]
                for i, v in enumerate(df[y_col]):
                    if pd.notna(v):
                        if chart_type == "bar":
                            ax.text(i, v, f"{v:,.0f}", ha="center", va="bottom", fontsize=9, color="#333")
                        else:
                            ax.text(v, i, f"  {v:,.0f}", va="center", fontsize=9, color="#333")

        elif chart_type == "line":
            if len(df) >= 5:
                peak = df.loc[df[y_col].idxmax()]
                trough = df.loc[df[y_col].idxmin()]
                ax.annotate(f"Peak: {peak[y_col]:.1f}",
                    xy=(peak[x_col], peak[y_col]),
                    xytext=(0, 12), textcoords="offset points",
                    fontsize=8, fontweight="bold", ha="center",
                    arrowprops=dict(arrowstyle="->", color="#E15759", lw=1.2), color="#E15759")
                ax.annotate(f"Low: {trough[y_col]:.1f}",
                    xy=(trough[x_col], trough[y_col]),
                    xytext=(0, -16), textcoords="offset points",
                    fontsize=8, fontweight="bold", ha="center",
                    arrowprops=dict(arrowstyle="->", color="#4E79A7", lw=1.2), color="#4E79A7")
    except Exception:
        pass


def _render(prepared: dict, spec: dict) -> Optional[plt.Figure]:
    """Dispatch to the correct chart renderer."""
    df = prepared["df"]
    ct = prepared["chart_type"]
    x = prepared["x"]
    y = prepared["y"]
    y2 = prepared.get("y2") or spec.get("y2")
    hue = prepared["hue"]
    title = prepared["title"]
    n_cats = prepared["num_categories"]

    # Apply LLM-specified sort order for bar-like charts
    sort_order = spec.get("sort", "none")
    if sort_order and "desc" in str(sort_order).lower():
        df = df.sort_values(y, ascending=False)
    elif sort_order and "asc" in str(sort_order).lower():
        df = df.sort_values(y, ascending=True)

    fig_h = max(_FIG_H, n_cats * 0.45) if ct == "hbar" else _FIG_H
    if ct in ("donut", "pie"):
        fig, ax = plt.subplots(figsize=(7, 7))
    else:
        fig, ax = plt.subplots(figsize=(_FIG_W, fig_h))

    # --- bar ---
    if ct == "bar":
        if not hue and sort_order == "none":
            df = df.sort_values(y, ascending=False)  # default: descending
        palette = sns.color_palette("YlGnBu_r", n_colors=len(df)) if not hue else None
        sns.barplot(data=df, x=x, y=y, hue=hue, palette=palette, edgecolor="white", linewidth=0.6, ax=ax)
        _fmt_axis(ax)
        plt.xticks(rotation=45, ha="right")

    # --- hbar ---
    elif ct == "hbar":
        palette = sns.color_palette("YlGnBu_r", n_colors=len(df)) if not hue else None
        if sort_order == "none":
            df = df.sort_values(y, ascending=True)  # default: ascending (top = highest)
        sns.barplot(data=df, x=y, y=x, hue=hue, palette=palette, edgecolor="white", linewidth=0.6, orient="h", ax=ax)
        _fmt_axis(ax, axis="x")

    # --- line ---
    elif ct == "line":
        sns.lineplot(data=df, x=x, y=y, hue=hue, marker="o", markersize=6, linewidth=2.2, ax=ax)
        _fmt_axis(ax)
        plt.xticks(rotation=45, ha="right")

    # --- multiline ---
    elif ct == "multiline":
        if not hue:
            plt.close(fig)
            return None
        sns.lineplot(data=df, x=x, y=y, hue=hue, marker="o", markersize=6, linewidth=2.2, ax=ax)
        ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.13), ncol=5, frameon=False)
        _fmt_axis(ax)
        plt.xticks(rotation=45, ha="right")

    # --- area ---
    elif ct == "area":
        if hue:
            pivot = df.pivot_table(index=x, columns=hue, values=y, aggfunc="sum").fillna(0)
            pivot.plot.area(ax=ax, alpha=0.7, linewidth=1.5)
            ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.13), ncol=5, frameon=False)
        else:
            ax.fill_between(range(len(df)), df[y], alpha=0.3, color=sns.color_palette(_PALETTE)[0])
            ax.plot(range(len(df)), df[y], marker="o", markersize=5, linewidth=2, color=sns.color_palette(_PALETTE)[0])
            ax.set_xticks(range(len(df)))
            ax.set_xticklabels(df[x], rotation=45, ha="right")
        ax.set_ylabel(y)
        _fmt_axis(ax)

    # --- stacked_bar ---
    elif ct == "stacked_bar":
        if not hue:
            plt.close(fig)
            return None
        pivot = df.pivot_table(index=x, columns=hue, values=y, aggfunc="sum").fillna(0)
        pivot.plot.bar(stacked=True, ax=ax, edgecolor="white", linewidth=0.5)
        ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.13), ncol=5, frameon=False)
        _fmt_axis(ax)
        plt.xticks(rotation=45, ha="right")

    # --- grouped_bar ---
    elif ct == "grouped_bar":
        if not hue:
            plt.close(fig)
            return None
        sns.barplot(data=df, x=x, y=y, hue=hue, edgecolor="white", linewidth=0.8, ax=ax)
        ax.legend(frameon=False, fontsize=9)
        _fmt_axis(ax)
        plt.xticks(rotation=45, ha="right")

    # --- donut ---
    elif ct == "donut":
        values = df[y].tolist()
        labels = df[x].tolist()
        total = sum(v for v in values if pd.notna(v))
        colors = sns.color_palette(_PALETTE, len(labels))
        ax.pie(values, labels=labels, autopct="%1.1f%%", colors=colors,
               pctdistance=0.78,
               wedgeprops=dict(width=0.38, edgecolor="white", linewidth=2),
               textprops=dict(fontsize=11))
        ax.text(0, 0, f"{total:,.0f}", ha="center", va="center", fontsize=18, fontweight="bold", color="#333")

    # --- pie (full circle, no center text) ---
    elif ct == "pie":
        values = df[y].tolist()
        labels = df[x].tolist()
        colors = sns.color_palette(_PALETTE, len(labels))
        ax.pie(values, labels=labels, autopct="%1.1f%%", colors=colors,
               textprops=dict(fontsize=9))

    # --- scatter ---
    elif ct == "scatter":
        sns.scatterplot(data=df, x=x, y=y, hue=hue, s=100, alpha=0.75, edgecolor="white", linewidth=1, ax=ax)
        if not hue:
            try:
                z = np.polyfit(df[x].astype(float), df[y].astype(float), 1)
                p = np.poly1d(z)
                x_range = np.linspace(df[x].min(), df[x].max(), 100)
                ax.plot(x_range, p(x_range), "--", color="red", alpha=0.5, linewidth=1.5, label="Trend")
                ax.legend(fontsize=8)
            except Exception:
                pass
        _fmt_axis(ax, axis="both")

    # --- bubble (scatter + size) ---
    elif ct == "bubble":
        size_col = spec.get("y2")  # y2 is used as the size dimension
        if size_col and size_col in df.columns:
            df[size_col] = pd.to_numeric(df[size_col], errors="coerce")
            sizes = df[size_col].fillna(0)
            # Normalize sizes to a reasonable range (50-500)
            s_min, s_max = sizes.min(), sizes.max()
            if s_max > s_min:
                sizes = 50 + (sizes - s_min) / (s_max - s_min) * 450
            else:
                sizes = 200
            sns.scatterplot(data=df, x=x, y=y, hue=hue, size=sizes, sizes=(50, 500),
                            alpha=0.7, edgecolor="white", linewidth=1, ax=ax, legend="brief")
        else:
            # Fallback to regular scatter if no size column
            sns.scatterplot(data=df, x=x, y=y, hue=hue, s=100, alpha=0.75, edgecolor="white", linewidth=1, ax=ax)
        _fmt_axis(ax, axis="both")

    # --- boxplot ---
    elif ct == "boxplot":
        sns.boxplot(data=df, x=x, y=y, hue=hue, ax=ax)
        _fmt_axis(ax)
        plt.xticks(rotation=45, ha="right")

    # --- histogram ---
    elif ct == "histogram":
        sns.histplot(data=df, x=x, bins="auto", kde=True, edgecolor="white", linewidth=0.5, ax=ax)
        _fmt_axis(ax)

    # --- dual_axis ---
    elif ct == "dual_axis":
        if not y2 or y2 not in df.columns:
            plt.close(fig)
            return None
        c1 = sns.color_palette(_PALETTE)[0]
        c2 = sns.color_palette(_PALETTE)[1]
        ax.plot(range(len(df)), df[y], marker="o", color=c1, linewidth=2, label=y)
        ax.set_ylabel(y, color=c1)
        ax.tick_params(axis="y", labelcolor=c1)
        _fmt_axis(ax)
        ax2 = ax.twinx()
        ax2.plot(range(len(df)), df[y2], marker="s", color=c2, linewidth=2, label=y2)
        ax2.set_ylabel(y2, color=c2)
        ax2.tick_params(axis="y", labelcolor=c2)
        ax2.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
        ax.set_xticks(range(len(df)))
        ax.set_xticklabels(df[x], rotation=45, ha="right")
        lines1, labels1 = ax.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        ax.legend(lines1 + lines2, labels1 + labels2, loc="upper left", frameon=False)

    # --- heatmap ---
    elif ct == "heatmap":
        if hue and hue in df.columns:
            try:
                pivot = df.pivot_table(index=hue, columns=x, values=y, aggfunc="mean")
                sns.heatmap(pivot, annot=True, fmt=".1f", cmap="Blues", ax=ax,
                            linewidths=0.5, cbar_kws={"label": y})
                ax.tick_params(axis="x", rotation=45, labelsize=9)
                ax.tick_params(axis="y", labelsize=9)
            except Exception:
                sns.barplot(data=df, x=x, y=y, hue=hue, ax=ax)
        else:
            sns.barplot(data=df, x=x, y=y, ax=ax)

    else:
        plt.close(fig)
        return None

    # Annotations (bar/hbar/line only)
    _annotate(ax, df, ct, x, y)

    if title:
        ax.set_title(title, fontweight="bold", fontsize=14)

    return fig
