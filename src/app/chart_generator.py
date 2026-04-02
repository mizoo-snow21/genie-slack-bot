"""
Chart generator using LLM for chart spec + matplotlib/seaborn for rendering.
The LLM decides what chart to draw; matplotlib renders it with japanize-matplotlib for Japanese.
"""
import io
import json
import logging
from typing import List, Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import japanize_matplotlib  # noqa: F401
import seaborn as sns
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

_PALETTE = "muted"
sns.set_theme(
    style="whitegrid",
    palette=_PALETTE,
    font="IPAexGothic",
    rc={
        "axes.titlesize": 14,
        "axes.titlepad": 14,
        "axes.labelsize": 11,
        "xtick.labelsize": 10,
        "ytick.labelsize": 10,
        "legend.fontsize": 9,
        "grid.alpha": 0.3,
        "axes.spines.top": False,
        "axes.spines.right": False,
    },
)

_FIG_W, _FIG_H = 10, 6
_DPI = 160

_CHART_SPEC_PROMPT = """You are a data visualization expert. Given column info, sample data, and the user's original question, decide the best chart type and return a JSON spec.

## Step 1: Understand user intent from question keywords

| Intent | Keywords | Best chart |
|--------|----------|------------|
| Ranking / Top-N | トップ, 最も, ランキング, 上位, 下位, best, worst, top, bottom | bar (<=10) or hbar (>10) |
| Comparison | 比較, 対比, vs, 差, 違い | grouped_bar or bar |
| Trend over time | 推移, 月別, 年次, 日別, 変化, trend, over time | line or area |
| Trend by group | 推移+地域別, カテゴリ別推移 | multiline |
| Composition / Share | シェア, 割合, 構成, 内訳, 比率, proportion, share, percentage | donut (ALWAYS use donut for share/proportion data, even with 10+ items — show top items) |
| Distribution | 分布, ヒストグラム, ばらつき, distribution | histogram |
| Correlation | 相関, 関係, correlation, relationship | scatter |
| Cumulative / Area | 累計, 累積, 積み上げ, cumulative | area or stacked_bar |

## Step 2: Choose columns

- **x**: most descriptive label column. Prefer name > category > date > id. NEVER use numeric measure columns.
- **y**: the primary numeric measure the user asked about. If unclear, pick the column with the largest values.
- **hue**: only for breakdown by a 2nd category (multiline, stacked_bar, grouped_bar). null otherwise.
- **y2**: only for dual_axis. A second numeric column to plot on right y-axis. null otherwise.
- Skip ID columns (customer_id, order_key), rank columns (rank, sales_rank), and dates that aren't time axes.

## Step 3: Pick chart type

| Type | When to use |
|------|-------------|
| bar | Category vs numeric. Ranking, comparison. <=10 items. |
| hbar | Same as bar, but >10 items or long labels (Japanese labels > 4 chars). Sort desc for ranking. |
| line | Single numeric over time (date x-axis). |
| multiline | Multiple series over time, grouped by a category (hue required). |
| area | Like line but filled. Good for cumulative, volume, or emphasizing magnitude over time. |
| stacked_bar | Parts-of-whole comparison across categories. hue = sub-category, y = values. |
| grouped_bar | Side-by-side bars. Only when comparing 2-3 measures with SIMILAR scales. hue required. |
| donut | Proportion of a whole. Use when user asks about share/シェア/割合/比率. PRIORITIZE donut over bar for share data. 3-15 items OK. |
| scatter | Relationship between two numeric variables. x and y are both numeric. |
| histogram | Distribution of a single numeric column. x = the numeric column, y = same column. |
| dual_axis | Two measures with different scales over the same x-axis. y = left axis, y2 = right axis. |
| none | Data not suitable for charting (single row, all text, no numeric). |

## Output format

Return ONLY valid JSON, no markdown, no explanation:
{
  "type": "bar|hbar|line|multiline|area|stacked_bar|grouped_bar|donut|scatter|histogram|dual_axis|none",
  "x": "column_name",
  "y": "column_name",
  "y2": "column_name or null",
  "hue": "column_name or null",
  "sort": "desc" or "asc" or "none" (ONLY these 3 values),
  "title": "chart title in same language as data (体言止め、15字以内)"
}"""


