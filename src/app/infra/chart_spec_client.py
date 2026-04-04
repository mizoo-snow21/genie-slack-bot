"""Unified chart spec generation via LLM.

Single source of truth for the chart-spec prompt and the LLM call.
Returns a *canonical* spec dict with keys: type, x, y, y2, hue, sort, title.
"""
import json
import logging
import re
from typing import Optional

from databricks.sdk import WorkspaceClient

from config import Config

logger = logging.getLogger(__name__)

# Common JSON output instruction
_JSON_RULE = (
    'Return one JSON object only. No leading/trailing text, no markdown fences. '
    'Use null (not "null" string).'
)

CHART_SPEC_SYSTEM_PROMPT = """You are a data visualization expert. Choose the chart that best communicates the data's key message.

First, ask yourself: "What is the ONE thing this chart should communicate?"
Then select the chart type that makes that message IMMEDIATELY visible.

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

- **x_column**: most descriptive label column. Prefer name > category > date > id. NEVER use numeric measure columns.
- **y_column**: the primary numeric measure the user asked about. If unclear, pick the column with the largest values.
- **color_column**: only for breakdown by a 2nd category (multiline, stacked_bar, grouped_bar). null otherwise.
- **y2_column**: only for dual_axis or bubble. A second numeric column. null otherwise.
- Skip ID columns (customer_id, order_key), rank columns (rank, sales_rank), and dates that aren't time axes.

## Step 3: Pick chart type (check in order, use the FIRST match)

1. **line** — if x-axis represents time (dates, months, quarters, years) and there is ONE series. Shows trends over time.
2. **multiline** — if x-axis represents time AND a categorical column can group multiple series. Requires color_column.
3. **area** — like line but filled. Good for cumulative totals, volume over time, or emphasizing magnitude.
4. **pie** — if the data shows composition/share with 2-7 categories and values represent parts of a whole. Full circle chart.
5. **donut** — same as pie but displayed as a ring with total in center. Use when you want to emphasize the total alongside proportions.
6. **scatter** — if the QUESTION asks about the relationship between two continuous measurements AND both x and y are numeric.
7. **bubble** — like scatter but with a third numeric dimension shown as point size. Use y2_column for the size column.
8. **boxplot** — if each category has multiple raw (non-aggregated) rows and the goal is to show distribution/spread.
9. **heatmap** — if the data has TWO categorical columns and ONE numeric column, forming a matrix. Use x_column for one dimension, color_column for the other, y_column for the numeric value.
10. **stacked_bar** — if the data shows composition across multiple groups with a color breakdown. Best for parts-of-whole comparison.
11. **grouped_bar** — side-by-side bars for comparing 2-3 measures with similar scales across categories. Requires color_column.
12. **histogram** — distribution of a single numeric column. Set x_column to the numeric column.
13. **dual_axis** — two measures with different scales over the same x-axis. y_column = left axis, y2_column = right axis.
14. **bar** — if x-axis has a natural order or few categories (≤12) with short labels. Vertical bars.
15. **hbar** — for everything else: rankings, comparisons across many categories, long labels. This is the FALLBACK, not the default.

IMPORTANT: Do NOT default to hbar. Actively look for reasons to use line, multiline, area, pie, donut, scatter, bubble, heatmap, bar, or other types first. Use hbar only when no other type fits better.

Rules:
- x_column and y_column MUST be column names that exist in the provided column list
- x_column = categorical or first variable, y_column = numeric measure
- color_column is optional — use only when ≤ 8 distinct values AND chart_type supports grouping (bar, hbar, line, multiline, scatter, grouped_bar, stacked_bar)
- Title MUST be in the SAME LANGUAGE as the user's question (not the column names)
- Title must reflect the SPECIFIC analytical angle of the question — include the grouping/color axis, not just the y-axis measure
- If a color_column or grouping axis is present, mention it in the title (e.g., "カテゴリ別の地域別売上" not just "地域別売上")
- If you cannot produce a natural title, return empty string
- sort: "desc" (highest first), "asc" (lowest first, or natural order like age bands/time), or "none" (keep original order). Choose the sort that makes the chart's message clearest.

{json_rule}
Examples:
{{"chart_type": "line", "x_column": "month", "y_column": "avg_value", "title": "月別平均値推移", "color_column": null, "sort": "none"}}
{{"chart_type": "multiline", "x_column": "month", "y_column": "value", "title": "月別カテゴリ別推移", "color_column": "category", "sort": "none"}}
{{"chart_type": "area", "x_column": "month", "y_column": "cumulative", "title": "累計推移", "color_column": null, "sort": "none"}}
{{"chart_type": "pie", "x_column": "segment", "y_column": "share", "title": "セグメント構成比", "color_column": null, "sort": "desc"}}
{{"chart_type": "donut", "x_column": "segment", "y_column": "share", "title": "セグメント構成比", "color_column": null, "sort": "desc"}}
{{"chart_type": "scatter", "x_column": "metric_a", "y_column": "metric_b", "title": "指標Aと指標Bの関係", "color_column": "category", "sort": "none"}}
{{"chart_type": "bubble", "x_column": "metric_a", "y_column": "metric_b", "title": "3指標の関係", "color_column": "category", "y2_column": "metric_c", "sort": "none"}}
{{"chart_type": "boxplot", "x_column": "category", "y_column": "value", "title": "カテゴリ別値分布", "color_column": null, "sort": "none"}}
{{"chart_type": "heatmap", "x_column": "sub_category", "y_column": "avg_value", "title": "カテゴリ×サブカテゴリ別平均値", "color_column": "category", "sort": "none"}}
{{"chart_type": "stacked_bar", "x_column": "group", "y_column": "count", "title": "グループ別構成比", "color_column": "type", "sort": "none"}}
{{"chart_type": "grouped_bar", "x_column": "category", "y_column": "value", "title": "カテゴリ別比較", "color_column": "group", "sort": "none"}}
{{"chart_type": "histogram", "x_column": "amount", "y_column": "amount", "title": "金額分布", "color_column": null, "sort": "none"}}
{{"chart_type": "dual_axis", "x_column": "month", "y_column": "count", "title": "件数と平均値の推移", "color_column": null, "y2_column": "avg_value", "sort": "none"}}
{{"chart_type": "bar", "x_column": "age_band", "y_column": "count", "title": "年齢帯別件数", "color_column": "type", "sort": "asc"}}
{{"chart_type": "hbar", "x_column": "group", "y_column": "revenue", "title": "グループ別売上", "color_column": null, "sort": "desc"}}

Only skip if no numeric column: {{"skip": true}}"""


