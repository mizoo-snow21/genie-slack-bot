"""LLM-driven chart generation (PNG via matplotlib/seaborn).

Generates PNG chart images from step result data and saves to Unity Catalog Volume.
"""
import asyncio
import io
import json
import logging
from typing import Optional

import pandas as pd

from databricks.sdk import WorkspaceClient
from config import Config

logger = logging.getLogger(__name__)


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

            from presentation.chart_renderer import render_to_bytes

            # Map research spec fields to canonical names
            canonical_spec = {
                "type": spec.get("chart_type", "bar"),
                "x": spec.get("x_column"),
                "y": spec.get("y_column"),
                "y2": spec.get("y2_column"),
                "hue": spec.get("color_column"),
                "sort": spec.get("sort", "none"),
                "title": spec.get("title", ""),
            }
            png_bytes = await asyncio.to_thread(render_to_bytes, df, canonical_spec, column_profile)

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
            return volume_path

        except Exception as e:
            logger.warning(f"Chart generation failed for {job_id}/{step_id}: {e}")
            return None