def generate_chart(
    column_names: List[str],
    data_array: List[list],
    column_types: List[dict],
    title: Optional[str] = None,
    llm_client=None,
    user_question: Optional[str] = None,
) -> Optional[bytes]:
    if not data_array or not column_names or len(data_array) < 2:
        return None
    if llm_client is None:
        return None

    try:
        spec = _get_chart_spec(llm_client, column_names, data_array, column_types, user_question)
        if not spec or spec.get("type") == "none":
            logger.info(f"LLM decided no chart: {spec}")
            return None

        logger.info(f"Chart spec from LLM: {spec}")

        df = _to_df(column_names, data_array, column_types)
        fig = _render(df, spec)
        if fig is None:
            return None

        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=_DPI, bbox_inches="tight", facecolor="white")
        plt.close(fig)
        buf.seek(0)
        return buf.read()
    except Exception as e:
        plt.close("all")
        logger.error(f"Chart generation failed: {e}", exc_info=True)
        return None


def _get_chart_spec(llm_client, column_names, data_array, column_types, user_question=None) -> Optional[dict]:
    col_info = [f"{n} ({ct.get('type_name', '?')})" for n, ct in zip(column_names, column_types)]
    sample = data_array[:5]

    question_line = f"User's question: {user_question}\n" if user_question else ""
    user_msg = f"""{question_line}Columns: {', '.join(col_info)}
Sample data ({len(data_array)} rows total, showing first {len(sample)}):
{json.dumps(sample, ensure_ascii=False)}"""

    try:
        response = llm_client.api_client.do(
            "POST",
            "/serving-endpoints/databricks-gpt-5-4-nano/invocations",
            body={
                "messages": [
                    {"role": "system", "content": _CHART_SPEC_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                "max_tokens": 200,
                "temperature": 0,
            },
        )
        text = response["choices"][0]["message"]["content"].strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
        return json.loads(text)
    except Exception as e:
        logger.error(f"LLM chart spec failed: {e}")
        return None


def _to_df(column_names, data_array, column_types):
    numeric_types = {"INT", "LONG", "FLOAT", "DOUBLE", "DECIMAL", "SHORT", "BYTE", "BIGINT", "SMALLINT", "TINYINT"}
    date_types = {"DATE", "TIMESTAMP", "TIMESTAMP_NTZ"}
    df = pd.DataFrame(data_array, columns=column_names)
    for i, ct in enumerate(column_types):
        tn = ct.get("type_name", "").upper()
        col = column_names[i]
        if tn in numeric_types:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        elif tn in date_types:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    return df


def _fmt_axis(ax, axis="y"):
    fmt = ticker.FuncFormatter(lambda x, _: f"{x:,.0f}")
    if axis in ("y", "both"):
        ax.yaxis.set_major_formatter(fmt)
    if axis in ("x", "both"):
        ax.xaxis.set_major_formatter(fmt)


def _render(df: pd.DataFrame, spec: dict) -> Optional[plt.Figure]:
    chart_type = spec.get("type", "none")
    x = spec.get("x")
    y = spec.get("y")
    y2 = spec.get("y2")
    hue = spec.get("hue")
    sort_order = spec.get("sort", "none")
    title = spec.get("title")

    if not x or not y or x not in df.columns or y not in df.columns:
        return None
    if hue and hue not in df.columns:
        hue = None
    if y2 and y2 not in df.columns:
        y2 = None

    # Normalize sort value (LLM sometimes returns "column_desc" instead of "desc")
    if sort_order and "desc" in str(sort_order).lower():
        df = df.sort_values(y, ascending=False)
    elif sort_order and "asc" in str(sort_order).lower():
        df = df.sort_values(y, ascending=True)

    fig, ax = plt.subplots(figsize=(_FIG_W, _FIG_H))

    # --- bar ---
    if chart_type == "bar":
        palette = sns.color_palette("YlGnBu_r", n_colors=len(df))
        sns.barplot(data=df, x=x, y=y, palette=palette, edgecolor="white", linewidth=0.6, ax=ax)
        for i, v in enumerate(df[y]):
            if pd.notna(v):
                ax.text(i, v, f"{v:,.0f}", ha="center", va="bottom", fontsize=9, color="#333")
        _fmt_axis(ax)
        plt.xticks(rotation=45, ha="right")

    # --- hbar ---
    elif chart_type == "hbar":
        plt.close(fig)
        fig, ax = plt.subplots(figsize=(_FIG_W, max(6, len(df) * 0.45)))
        palette = sns.color_palette("YlGnBu_r", n_colors=len(df))
        sns.barplot(data=df, x=y, y=x, palette=palette, edgecolor="white", linewidth=0.6, orient="h", ax=ax)
        for i, v in enumerate(df[y]):
            if pd.notna(v):
                ax.text(v, i, f"  {v:,.0f}", va="center", fontsize=9, color="#333")
        _fmt_axis(ax, axis="x")

    # --- line ---
    elif chart_type == "line":
        sns.lineplot(data=df, x=x, y=y, marker="o", markersize=6, linewidth=2.2, ax=ax)
        _fmt_axis(ax)
        plt.xticks(rotation=45, ha="right")

    # --- multiline ---
    elif chart_type == "multiline":
        if not hue:
            plt.close(fig)
            return None
        sns.lineplot(data=df, x=x, y=y, hue=hue, marker="o", markersize=6, linewidth=2.2, ax=ax)
        ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.13), ncol=5, frameon=False)
        _fmt_axis(ax)
        plt.xticks(rotation=45, ha="right")

    # --- area ---
    elif chart_type == "area":
        if hue:
            # Stacked area by category
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
    elif chart_type == "stacked_bar":
        if not hue:
            plt.close(fig)
            return None
        pivot = df.pivot_table(index=x, columns=hue, values=y, aggfunc="sum").fillna(0)
        pivot.plot.bar(stacked=True, ax=ax, edgecolor="white", linewidth=0.5)
        ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.13), ncol=5, frameon=False)
        _fmt_axis(ax)
        plt.xticks(rotation=45, ha="right")

    # --- grouped_bar ---
    elif chart_type == "grouped_bar":
        if not hue:
            plt.close(fig)
            return None
        sns.barplot(data=df, x=x, y=y, hue=hue, edgecolor="white", linewidth=0.8, ax=ax)
        ax.legend(frameon=False, fontsize=9)
        _fmt_axis(ax)
        plt.xticks(rotation=45, ha="right")

    # --- donut ---
    elif chart_type == "donut":
        plt.close(fig)
        fig, ax = plt.subplots(figsize=(7, 7))
        values = df[y].tolist()
        labels = df[x].tolist()
        total = sum(v for v in values if pd.notna(v))
        colors = sns.color_palette(_PALETTE, len(labels))
        ax.pie(
            values, labels=labels, autopct="%1.1f%%", colors=colors,
            pctdistance=0.78,
            wedgeprops=dict(width=0.38, edgecolor="white", linewidth=2),
            textprops=dict(fontsize=11),
        )
        ax.text(0, 0, f"{total:,.0f}", ha="center", va="center", fontsize=18, fontweight="bold", color="#333")

    # --- scatter ---
    elif chart_type == "scatter":
        if hue:
            sns.scatterplot(data=df, x=x, y=y, hue=hue, s=100, alpha=0.75, edgecolor="white", linewidth=1, ax=ax)
        else:
            sns.scatterplot(data=df, x=x, y=y, s=100, alpha=0.75, edgecolor="white", linewidth=1, ax=ax)
        _fmt_axis(ax, axis="both")

    # --- histogram ---
    elif chart_type == "histogram":
        sns.histplot(data=df, x=x, bins="auto", kde=True, edgecolor="white", linewidth=0.5, ax=ax)
        _fmt_axis(ax)

    # --- dual_axis ---
    elif chart_type == "dual_axis":
        if not y2:
            plt.close(fig)
            return None
        color1 = sns.color_palette(_PALETTE)[0]
        color2 = sns.color_palette(_PALETTE)[1]

        ax.plot(range(len(df)), df[y], marker="o", color=color1, linewidth=2, label=y)
        ax.set_ylabel(y, color=color1)
        ax.tick_params(axis="y", labelcolor=color1)
        _fmt_axis(ax)

        ax2 = ax.twinx()
        ax2.plot(range(len(df)), df[y2], marker="s", color=color2, linewidth=2, label=y2)
        ax2.set_ylabel(y2, color=color2)
        ax2.tick_params(axis="y", labelcolor=color2)
        ax2.yaxis.set_major_formatter(ticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))

        ax.set_xticks(range(len(df)))
        ax.set_xticklabels(df[x], rotation=45, ha="right")

        lines1, labels1 = ax.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        ax.legend(lines1 + lines2, labels1 + labels2, loc="upper left", frameon=False)

    else:
        plt.close(fig)
        return None

    if title:
        ax.set_title(title, fontweight="bold", fontsize=14)

    return fig
