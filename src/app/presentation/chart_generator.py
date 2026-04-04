"""Unified chart generation (PNG via matplotlib/seaborn).

Single entry point for both quick-answer and research modes.
Delegates spec generation to ``infra.chart_spec_client`` (via LLMClient)
and rendering to ``presentation.chart_renderer``.
"""
import asyncio
import io
import logging
from typing import Optional

import pandas as pd

from databricks.sdk import WorkspaceClient
from config import Config

logger = logging.getLogger(__name__)

# Type-name sets used for DataFrame coercion
_NUMERIC_TYPES = frozenset({
    "INT", "LONG", "FLOAT", "DOUBLE", "DECIMAL",
    "SHORT", "BYTE", "BIGINT", "SMALLINT", "TINYINT",
})
_DATE_TYPES = frozenset({"DATE", "TIMESTAMP", "TIMESTAMP_NTZ"})


def _build_df(
    column_names: list[str],
    data_rows: list[list],
    column_types: list[dict],
) -> pd.DataFrame:
    """Build a DataFrame with proper type coercion."""
    df = pd.DataFrame(data_rows, columns=column_names)
    for i, ct in enumerate(column_types):
        tn = ct.get("type_name", "").upper()
        col = column_names[i]
        if tn in _NUMERIC_TYPES:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        elif tn in _DATE_TYPES:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    return df


class ChartGenerator:
    """Generates PNG charts using LLM-driven spec + shared renderer."""

    def __init__(self, llm_client, ws: WorkspaceClient):
        self._llm = llm_client
        self._ws = ws

    # ------------------------------------------------------------------
    # Quick-answer mode (synchronous, returns bytes)
    # ------------------------------------------------------------------

    def generate_bytes(
        self,
        column_names: list[str],
        data_array: list[list],
        column_types: list[dict],
        *,
        user_question: str | None = None,
    ) -> Optional[bytes]:
        """Generate a chart and return PNG bytes (quick-answer mode).

        This is a **synchronous** method.  The caller (slack_handler)
        wraps it with ``asyncio.to_thread``.
        """
        if not data_array or not column_names or len(data_array) < 2:
            return None

        try:
            from infra.chart_spec_client import get_chart_spec

            # Build column metadata in the format chart_spec_client expects
            columns_meta = [
                {"name": n, "type_name": ct.get("type_name", "?")}
                for n, ct in zip(column_names, column_types)
            ]
            spec = get_chart_spec(
                self._ws,
                columns_meta,
                data_array[:5],
                question=user_question,
            )

            if not spec or spec.get("type") == "none":
                logger.info(f"LLM decided no chart: {spec}")
                return None

            logger.info(f"Chart spec from LLM (quick): {spec}")

            from presentation.chart_renderer import render_to_bytes

            df = _build_df(column_names, data_array, column_types)
            return render_to_bytes(df, spec)

        except Exception as e:
            logger.error(f"Chart generation failed: {e}", exc_info=True)
            return None

    # ------------------------------------------------------------------
    # Research mode (async, saves to Volume, returns path)
    # ------------------------------------------------------------------

    async def generate(
        self,
        job_id: str,
        step_id: str,
        columns: list[dict],
        sample_rows: list[list],
        column_profile: list[dict] | None = None,
        question: str | None = None,
    ) -> Optional[tuple[str, dict]]:
        """Generate a chart and save to Volume.

        Returns ``(volume_path, canonical_spec)`` or None.
        The caller can use ``spec["sort"]`` and ``spec["y"]`` to sort
        table data consistently with the chart.
        """
        if not columns or not sample_rows:
            logger.info(f"Chart skipped for {job_id}/{step_id}: no data")
            return None

        # Skip chart for very small datasets
        if len(sample_rows) < 3:
            logger.info(f"Chart skipped for {job_id}/{step_id}: too few rows ({len(sample_rows)})")
            return None

        try:
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
                [c for c in columns if c.get("name")],
                sample_rows,
                column_profile or [],
            )
            enriched_col_names = [c.get("name", "") for c in enriched_cols]

            df = pd.DataFrame(enriched_rows, columns=enriched_col_names)

            from presentation.chart_renderer import render_to_bytes

            # spec is already canonical (normalized by LLMClient)
            png_bytes = await asyncio.to_thread(render_to_bytes, df, spec, column_profile)

            if not png_bytes:
                return None

            dir_path = Config.volume_charts_dir(job_id)
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
            return volume_path, spec

        except Exception as e:
            logger.warning(f"Chart generation failed for {job_id}/{step_id}: {e}")
            return None
