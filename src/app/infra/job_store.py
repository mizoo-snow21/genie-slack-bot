"""Delta table CRUD for research_jobs."""
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from databricks.sdk import WorkspaceClient

from config import Config

logger = logging.getLogger(__name__)


class JobStore:
    """CRUD operations for research_jobs Delta table."""

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
        """Execute SQL statement with parameterized queries.

        Uses the SQL Statement API's `parameters` field to safely bind values,
        preventing SQL injection from user input, LLM output, or JSON blobs.
        Parameters are passed as: [{"name": "p1", "value": "...", "type": "STRING"}, ...]
        Referenced in SQL as :p1, :p2, etc.
        """
        body = {
            "statement": sql,
            "warehouse_id": self._get_warehouse_id(),
            "wait_timeout": "30s",
        }
        if parameters:
            body["parameters"] = parameters
        return self._ws.api_client.do("POST", "/api/2.0/sql/statements", body=body)

    def _query_rows(self, sql: str, parameters: list[dict] | None = None) -> list[dict]:
        """Execute SQL and return list of row dicts."""
        resp = self._execute_sql(sql, parameters)
        columns = [c["name"] for c in resp.get("manifest", {}).get("schema", {}).get("columns", [])]
        rows = resp.get("result", {}).get("data_array", [])
        return [dict(zip(columns, row)) for row in rows]

    def create_job(
        self,
        space_id: str,
        question: str,
        config: dict,
        slack_channel_id: str = "",
        slack_thread_ts: str = "",
    ) -> str:
        """Insert a new job with status=queued. Returns job_id."""
        if not space_id:
            raise ValueError("space_id is required")
        job_id = f"res_{uuid.uuid4()}"
        config_json = json.dumps(config)
        table = Config.table_name("research_jobs")

        sql = f"""
        INSERT INTO {table}
        (job_id, space_id, question, status, cancel_requested, config,
         slack_channel_id, slack_thread_ts,
         heartbeat_at, created_at, updated_at)
        VALUES
        (:job_id, :space_id, :question, 'queued', FALSE,
         :config, :slack_channel_id, :slack_thread_ts,
         CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP())
        """
        params = [
            {"name": "job_id", "value": job_id, "type": "STRING"},
            {"name": "space_id", "value": space_id, "type": "STRING"},
            {"name": "question", "value": question, "type": "STRING"},
            {"name": "config", "value": config_json, "type": "STRING"},
            {"name": "slack_channel_id", "value": slack_channel_id, "type": "STRING"},
            {"name": "slack_thread_ts", "value": slack_thread_ts, "type": "STRING"},
        ]
        self._execute_sql(sql, params)
        logger.info(f"Created job {job_id}")
        return job_id

    def get_job(self, job_id: str) -> Optional[dict]:
        """Get a job by ID. Returns dict or None."""
        table = Config.table_name("research_jobs")
        rows = self._query_rows(
            f"SELECT * FROM {table} WHERE job_id = :job_id",
            [{"name": "job_id", "value": job_id, "type": "STRING"}],
        )
        return rows[0] if rows else None

    def _extract_affected_rows(self, resp: dict) -> int:
        """Extract num_affected_rows from SQL statement response."""
        rows = resp.get("result", {}).get("data_array", [])
        if rows and rows[0]:
            try:
                return int(rows[0][0])
            except (ValueError, IndexError):
                pass
        return 0

    def claim_next_job(self, worker_id: Optional[str] = None) -> Optional[str]:
        """Atomically claim the oldest queued job.

        Loops through queued candidates until one is successfully claimed
        or no candidates remain. Returns job_id or None.

        Args:
            worker_id: Identifier for this worker instance. Stored in DB
                for defense-in-depth against multi-replica overlap.
        """
        table = Config.table_name("research_jobs")

        rows = self._query_rows(
            f"SELECT job_id FROM {table} WHERE status = 'queued' ORDER BY created_at LIMIT 10"
        )
        if not rows:
            return None

        for row in rows:
            job_id = row["job_id"]
            params = [
                {"name": "job_id", "value": job_id, "type": "STRING"},
            ]
            worker_clause = ""
            if worker_id:
                worker_clause = ", worker_id = :worker_id"
                params.append({"name": "worker_id", "value": worker_id, "type": "STRING"})

            resp = self._execute_sql(
                f"""
                UPDATE {table}
                SET status = 'planning',
                    updated_at = CURRENT_TIMESTAMP(),
                    heartbeat_at = CURRENT_TIMESTAMP()
                    {worker_clause}
                WHERE job_id = :job_id AND status = 'queued'
                """,
                params,
            )
            if self._extract_affected_rows(resp) > 0:
                logger.info(f"Claimed job {job_id} (worker={worker_id})")
                return job_id
            logger.info(f"Race on job {job_id}, trying next candidate")

        logger.info("All queued candidates lost to contention")
        return None

    def transition_status(
        self, job_id: str, from_status: str, to_status: str, error: Optional[str] = None
    ) -> bool:
        """Atomically transition job status. Returns True if affected_rows > 0."""
        table = Config.table_name("research_jobs")

        completed_clause = ""
        params = [
            {"name": "to_status", "value": to_status, "type": "STRING"},
            {"name": "job_id", "value": job_id, "type": "STRING"},
            {"name": "from_status", "value": from_status, "type": "STRING"},
        ]
        if to_status in ("completed", "failed", "cancelled"):
            completed_clause = ", completed_at = CURRENT_TIMESTAMP()"

        error_clause = ""
        if error:
            error_clause = ", error = :error"
            params.append({"name": "error", "value": error[:500], "type": "STRING"})

        resp = self._execute_sql(
            f"""
            UPDATE {table}
            SET status = :to_status,
                updated_at = CURRENT_TIMESTAMP(),
                heartbeat_at = CURRENT_TIMESTAMP()
                {completed_clause} {error_clause}
            WHERE job_id = :job_id AND status = :from_status
            """,
            params,
        )

        success = self._extract_affected_rows(resp) > 0
        if success:
            logger.info(f"Job {job_id}: {from_status} -> {to_status}")
        else:
            logger.warning(f"Job {job_id}: transition {from_status} -> {to_status} failed (affected_rows=0)")
        return success

    def update_heartbeat(self, job_id: str):
        """Update heartbeat timestamp."""
        table = Config.table_name("research_jobs")
        self._execute_sql(
            f"UPDATE {table} SET heartbeat_at = CURRENT_TIMESTAMP() WHERE job_id = :job_id",
            [
                {"name": "job_id", "value": job_id, "type": "STRING"},
            ],
        )

    def set_cancel_requested(self, job_id: str) -> bool:
        """Set cancel flag only (does NOT change status). Idempotent.

        Returns True if a row was updated (flag was previously FALSE on a
        non-terminal job). Returns False if the flag was already set or the
        job is terminal. Callers should NOT treat False as an error — the
        flag is guaranteed to be TRUE for any non-terminal job after this call.
        Worker checks this flag at step boundaries and transitions to cancelled.
        """
        table = Config.table_name("research_jobs")
        resp = self._execute_sql(
            f"""
            UPDATE {table}
            SET cancel_requested = TRUE, updated_at = CURRENT_TIMESTAMP()
            WHERE job_id = :job_id
            AND status NOT IN ('completed', 'failed', 'cancelled')
            """,
            [
                {"name": "job_id", "value": job_id, "type": "STRING"},
            ],
        )
        return self._extract_affected_rows(resp) > 0

    def is_cancel_requested(self, job_id: str) -> bool:
        """Check if cancel has been requested."""
        job = self.get_job(job_id)
        return job is not None and job.get("cancel_requested") in (True, "true", "TRUE")

    def recover_orphaned_jobs(self, exclude_job_id: Optional[str] = None, worker_id: Optional[str] = None):
        """Mark orphaned jobs as failed based on heartbeat threshold.

        Args:
            exclude_job_id: If set, skip this job (currently being executed by
                this worker). Single-threaded synchronous operations (Genie
                polling, chart rendering) can block heartbeat updates, so
                the running job must be excluded from orphan detection.
            worker_id: If set, also exclude jobs claimed by this worker
                (defense-in-depth for multi-replica scenarios).
        """
        table = Config.table_name("research_jobs")
        threshold_seconds = Config.ORPHAN_THRESHOLD

        exclude_clause = ""
        params = []
        if exclude_job_id:
            exclude_clause = "AND job_id != :exclude_id"
            params.append({"name": "exclude_id", "value": exclude_job_id, "type": "STRING"})
        if worker_id:
            exclude_clause += " AND (worker_id IS NULL OR worker_id != :worker_id)"
            params.append({"name": "worker_id", "value": worker_id, "type": "STRING"})

        self._execute_sql(
            f"""
            UPDATE {table}
            SET status = 'failed',
                error = 'Orphaned: worker heartbeat expired',
                updated_at = CURRENT_TIMESTAMP(),
                completed_at = CURRENT_TIMESTAMP()
            WHERE status IN ('planning', 'running_subquestion', 'evaluating', 'synthesizing')
            AND heartbeat_at < TIMESTAMPADD(SECOND, -{threshold_seconds}, CURRENT_TIMESTAMP())
            {exclude_clause}
            """,
            params if params else None,
        )
