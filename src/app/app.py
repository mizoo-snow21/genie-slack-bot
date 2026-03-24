"""
Main application entry point for Databricks Genie Slack Bot
"""
import logging
import sys
from config import Config
from databricks_genie_client import DatabricksGenieClient
from slack_bot import SlackGenieBot


def setup_logging():
    """Configure application logging"""
    logging.basicConfig(
        level=getattr(logging, Config.LOG_LEVEL.upper(), logging.INFO),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def main():
    """Main application entry point"""
    setup_logging()
    logger = logging.getLogger(__name__)

    try:
        logger.info("Starting Databricks Genie Slack Bot...")

        Config.validate()
        logger.info("Configuration validated successfully")

        genie_client = DatabricksGenieClient(
            space_id=Config.DATABRICKS_GENIE_SPACE_ID
        )
        logger.info(f"Genie client initialized for space: {Config.DATABRICKS_GENIE_SPACE_ID}")

        slack_bot = SlackGenieBot(
            slack_bot_token=Config.SLACK_BOT_TOKEN,
            slack_signing_secret=Config.SLACK_SIGNING_SECRET,
            slack_app_token=Config.SLACK_APP_TOKEN,
            genie_client=genie_client
        )

        logger.info("Starting socket mode handler...")
        slack_bot.start()

    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Shutting down gracefully...")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
