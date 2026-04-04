"""LLM-driven chart generation (PNG via matplotlib/seaborn).

Generates PNG chart images from step result data and saves to Unity Catalog Volume.
"""
import asyncio
import io
import json
import logging
from typing import Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns
import numpy as np
import pandas as pd

from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)

# Professional chart styling — set_style BEFORE japanize_matplotlib
# so Japanese font settings are not overwritten
_CHART_COLORS = ["#4E79A7", "#F28E2B", "#E15759", "#76B7B2", "#59A14F", "#EDC948", "#B07AA1", "#FF9DA7"]
sns.set_palette(_CHART_COLORS)
sns.set_style("whitegrid", {"grid.linestyle": "--", "grid.alpha": 0.3})
import japanize_matplotlib  # noqa: F401 — must be AFTER set_style
from presentation.font_init import register_japanese_font
register_japanese_font()


class ChartGenerator:
    """Generates PNG charts from step result data using LLM-driven spec."""

    def __init__(self, llm_client, ws: WorkspaceClient):
        self._llm = llm_client
        self._ws = ws

    async def generate(
        self,
        job_id: str,
        step_id: str,
        columns: list[dict],
        sample_rows: list[list],
        catalog: str,
        schema: str,
        column_profile: list[dict] | None = None,
        question: str | None = None,
    ) -> Optional[str]:
        """Generate a chart and save to Volume. Returns Volume path or None."""
        if not columns or not sample_rows:
            logger.info(f"Chart skipped for {job_id}/{step_id}: no data")
            return None

        # Skip chart for very small datasets — scatter/bar with 1-2 points is meaningless
        if len(sample_rows) < 3:
            logger.info(f"Chart skipped for {job_id}/{step_id}: too few rows ({len(sample_rows)})")
            return None

        try:
            col_names = [c.get("name", "") for c in columns]
            spec = await self._llm.generate_chart_spec(
                columns=columns,
                sample_rows=sample_rows[:5],
                column_profile=column_profile,
                question=question,
            )

            if not spec:
                logger.warning(f"Chart skipped for {job_id}/{step_id}: LLM returned no spec")
                return None
            logger.info(f"Chart spec for {job_id}/{step_id}: {spec}")

            # Enrich sample data with derived columns for chart diversity
            from domain.data_enricher import enrich_sample
            enriched_cols, enriched_rows = enrich_sample(
                [c for c in columns if c.get("name")],  # ensure valid columns
                sample_rows,
                column_profile or [],
            )
            enriched_col_names = [c.get("name", "") for c in enriched_cols]

            df = pd.DataFrame(enriched_rows, columns=enriched_col_names)

            png_bytes = await asyncio.to_thread(
                self._render_chart, df, spec, column_profile
            )

            if not png_bytes:
                return None

            dir_path = f"/Volumes/{catalog}/{schema}/charts/{job_id}"
            try:
                self._ws.files.create_directory(dir_path)
            except Exception:
                pass

            volume_path = f"{dir_path}/{step_id}.png"
            self._ws.files.upload(
                volume_path,
                io.BytesIO(png_bytes),
                overwrite=True,
            )

            logger.info(f"Chart saved: {volume_path}")
            return volume_path

        except Exception as e:
            logger.warning(f"Chart generation failed for {job_id}/{step_id}: {e}")
            return None

    def _prepare_chart_data(
        self, df: pd.DataFrame, spec: dict, column_profile: list[dict] | None = None,
    ) -> Optional[dict]:
        """Validate and prepare DataFrame for chart rendering.

        Returns dict with keys: df, chart_type, category_col, value_col, hue, title
        or None if data is unsuitable.
        """
        chart_type = spec.get("chart_type", "bar")
        x_col = spec.get("x_column")
        y_col = spec.get("y_column")
        title = spec.get("title", "")
        color_col = spec.get("color_column")

        if not x_col or not y_col:
            logger.warning("Chart spec missing x_column or y_column")
            return None

        # Column existence check
        if x_col not in df.columns:
            logger.warning(f"x_column '{x_col}' not in DataFrame columns: {list(df.columns)}")
            return None
        if y_col not in df.columns:
            logger.warning(f"y_column '{y_col}' not in DataFrame columns: {list(df.columns)}")
            return None

        # Determine which column is numeric vs categorical
        x_numeric_ratio = pd.to_numeric(df[x_col], errors="coerce").notna().mean()
        y_numeric_ratio = pd.to_numeric(df[y_col], errors="coerce").notna().mean()

        # For bar-like charts, ensure value_col is numeric and category_col is categorical
        if chart_type in ("bar", "hbar", "pie", "boxplot"):
            if x_numeric_ratio > 0.8 and y_numeric_ratio < 0.5:
                x_col, y_col = y_col, x_col
                logger.info(f"Swapped x/y based on type inference: x={x_col}, y={y_col}")

        # For line/scatter, ensure y is numeric
        if chart_type in ("line", "scatter"):
            if y_numeric_ratio < 0.5 and x_numeric_ratio > 0.5:
                x_col, y_col = y_col, x_col
                logger.info(f"Swapped x/y for {chart_type}: x={x_col}, y={y_col}")

        # Convert columns to appropriate types
        df[y_col] = pd.to_numeric(df[y_col], errors="coerce")
        if chart_type == "scatter":
            # Scatter needs both axes numeric
            df[x_col] = pd.to_numeric(df[x_col], errors="coerce")
            df = df.dropna(subset=[x_col, y_col])
        else:
            df = df.dropna(subset=[y_col])

        if df.empty:
            logger.warning("No valid data after numeric conversion")
            return None

        # Sort line charts by x-axis for correct rendering
        if chart_type == "line":
            # Try datetime parse first, then numeric, then string sort
            try:
                df[x_col] = pd.to_datetime(df[x_col])
                df = df.sort_values(x_col)
            except (ValueError, TypeError):
                x_as_num = pd.to_numeric(df[x_col], errors="coerce")
                if x_as_num.notna().mean() > 0.8:
                    df = df.sort_values(x_col, key=lambda s: pd.to_numeric(s, errors="coerce"))
                else:
                    df = df.sort_values(x_col)

        # Clean category column for bar-like charts
        if chart_type in ("bar", "hbar", "pie", "boxplot"):
            df[x_col] = df[x_col].astype(str).str.strip()
            df = df[df[x_col].notna() & (df[x_col] != "") & (df[x_col] != "nan")]

        if df.empty:
            logger.warning("No valid data after category cleanup")
            return None

        # Pie validation: fallback to bar if conditions aren't met
        if chart_type == "pie":
            n_cats = df[x_col].nunique()
            has_negative = (df[y_col] < 0).any()
            if n_cats > 7 or n_cats < 2 or has_negative:
                chart_type = "bar"
                logger.info(f"Pie downgraded to bar (categories={n_cats}, negative={has_negative})")

        # Detect temporal or ordinal x-axis — override to line if appropriate
        num_categories = df[x_col].nunique()
        if chart_type in ("bar", "hbar") and not color_col:
            x_sample = df[x_col].astype(str)
            try:
                pd.to_datetime(x_sample)
                chart_type = "line"
                logger.info(f"Override to line: x-axis '{x_col}' is temporal")
            except (ValueError, TypeError):
                pass

        # Also check column_profile temporal flags if available
        if chart_type in ("bar", "hbar") and not color_col and column_profile:
            for p in column_profile:
                if p.get("name") == x_col and p.get("semantic_role") == "time":
                    chart_type = "line"
                    logger.info(f"Override to line via profile: x-axis '{x_col}' has role=time")
                    break

        # Auto-switch to hbar when too many categories or long labels
        if chart_type == "bar":
            avg_label_len = df[x_col].astype(str).str.len().mean()
            if num_categories > 12 or avg_label_len > 16:
                chart_type = "hbar"
                logger.info(f"Auto-switched to hbar (categories={num_categories}, avg_label_len={avg_label_len:.0f})")

        # Validate and sanitize color column
        hue = None
        if color_col and color_col in df.columns:
            if df[color_col].nunique() <= 8:
                hue = color_col
            else:
                logger.info(f"Disabled hue '{color_col}' (too many unique values: {df[color_col].nunique()})")

        return {
            "df": df,
            "chart_type": chart_type,
            "category_col": x_col,
            "value_col": y_col,
            "hue": hue,
            "title": title,
            "num_categories": num_categories,
        }

    def _annotate_chart(self, ax, df, chart_type, cat_col, val_col):
        """Add deterministic annotations to highlight key data points."""
        try:
            if chart_type in ("bar", "hbar"):
                # Highlight the top category
                if not df.empty:
                    idx_max = df[val_col].idxmax()
                    max_row = df.loc[idx_max]
                    max_cat = str(max_row[cat_col])
                    max_val = max_row[val_col]
                    # Add a text annotation near the top of the chart
                    ax.set_title(
                        ax.get_title() + f"\n(Top: {max_cat})",
                        fontsize=11, fontstyle="italic", color="#666666"
                    )

            elif chart_type == "line":
                # Mark peak and trough if enough data points
                if len(df) >= 5:
                    idx_peak = df[val_col].idxmax()
                    idx_trough = df[val_col].idxmin()
                    peak_row = df.loc[idx_peak]
                    trough_row = df.loc[idx_trough]

                    ax.annotate(
                        f"Peak: {peak_row[val_col]:.1f}",
                        xy=(peak_row[cat_col], peak_row[val_col]),
                        xytext=(0, 12), textcoords="offset points",
                        fontsize=8, fontweight="bold", ha="center",
                        arrowprops=dict(arrowstyle="->", color="#E15759", lw=1.2),
                        color="#E15759",
                    )
                    ax.annotate(
                        f"Low: {trough_row[val_col]:.1f}",
                        xy=(trough_row[cat_col], trough_row[val_col]),
                        xytext=(0, -16), textcoords="offset points",
                        fontsize=8, fontweight="bold", ha="center",
                        arrowprops=dict(arrowstyle="->", color="#4E79A7", lw=1.2),
                        color="#4E79A7",
                    )

            elif chart_type == "scatter":
                # Mark the point with highest y
                if not df.empty:
                    idx_max = df[val_col].idxmax()
                    max_row = df.loc[idx_max]
                    ax.annotate(
                        f"Max: {max_row[val_col]:.1f}",
                        xy=(max_row[cat_col], max_row[val_col]),
                        xytext=(8, 8), textcoords="offset points",
                        fontsize=8, fontweight="bold",
                        arrowprops=dict(arrowstyle="->", color="#E15759", lw=1),
                        color="#E15759",
                    )
        except Exception as e:
            # Annotation is non-critical — log and continue
            logger.debug(f"Chart annotation skipped: {e}")

    def _render_chart(self, df: pd.DataFrame, spec: dict, column_profile: list[dict] | None = None) -> Optional[bytes]:
        """Render a chart to PNG bytes based on the LLM-generated spec."""
        try:
            prepared = self._prepare_chart_data(df.copy(), spec, column_profile)
            if not prepared:
                return None

            df = prepared["df"]
            chart_type = prepared["chart_type"]
            cat_col = prepared["category_col"]
            val_col = prepared["value_col"]
            hue = prepared["hue"]
            title = prepared["title"]
            n_cats = prepared["num_categories"]

            logger.info(f"Rendering: type={chart_type}, cat={cat_col}, val={val_col}, hue={hue}, rows={len(df)}")

            fig_height = max(6, n_cats * 0.45) if chart_type == "hbar" else 6
            fig, ax = plt.subplots(figsize=(10, fig_height))

            if chart_type == "bar":
                if not hue:
                    df = df.sort_values(val_col, ascending=False)
                sns.barplot(data=df, x=cat_col, y=val_col, hue=hue, ax=ax)
                plt.xticks(rotation=45, ha="right", fontsize=9)
            elif chart_type == "hbar":
                df_sorted = df.sort_values(val_col, ascending=True)
                sns.barplot(data=df_sorted, x=val_col, y=cat_col, hue=hue, orient="h", ax=ax)
                ax.tick_params(axis="y", labelsize=9)
            elif chart_type == "line":
                sns.lineplot(data=df, x=cat_col, y=val_col, hue=hue, marker="o", linewidth=2, ax=ax)
                plt.xticks(rotation=45, ha="right", fontsize=9)
            elif chart_type == "scatter":
                sns.scatterplot(data=df, x=cat_col, y=val_col, hue=hue, s=100, alpha=0.7, ax=ax)
                # Add trend line if no hue grouping
                if not hue:
                    try:
                        z = np.polyfit(df[cat_col].astype(float), df[val_col].astype(float), 1)
                        p = np.poly1d(z)
                        x_range = np.linspace(df[cat_col].min(), df[cat_col].max(), 100)
                        ax.plot(x_range, p(x_range), "--", color="red", alpha=0.5, linewidth=1.5, label="Trend")
                        ax.legend(fontsize=8)
                    except Exception:
                        pass
                if hue:
                    ax.legend(fontsize=8, loc="best")
            elif chart_type == "boxplot":
                sns.boxplot(data=df, x=cat_col, y=val_col, hue=hue, ax=ax)
                plt.xticks(rotation=45, ha="right", fontsize=9)
            elif chart_type == "pie":
                pie_data = df[[cat_col, val_col]].dropna()
                colors = _CHART_COLORS[:len(pie_data)]
                ax.pie(pie_data[val_col], labels=pie_data[cat_col], autopct="%1.1f%%",
                       colors=colors, textprops={"fontsize": 9})
            elif chart_type == "heatmap":
                # Pivot: x_column = columns, color_column = rows, y_column = values
                if hue and hue in df.columns:
                    try:
                        pivot = df.pivot_table(index=hue, columns=cat_col, values=val_col, aggfunc="mean")
                        sns.heatmap(pivot, annot=True, fmt=".1f", cmap="Blues", ax=ax,
                                    linewidths=0.5, cbar_kws={"label": val_col})
                        ax.tick_params(axis="x", rotation=45, labelsize=9)
                        ax.tick_params(axis="y", labelsize=9)
                    except Exception as e:
                        logger.warning(f"Heatmap pivot failed, falling back to bar: {e}")
                        sns.barplot(data=df, x=cat_col, y=val_col, hue=hue, ax=ax)
                else:
                    sns.barplot(data=df, x=cat_col, y=val_col, hue=hue, ax=ax)
            else:
                sns.barplot(data=df, x=cat_col, y=val_col, hue=hue, ax=ax)

            # Add annotations to highlight key data points
            self._annotate_chart(ax, df, chart_type, cat_col, val_col)

            ax.set_title(title, fontsize=13, fontweight="bold", pad=12)
            # Set axis labels (use spec labels or clean column names)
            x_label = spec.get("x_label", "")
            y_label = spec.get("y_label", "")
            if x_label:
                ax.set_xlabel(x_label, fontsize=10)
            if y_label:
                ax.set_ylabel(y_label, fontsize=10)
            ax.tick_params(axis="both", labelsize=9)
            plt.tight_layout()

            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
            plt.close(fig)
            buf.seek(0)
            return buf.read()

        except Exception as e:
            logger.warning(f"Chart rendering failed: {e}")
            plt.close("all")
            return None
