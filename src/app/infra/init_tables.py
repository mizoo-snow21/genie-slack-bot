"""Initialize Delta tables for research job storage."""
import logging

from databricks.sdk import WorkspaceClient

from config import Config

logger = logging.getLogger(__name__)

CREATE_JOBS = """
CREATE TABLE IF NOT EXISTS {table} (
    job_id STRING NOT NULL,
    space_id STRING NOT NULL,
    question STRING NOT NULL,
    status STRING NOT NULL,
    cancel_requested BOOLEAN,
    config STRING,
    worker_id STRING,
    slack_channel_id STRING,
    slack_thread_ts STRING,
    heartbeat_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    completed_at TIMESTAMP,
    error STRING
)
USING DELTA
"""

CREATE_STEPS = """
CREATE TABLE IF NOT EXISTS {table} (
    job_id STRING NOT NULL,
    step_id STRING NOT NULL,
    step_order INT NOT NULL,
    question STRING NOT NULL,
    status STRING NOT NULL,
    genie_conversation_id STRING,
    sql_query STRING,
    result_summary STRING,
    result_columns STRING,
    result_row_count INT,
    result_sample STRING,
    result_is_truncated BOOLEAN,
    chart_volume_path STRING,
    column_profile STRING,
    created_at TIMESTAMP NOT NULL,
    completed_at TIMESTAMP
)
USING DELTA
"""

CREATE_REPORTS = """
CREATE TABLE IF NOT EXISTS {table} (
    job_id STRING NOT NULL,
    report_markdown STRING NOT NULL,
    report_narrative STRING,
    created_at TIMESTAMP NOT NULL
)
USING DELTA
"""


CREATE_CHARTS_VOLUME = """
CREATE VOLUME IF NOT EXISTS {volume}
COMMENT 'Chart PNG images for Genie Slack Bot'
"""


def init_tables(ws: WorkspaceClient):
    """Create Delta tables and UC Volume if they don't exist."""
    warehouse_id = _get_warehouse_id(ws)

    statements = [
        ("research_jobs", CREATE_JOBS),
        ("research_steps", CREATE_STEPS),
        ("research_reports", CREATE_REPORTS),
    ]
    for name, ddl in statements:
        table = Config.table_name(name)
        sql = ddl.format(table=table)
        logger.info(f"Ensuring table exists: {table}")
        ws.api_client.do(
            "POST",
            "/api/2.0/sql/statements",
            body={
                "statement": sql,
                "warehouse_id": warehouse_id,
                "wait_timeout": "30s",
            },
        )

    # Migration: add report_narrative column to existing tables
    reports_table = Config.table_name("research_reports")
    try:
        ws.api_client.do(
            "POST",
            "/api/2.0/sql/statements",
            body={
                "statement": f"ALTER TABLE {reports_table} ADD COLUMNS (report_narrative STRING)",
                "warehouse_id": warehouse_id,
                "wait_timeout": "30s",
            },
        )
        logger.info(f"Added report_narrative column to {reports_table}")
    except Exception:
        pass  # Column already exists

    # Migration: add column_profile column to research_steps
    steps_table = Config.table_name("research_steps")
    try:
        ws.api_client.do(
            "POST",
            "/api/2.0/sql/statements",
            body={
                "statement": f"ALTER TABLE {steps_table} ADD COLUMNS (column_profile STRING)",
                "warehouse_id": warehouse_id,
                "wait_timeout": "30s",
            },
        )
        logger.info(f"Added column_profile column to {steps_table}")
    except Exception as e:
        if "already exists" in str(e).lower() or "COLUMN_ALREADY_EXISTS" in str(e):
            pass
        else:
            logger.warning(f"Failed to add column_profile column to {steps_table}: {e}")

    # Migration: add slack columns to research_jobs
    jobs_table = Config.table_name("research_jobs")
    for col_name in ("slack_channel_id", "slack_thread_ts"):
        try:
            ws.api_client.do(
                "POST",
                "/api/2.0/sql/statements",
                body={
                    "statement": f"ALTER TABLE {jobs_table} ADD COLUMNS ({col_name} STRING)",
                    "warehouse_id": warehouse_id,
                    "wait_timeout": "30s",
                },
            )
            logger.info(f"Added {col_name} column to {jobs_table}")
        except Exception:
            pass  # Column already exists

    # Create UC Volume for chart images
    volume = Config.table_name("slack_charts")
    sql = CREATE_CHARTS_VOLUME.format(volume=volume)
    logger.info(f"Ensuring volume exists: {volume}")
    ws.api_client.do(
        "POST",
        "/api/2.0/sql/statements",
        body={
            "statement": sql,
            "warehouse_id": warehouse_id,
            "wait_timeout": "30s",
        },
    )


def _get_warehouse_id(ws: WorkspaceClient) -> str:
    """Get the first available SQL warehouse ID."""
    warehouses = ws.warehouses.list()
    for wh in warehouses:
        if wh.state and wh.state.value in ("RUNNING", "STARTING"):
            return wh.id
    raise RuntimeError("No available SQL warehouse found")
