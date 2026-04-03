"""Two-phase report renderer.

Phase A: Build structured evidence JSON from step artifacts (deterministic).
Phase B (external): LLM generates narrative with [Step N] markers.
Phase C: Merge — inject actual data tables at markers.
"""
import json
import re
from typing import Optional

from domain.finding_card import build_finding_card


class ReportRenderer:
    """Renders final Markdown report from LLM narrative + step evidence."""

    @staticmethod
    def build_evidence(steps: list[dict]) -> list[dict]:
        """Phase A: Build structured evidence from step data.

        Args:
            steps: List of step dicts from Delta (with JSON string fields).

        Returns:
            List of evidence dicts with parsed data.
        """
        evidence = []
        for i, step in enumerate(steps):
            columns_raw = step.get("result_columns", "[]")
            columns = json.loads(columns_raw) if isinstance(columns_raw, str) else columns_raw
            col_names = [c.get("name", f"col_{j}") for j, c in enumerate(columns)]

            sample_raw = step.get("result_sample", "[]")
            table_rows = json.loads(sample_raw) if isinstance(sample_raw, str) else sample_raw

            profile_raw = step.get("column_profile")
            if profile_raw and isinstance(profile_raw, str):
                try:
                    column_profile = json.loads(profile_raw)
                except (json.JSONDecodeError, TypeError):
                    column_profile = []
            elif isinstance(profile_raw, list):
                column_profile = profile_raw
            else:
                column_profile = []

            evidence.append({
                "step_id": step["step_id"],
                "step_number": i + 1,
                "question": step.get("question", ""),
                "columns": col_names,
                "table_rows": table_rows,
                "row_count": step.get("result_row_count", len(table_rows)),
                "is_truncated": step.get("result_is_truncated", False),
                "sql": step.get("sql_query", ""),
                "chart_volume_path": step.get("chart_volume_path"),
                "column_profile": column_profile,
            })
        return evidence

    @staticmethod
    def merge(narrative: str, evidence: list[dict], job_id: str = "", for_pdf: bool = False) -> str:
        """Phase C: Inject data tables into LLM narrative at [Step N] markers.

        Each [Step N] marker is replaced with:
        - A Markdown table of the step's data
        - A truncation note if applicable
        - The SQL query in a collapsible details block (unless for_pdf=True)
        - A chart image reference if a chart was generated for this step

        Args:
            for_pdf: If True, place chart above table and omit SQL blocks.
        """
        if not evidence:
            return narrative

        for ev in evidence:
            # LLM may use [Step 1], [Step s1], or [Step S1] — try all variants
            marker = None
            for candidate in [
                f"[Step {ev['step_number']}]",
                f"[Step {ev['step_id']}]",
                f"[Step {ev['step_id'].upper()}]",
            ]:
                if candidate in narrative:
                    marker = candidate
                    break
            if not marker:
                continue

            table_md = ReportRenderer._render_table(ev["columns"], ev["table_rows"])

            truncation_note = ""
            if ev["is_truncated"]:
                truncation_note = f"\n\n*Showing first {len(ev['table_rows'])} of {ev['row_count']} rows*"

            # Build finding card for caveat display
            caveat_note = ""
            try:
                fc = build_finding_card(
                    question=ev.get("question", ""),
                    description="",
                    column_profile=ev.get("column_profile", []),
                    sample_rows=ev.get("table_rows", []),
                    row_count=ev.get("row_count", 0),
                    sample_size=len(ev.get("table_rows", [])),
                    is_truncated=ev.get("is_truncated", False),
                )
                if fc.get("caveats"):
                    caveat_text = "; ".join(fc["caveats"])
                    caveat_note = f"\n\n*Note: {caveat_text}*"
            except Exception:
                pass

            chart_block = ""
            if ev.get("chart_volume_path") and job_id:
                step_id = ev["step_id"]
                chart_block = f"\n\n![Step {ev['step_number']} Chart](/research/{job_id}/steps/{step_id}/chart)"

            if for_pdf:
                # PDF: chart above table, no SQL, no raw question text
                replacement = f"\n{chart_block}\n\n{table_md}{truncation_note}{caveat_note}\n"
            else:
                # Markdown/GDocs: table first, chart after, SQL in details
                sql_block = ""
                if ev["sql"]:
                    sql_block = f"\n\n<details><summary>SQL</summary>\n\n```sql\n{ev['sql']}\n```\n\n</details>"
                replacement = f"\n\n{table_md}{truncation_note}{caveat_note}{chart_block}{sql_block}\n"

            # Replace only the first occurrence with full content.
            # Any subsequent references to the same step get the marker removed
            # to avoid duplicate table insertion.
            narrative = narrative.replace(marker, replacement, 1)
            narrative = narrative.replace(marker, f"(Step {ev['step_number']})")

        return narrative

    @staticmethod
    def _render_table(columns: list[str], rows: list[list]) -> str:
        """Render a Markdown table from columns and rows."""
        if not columns or not rows:
            return "_No data_"

        header = "| " + " | ".join(columns) + " |"
        separator = "| " + " | ".join("---" for _ in columns) + " |"
        body_lines = []
        for row in rows:
            cells = [str(cell) if cell is not None else "-" for cell in row]
            # Pad or truncate to match column count
            while len(cells) < len(columns):
                cells.append("-")
            body_lines.append("| " + " | ".join(cells[:len(columns)]) + " |")

        return "\n".join([header, separator] + body_lines)