def _extract_json(text: str) -> Optional[str]:
    """Try to extract a JSON object from text that may contain prose."""
    match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text)
    return match.group(0) if match else None


def _normalize_spec(raw: dict) -> dict:
    """Normalize LLM response fields to canonical spec keys.

    Accepts either research-style (chart_type, x_column, …) or
    quick-style (type, x, …) field names and returns a canonical dict.
    """
    return {
        "type": raw.get("chart_type") or raw.get("type", "bar"),
        "x": raw.get("x_column") or raw.get("x"),
        "y": raw.get("y_column") or raw.get("y"),
        "y2": raw.get("y2_column") or raw.get("y2"),
        "hue": raw.get("color_column") or raw.get("hue"),
        "sort": raw.get("sort", "none"),
        "title": raw.get("title", ""),
    }


def _build_user_message(
    columns: list[dict],
    sample_rows: list[list],
    column_profile: list[dict] | None = None,
    question: str | None = None,
) -> str:
    """Build the user message for the chart spec LLM call."""
    col_desc = json.dumps(columns, ensure_ascii=False)
    rows_desc = json.dumps(sample_rows[:5], ensure_ascii=False)
    parts: list[str] = []
    if question:
        parts.append(f"Question: {question}")
    parts.append(f"Columns: {col_desc}")
    parts.append(f"Sample rows (first 5): {rows_desc}")
    if column_profile:
        parts.append(f"Column profile (sample-based): {json.dumps(column_profile, ensure_ascii=False)}")
    return "\n".join(parts)


def get_chart_spec(
    ws: WorkspaceClient,
    columns: list[dict],
    sample_rows: list[list],
    *,
    column_profile: list[dict] | None = None,
    question: str | None = None,
) -> Optional[dict]:
    """Call LLM and return a canonical chart spec dict, or None.

    This is a **synchronous** function. Callers needing async should wrap
    with ``asyncio.to_thread``.
    """
    prompt = CHART_SPEC_SYSTEM_PROMPT.format(json_rule=_JSON_RULE)
    user_msg = _build_user_message(columns, sample_rows, column_profile, question)

    try:
        resp = ws.api_client.do(
            "POST",
            f"/serving-endpoints/{Config.LLM_CHART_ENDPOINT}/invocations",
            body={
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user_msg},
                ],
                "max_tokens": 200,
                "temperature": 0,
            },
        )
    except Exception as e:
        logger.error(f"Chart spec LLM call failed: {e}")
        return None

    try:
        text = resp["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as e:
        logger.error(f"Unexpected chart spec response format: {e}")
        return None

    # Strip markdown code fences
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    # Parse JSON
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        extracted = _extract_json(text)
        if extracted:
            try:
                raw = json.loads(extracted)
            except json.JSONDecodeError:
                logger.error(f"Failed to parse chart spec JSON: {text[:200]}")
                return None
        else:
            logger.error(f"Failed to parse chart spec JSON: {text[:200]}")
            return None

    if raw.get("skip"):
        return None

    return _normalize_spec(raw)
