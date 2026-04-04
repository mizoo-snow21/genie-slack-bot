"""Delta table CRUD for research_steps and research_reports."""
import json
import logging
from datetime import datetime
from typing import Optional

from databricks.sdk import WorkspaceClient

from config import Config

logger = logging.getLogger(__name__)


class StepStore:
    """CRUD operations for research_steps and research_reports."""

    def __init__(self, ws: WorkspaceClient):
        self._ws = ws
        self._warehouse_id: Optional[str] = None

    def _get_warehouse_id(self) -> str:
        if self._warehouse_id is None:
            warehouses = self._ws.warehouses.list()
            for wh in warehouses:
                if wh.state and wh.state.value in ("RUNNING", "STARTING"):
                    self._warehouse_id = wh.id
                    return self._warehouse_id
            raise RuntimeError("No available SQL warehouse found")
        return self._warehouse_id

    def _execute_sql(self, sql: str, parameters: list[dict] | None = None) -> dict:
        """Execute SQL with parameterized queries (same pattern as JobStore)."""
        body = {
            "statement": sql,
            "warehouse_id": self._get_warehouse_id(),
            "wait_timeout": "30s",
        }
        if parameters:
            body["parameters"] = parameters
        return self._ws.api_client.do("POST", "/api/2.0/sql/statements", body=body)

    def _query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]:
        resp = self._execute_sql(sql, parameters)
        columns = [c["name"] for c in resp.get("manifest", {}).get("schema", {}).get("columns", [])]
        rows = resp.get("result", {}).get("data_array", [])
        return [dict(zip(columns, row)) for row in rows]

    def create_step(self, job_id: str, step_id: str, step_order: int, question: str):
        """Insert a new step with status=pending."""
        table = Config.table_name("research_steps")
        self._execute_sql(
            f"""
            INSERT INTO {table}
            (job_id, step_id, step_order, question, status, created_at)
            VALUES
            (:job_id, :step_id, :step_order, :question, 'pending', CURRENT_TIMESTAMP())
            """,
            [
                {"name": "job_id", "value": job_id, "type": "STRING"},
                {"name": "step_id", "value": step_id, "type": "STRING"},
                {"name": "step_order", "value": str(step_order), "type": "INT"},
                {"name": "question", "value": question, "type": "STRING"},
            ],
        )

    def update_step_running(self, job_id: str, step_id: str, genie_conversation_id: str):
        """Mark step as running with Genie conversation ID."""
        table = Config.table_name("research_steps")
        self._execute_sql(
            f"""
            UPDATE {table}
            SET status = 'running', genie_conversation_id = :conv_id
            WHERE job_id = :job_id AND step_id = :step_id
            """,
            [
                {"name": "conv_id", "value": genie_conversation_id, "type": "STRING"},
                {"name": "job_id", "value": job_id, "type": "STRING"},
                {"name": "step_id", "value": step_id, "type": "STRING"},
            ],
        )

    def update_step_completed(
        self,
        job_id: str,
        step_id: str,
        sql_query: str,
        result_summary: str,
        result_columns: list[dict],
        result_row_count: int,
        result_sample: list[list],
        result_is_truncated: bool,
        column_profile: list[dict] | None = None,
    ):
        """Mark step as completed with results."""
        table = Config.table_name("research_steps")

        self._execute_sql(
            f"""
            UPDATE {table}
            SET status = 'completed',
                sql_query = :sql_query,
                result_summary = :summary,
                result_columns = :columns,
                result_row_count = :row_count,
                result_sample = :sample,
                result_is_truncated = :truncated,
                column_profile = :profile,
                completed_at = CURRENT_TIMESTAMP()
            WHERE job_id = :job_id AND step_id = :step_id
            """,
            [
                {"name": "sql_query", "value": sql_query or "", "type": "STRING"},
                {"name": "summary", "value": result_summary, "type": "STRING"},
                {"name": "columns", "value": json.dumps(result_columns), "type": "STRING"},
                {"name": "row_count", "value": str(result_row_count), "type": "INT"},
                {"name": "sample", "value": json.dumps(result_sample, ensure_ascii=False), "type": "STRING"},
                {"name": "truncated", "value": str(result_is_truncated).upper(), "type": "BOOLEAN"},
                {"name": "profile", "value": json.dumps(column_profile or [], ensure_ascii=False), "type": "STRING"},
                {"name": "job_id", "value": job_id, "type": "STRING"},
                {"name": "step_id", "value": step_id, "type": "STRING"},
            ],
        )

    def update_step_failed(self, job_id: str, step_id: str):
        """Mark step as failed."""
        table = Config.table_name("research_steps")
        self._execute_sql(
            f"""
            UPDATE {table}
            SET status = 'failed', completed_at = CURRENT_TIMESTAMP()
            WHERE job_id = :job_id AND step_id = :step_id
            """,
            [
                {"name": "job_id", "value": job_id, "type": "STRING"},
                {"name": "step_id", "value": step_id, "type": "STRING"},
            ],
        )

    def update_step_chart(self, job_id: str, step_id: str, chart_volume_path: str):
        """Update chart_volume_path for a completed step."""
        table = Config.table_name("research_steps")
        self._execute_sql(
            f"""
            UPDATE {table}
            SET chart_volume_path = :chart_path
            WHERE job_id = :job_id AND step_id = :step_id
            """,
            [
                {"name": "chart_path", "value": chart_volume_path, "type": "STRING"},
                {"name": "job_id", "value": job_id, "type": "STRING"},
                {"name": "step_id", "value": step_id, "type": "STRING"},
            ],
        )

    def update_step_sample(self, job_id: str, step_id: str, sample: list[list]):
        """Update result_sample for a completed step (e.g. after sorting)."""
        import json as _json
        table = Config.table_name("research_steps")
        self._execute_sql(
            f"""
            UPDATE {table}
            SET result_sample = :sample
            WHERE job_id = :job_id AND step_id = :step_id
            """,
            [
                {"name": "sample", "value": _json.dumps(sample, ensure_ascii=False), "type": "STRING"},
                {"name": "job_id", "value": job_id, "type": "STRING"},
                {"name": "step_id", "value": step_id, "type": "STRING"},
            ],
        )

    def get_steps(self, job_id: str) -> list[dict]:
        """Get all steps for a job, ordered by step_order."""
        table = Config.table_name("research_steps")
        return self._query_rows(
            f"SELECT * FROM {table} WHERE job_id = :job_id ORDER BY step_order",
            [{"name": "job_id", "value": job_id, "type": "STRING"}],
        )

    def save_report(self, job_id: str, report_markdown: str, report_narrative: str = ""):
        """Save the final report and optional narrative (pre-merge LLM output)."""
        table = Config.table_name("research_reports")
        self._execute_sql(
            f"""
            INSERT INTO {table} (job_id, report_markdown, report_narrative, created_at)
            VALUES (:job_id, :report, :narrative, CURRENT_TIMESTAMP())
            """,
            [
                {"name": "job_id", "value": job_id, "type": "STRING"},
                {"name": "report", "value": report_markdown, "type": "STRING"},
                {"name": "narrative", "value": report_narrative, "type": "STRING"},
            ],
        )

    def get_report(self, job_id: str) -> Optional[str]:
        """Get the report markdown for a job. Returns None if not found."""
        table = Config.table_name("research_reports")
        rows = self._query_rows(
            f"SELECT report_markdown FROM {table} WHERE job_id = :job_id",
            [{"name": "job_id", "value": job_id, "type": "STRING"}],
        )
        return rows[0]["report_markdown"] if rows else None

    def get_report_record(self, job_id: str) -> Optional[dict]:
        """Get full report record including narrative. Returns dict or None."""
        table = Config.table_name("research_reports")
        rows = self._query_rows(
            f"SELECT report_markdown, report_narrative FROM {table} WHERE job_id = :job_id",
            [{"name": "job_id", "value": job_id, "type": "STRING"}],
        )
        return rows[0] if rows else None
