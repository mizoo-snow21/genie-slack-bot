"""
Chart generator using LLM for chart spec + shared renderer for rendering.
The LLM decides what chart to draw; chart_renderer handles all matplotlib/seaborn rendering.
"""
import json
import logging
from typing import List, Optional

from config import Config

import pandas as pd

logger = logging.getLogger(__name__)

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
  "title": "chart title in same language as user's question (体言止め、15字以内)"
}

Return one JSON object only. No leading/trailing text, no markdown fences. Use null (not "null" string).
Column names in x, y, y2, hue MUST exist in the provided column list."""


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

        from presentation.chart_renderer import render_to_bytes

        # Build DataFrame with proper type coercion
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

        return render_to_bytes(df, spec)
    except Exception as e:
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
            f"/serving-endpoints/{Config.LLM_CHART_ENDPOINT}/invocations",
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
