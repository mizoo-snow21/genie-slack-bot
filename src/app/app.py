import asyncio
import logging
import os
import sys

from config import Config


def setup_logging():
    level = getattr(logging, Config.LOG_LEVEL.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )


async def main():
    setup_logging()
    logger = logging.getLogger(__name__)

    print("=" * 60, flush=True)
    print("GENIE SLACK BOT v2 (research integration) starting...", flush=True)
    print("=" * 60, flush=True)

    # Write startup log to file for remote debugging
    try:
        import datetime
        with open("/tmp/genie-slack-bot-startup.log", "w") as f:
            f.write(f"v2 started at {datetime.datetime.now().isoformat()}\n")
            f.write(f"ENABLE_RESEARCH={os.environ.get('ENABLE_RESEARCH', 'not set')}\n")
            f.write(f"cwd={os.getcwd()}\n")
            f.write(f"files={os.listdir('.')}\n")
        print(f"Startup log written to /tmp/genie-slack-bot-startup.log", flush=True)
    except Exception as ex:
        print(f"Startup log write failed: {ex}", flush=True)

    Config.validate()
    logger.info("Config validated (ENABLE_RESEARCH=%s)", Config.ENABLE_RESEARCH)

    from databricks.sdk import WorkspaceClient
    ws = WorkspaceClient()

    # Check that LLM endpoints and catalog exist (warns, does not fail)
    Config.validate_runtime(ws)

    # Core: Genie client (always needed)
    from infra.genie_client import GenieClient
    genie = GenieClient(ws, Config.DATABRICKS_GENIE_SPACE_ID)

    # Quick-answer chart generator (separate file, no research deps)
    from presentation.chart_generator_quick import generate_chart

    # Research components (lazy, only if enabled)
    orchestrator = None
    job_store = None
    step_store = None
    pdf_renderer = None

    if Config.ENABLE_RESEARCH:
        logger.info("Research mode enabled, initializing...")
        try:
            from infra.init_tables import init_tables
            from infra.llm_client import LLMClient
            from infra.job_store import JobStore
            from infra.step_store import StepStore
            from presentation.chart_generator import ChartGenerator
            from presentation.pdf_renderer import PdfRenderer
            from domain.orchestrator import ResearchOrchestrator

            init_tables(ws)

            llm = LLMClient(ws)
            job_store = JobStore(ws)
            step_store = StepStore(ws)
            chart_gen = ChartGenerator(llm_client=llm, ws=ws)
            pdf_renderer = PdfRenderer()
            orchestrator = ResearchOrchestrator(
                job_store=job_store,
                step_store=step_store,
                genie=genie,
                llm=llm,
                chart_generator=chart_gen,
            )
            logger.info("Research components initialized successfully")
        except Exception as e:
            logger.error("Failed to initialize research components: %s", e, exc_info=True)
            logger.info("Falling back to quick-answer only mode")

    # Slack handler
    from presentation.slack_handler import SlackHandler
    handler = SlackHandler(
        slack_bot_token=Config.SLACK_BOT_TOKEN,
        slack_signing_secret=Config.SLACK_SIGNING_SECRET,
        slack_app_token=Config.SLACK_APP_TOKEN,
        genie_client=genie,
        chart_generator_func=generate_chart,
        orchestrator=orchestrator,
        job_store=job_store,
        step_store=step_store,
        pdf_renderer=pdf_renderer,
    )

    logger.info("Starting Slack bot...")
    await handler.start()


if __name__ == "__main__":
    asyncio.run(main())
