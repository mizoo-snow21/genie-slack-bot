"""
Configuration management for the Databricks Genie Slack App
"""
import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    """Application configuration"""

    # Slack Configuration
    SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
    SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET")
    SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")

    # Databricks Configuration
    # Databricks Apps: SDK uses the app's service principal automatically
    # Local dev: SDK reads DATABRICKS_HOST and DATABRICKS_TOKEN from environment
    DATABRICKS_GENIE_SPACE_ID = os.getenv("DATABRICKS_GENIE_SPACE_ID")

    # App Configuration
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

    # --- LLM endpoints (shared across modes) ---
    LLM_CHART_ENDPOINT: str = os.getenv("LLM_CHART_ENDPOINT", "databricks-gpt-5-4-mini")  # Chart spec generation (quick-answer + research)

    # --- Research feature flag ---
    ENABLE_RESEARCH: bool = os.getenv("ENABLE_RESEARCH", "false").lower() == "true"

    # --- Research: Delta store ---
    RESEARCH_CATALOG: str = os.getenv("RESEARCH_CATALOG", "")
    RESEARCH_SCHEMA: str = os.getenv("RESEARCH_SCHEMA", "genie_research")

    # --- Research: LLM endpoints ---
    LLM_RESEARCH_ENDPOINT: str = os.getenv("LLM_RESEARCH_ENDPOINT", "databricks-gpt-5-4")  # Plan, evaluate, summarize
    LLM_NARRATIVE_ENDPOINT: str = os.getenv("LLM_NARRATIVE_ENDPOINT", "databricks-claude-opus-4-6")  # Report narrative

    # --- Research: pipeline ---
    INITIAL_PLAN_STEPS: int = int(os.getenv("INITIAL_PLAN_STEPS", "4"))  # Initial parallel sub-questions
    MAX_STEPS: int = int(os.getenv("MAX_STEPS", "6"))  # Initial + follow-up total cap
    MAX_DURATION: int = int(os.getenv("MAX_DURATION", "300"))  # Seconds
    MAX_RESULT_ROWS: int = int(os.getenv("MAX_RESULT_ROWS", "100"))

    # --- Research: Genie client ---
    GENIE_MAX_RETRIES: int = int(os.getenv("GENIE_MAX_RETRIES", "3"))

    # --- Research: chart generation ---
    ENABLE_CHARTS: bool = os.getenv("ENABLE_CHARTS", "true").lower() == "true"
    CHART_TIMEOUT: int = int(os.getenv("CHART_TIMEOUT", "30"))  # Per-chart generation timeout (seconds)

    # --- Research: health monitoring ---
    HEARTBEAT_INTERVAL: int = int(os.getenv("HEARTBEAT_INTERVAL", "30"))  # Seconds between heartbeat updates
    ORPHAN_THRESHOLD: int = int(os.getenv("ORPHAN_THRESHOLD", "600"))  # Seconds before a job is considered orphaned

    # --- Research: storage cleanup ---
    CLEANUP_RETENTION_DAYS: int = int(os.getenv("CLEANUP_RETENTION_DAYS", "30"))  # Days to retain chart/PDF files in Volume

    @classmethod
    def table_name(cls, table: str) -> str:
        """Return fully qualified Delta table name for research storage."""
        return f"{cls.RESEARCH_CATALOG}.{cls.RESEARCH_SCHEMA}.{table}"

    @classmethod
    def volume_charts_dir(cls, job_id: str = "") -> str:
        """Return Volume path for chart/PDF storage."""
        base = f"/Volumes/{cls.RESEARCH_CATALOG}/{cls.RESEARCH_SCHEMA}/charts"
        return f"{base}/{job_id}" if job_id else base

    @classmethod
    def validate(cls):
        """Validate that all required configuration is present."""
        required_vars = [
            "SLACK_BOT_TOKEN",
            "SLACK_SIGNING_SECRET",
            "SLACK_APP_TOKEN",
            "DATABRICKS_GENIE_SPACE_ID"
        ]

        if cls.ENABLE_RESEARCH:
            required_vars.append("RESEARCH_CATALOG")

        missing = [var for var in required_vars if not getattr(cls, var)]

        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

        return True

    @classmethod
    def validate_runtime(cls, ws):
        """Validate runtime dependencies (endpoints, catalog). Call after WorkspaceClient init."""
        import logging
        logger = logging.getLogger(__name__)
        warnings = []

        # Check LLM_CHART_ENDPOINT (used by both quick-answer and research)
        try:
            ws.serving_endpoints.get(cls.LLM_CHART_ENDPOINT)
        except Exception:
            warnings.append(
                f"LLM_CHART_ENDPOINT '{cls.LLM_CHART_ENDPOINT}' not found. "
                f"Charts will not be generated. Create the endpoint or set LLM_CHART_ENDPOINT env var."
            )

        if cls.ENABLE_RESEARCH:
            # Check research LLM endpoints
            for var, name in [("LLM_RESEARCH_ENDPOINT", cls.LLM_RESEARCH_ENDPOINT), ("LLM_NARRATIVE_ENDPOINT", cls.LLM_NARRATIVE_ENDPOINT)]:
                try:
                    ws.serving_endpoints.get(name)
                except Exception:
                    warnings.append(
                        f"{var} '{name}' not found. "
                        f"Research mode will fail. Create the endpoint or set {var} env var."
                    )

            # Check catalog exists
            try:
                ws.catalogs.get(cls.RESEARCH_CATALOG)
            except Exception:
                warnings.append(
                    f"RESEARCH_CATALOG '{cls.RESEARCH_CATALOG}' not found. "
                    f"Delta tables cannot be created. Ensure the catalog exists and SP has access."
                )

        for w in warnings:
            logger.warning(f"⚠️  {w}")

        return warnings
