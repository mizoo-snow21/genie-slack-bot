"""
Slack bot handler for Databricks Genie integration
"""
import io
import re
import time
import logging
from collections import OrderedDict
from typing import Dict, Any, List, Optional
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
from databricks_genie_client import DatabricksGenieClient
from chart_generator import generate_chart

logger = logging.getLogger(__name__)

MAX_SLACK_TEXT = 3500
MAX_DEDUP_EVENTS = 1000


class SlackGenieBot:
    """Slack bot that interfaces with Databricks Genie"""

    def __init__(
        self,
        slack_bot_token: str,
        slack_signing_secret: str,
        slack_app_token: str,
        genie_client: DatabricksGenieClient
    ):
        self.app = App(
            token=slack_bot_token,
            signing_secret=slack_signing_secret
        )
        self.slack_app_token = slack_app_token
        self.genie_client = genie_client
        self.client = WebClient(token=slack_bot_token)

        self.conversation_map: Dict[str, str] = {}
        self.message_feedback_map: Dict[str, tuple] = {}
        self._processed_events: OrderedDict[str, bool] = OrderedDict()

        self._register_handlers()

    def _register_handlers(self):
        """Register Slack event handlers"""

        @self.app.event("app_mention")
        def handle_app_mention(event, say, client):
            self._handle_message(event, say, client)

        @self.app.event("message")
        def handle_message_events(event, say, client):
            if event.get("channel_type") == "im" or event.get("thread_ts"):
                self._handle_message(event, say, client)

        @self.app.action("feedback_positive")
        def handle_positive_feedback(ack, body, client):
            ack()
            self._handle_feedback(body, "positive", client)

        @self.app.action("feedback_negative")
        def handle_negative_feedback(ack, body, client):
            ack()
            self._handle_feedback(body, "negative", client)

    def _is_duplicate_event(self, event: Dict[str, Any]) -> bool:
        """Check and record event to prevent duplicate processing."""
        key = event.get("client_msg_id") or f"{event.get('channel')}:{event.get('ts')}"
        if key in self._processed_events:
            return True
        self._processed_events[key] = True
        while len(self._processed_events) > MAX_DEDUP_EVENTS:
            self._processed_events.popitem(last=False)
        return False

    def _handle_message(self, event: Dict[str, Any], say, client):
        """Handle incoming messages from Slack"""
        try:
            if event.get("bot_id"):
                return

            if self._is_duplicate_event(event):
                logger.info(f"Skipping duplicate event: {event.get('ts')}")
                return

            text = event.get("text", "")
            channel = event.get("channel")
            thread_ts = event.get("thread_ts") or event.get("ts")

            text = self._clean_message_text(text)

            if not text.strip():
                say("Please ask me a question about your data!", thread_ts=thread_ts)
                return

            # Send typing indicator with blocks
            thinking_msg = client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                blocks=[
                    {"type": "context", "elements": [
                        {"type": "mrkdwn", "text": ":hourglass_flowing_sand: *Analyzing your question...*"}
                    ]}
                ],
                text="Analyzing your question...",
            )
            thinking_ts = thinking_msg.get("ts")

            conversation_id = self.conversation_map.get(thread_ts)

            logger.info(f"Asking Genie: {text}")
            result = self.genie_client.ask_question(text, conversation_id)

            if result.get("conversation_id"):
                self.conversation_map[thread_ts] = result["conversation_id"]

            # Delete thinking message
            if thinking_ts:
                try:
                    client.chat_delete(channel=channel, ts=thinking_ts)
                except Exception:
                    pass

            # Build and send the main response as a single rich message
            self._send_rich_response(channel, thread_ts, result, client)

            # Send chart (separate because it's a file upload)
            result_data = result.get("result_data")
            if result_data:
                data = result_data.get("data", {})
                if data.get("data_array"):
                    self._send_chart(channel, thread_ts, result_data, client)

            # Send feedback buttons (always last)
            if result.get("success"):
                conv_id = result.get("conversation_id")
                msg_id = result.get("message_id")

                if conv_id and msg_id:
                    feedback_msg = self._send_feedback_buttons(channel, thread_ts, client)
                    if feedback_msg:
                        msg_ts = feedback_msg.get("ts")
                        if msg_ts:
                            self.message_feedback_map[msg_ts] = (conv_id, msg_id)

        except Exception as e:
            logger.error(f"Error handling message: {e}", exc_info=True)
            say(f"Sorry, I encountered an error: {str(e)}", thread_ts=thread_ts)

    def _send_rich_response(self, channel: str, thread_ts: str, result: Dict[str, Any], client):
        """Send a rich Block Kit response combining answer, table, and suggestions."""
        blocks: List[Dict] = []

        # --- Error case ---
        if not result.get("success"):
            error = result.get("error", "Unknown error")
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f":x: *Error*\n{error}"}
            })
            client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                blocks=blocks, text=f"Error: {error}",
            )
            return

        # --- Answer text ---
        response_text = result.get("response", "")
        if response_text:
            answer = self._markdown_to_slack(response_text)
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f":sparkles: *Answer*\n{answer}"}
            })

        # --- Query results table ---
        result_data = result.get("result_data")
        if result_data:
            data = result_data.get("data", {})
            schema = result_data.get("schema", {})
            data_array = data.get("data_array", [])

            if data_array:
                columns = schema.get("columns", [])
                column_names = [col.get("name", f"col_{i}") for i, col in enumerate(columns)]
                row_count = data.get("row_count", len(data_array))

                max_rows = 10
                table_text = self._format_data_array(column_names, data_array[:max_rows])
                table_block_text = f"```\n{table_text}\n```"

                if row_count > max_rows:
                    table_block_text += f"\n_Showing {max_rows} of {row_count} rows_"

                # Check size limit
                if len(table_block_text) > MAX_SLACK_TEXT:
                    for fewer_rows in range(max_rows - 1, 0, -1):
                        table_text = self._format_data_array(column_names, data_array[:fewer_rows])
                        table_block_text = f"```\n{table_text}\n```\n_Showing {fewer_rows} of {row_count} rows_"
                        if len(table_block_text) <= MAX_SLACK_TEXT:
                            break
                    else:
                        table_block_text = f"_Result set too large to display inline ({row_count} rows)._"

                blocks.append({"type": "divider"})
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f":bar_chart: *Query Results*\n{table_block_text}"}
                })

        # --- Suggested follow-up questions ---
        suggested_questions = result.get("suggested_questions", [])
        if suggested_questions:
            questions_text = "\n".join(f"  {i}. {q}" for i, q in enumerate(suggested_questions, 1))
            blocks.append({"type": "divider"})
            blocks.append({
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f":bulb: *Try asking:*\n{questions_text}"}
                ]
            })

        if not blocks:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": ":white_check_mark: Query executed successfully."}
            })

        # Build fallback text
        fallback = response_text or "Query executed successfully"

        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            blocks=blocks,
            text=fallback,
        )

    def _clean_message_text(self, text: str) -> str:
        """Remove bot mention and clean up message text"""
        text = re.sub(r'<@[A-Z0-9]+>', '', text)
        return text.strip()

    @staticmethod
    def _markdown_to_slack(text: str) -> str:
        """Convert Markdown to Slack mrkdwn format."""
        text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
        text = re.sub(r'__(.+?)__', r'*\1*', text)
        text = re.sub(r'^#{1,6}\s+(.+)$', r'*\1*', text, flags=re.MULTILINE)
        text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<\2|\1>', text)
        text = re.sub(r'^[\-\*]\s+', '• ', text, flags=re.MULTILINE)
        return text

    def _send_chart(self, channel: str, thread_ts: str, result_data: dict, client):
        """Generate and send a Plotly chart image to Slack"""
        try:
            data = result_data.get("data", {})
            schema = result_data.get("schema", {})

            data_array = data.get("data_array", [])
            columns = schema.get("columns", [])
            column_names = [col.get("name", f"col_{i}") for i, col in enumerate(columns)]

            png_bytes = generate_chart(column_names, data_array, columns)
            if not png_bytes:
                return

            client.files_upload_v2(
                channel=channel,
                thread_ts=thread_ts,
                file_uploads=[{
                    "file": io.BytesIO(png_bytes),
                    "filename": "chart.png",
                    "title": "Query Result Chart",
                }],
            )
            time.sleep(2)
            logger.info("Chart uploaded to Slack")
        except Exception as e:
            logger.error(f"Error sending chart: {e}", exc_info=True)

    def _send_feedback_buttons(self, channel: str, thread_ts: str, client):
        """Send feedback buttons as a separate message"""
        try:
            blocks = [
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": ":thumbsup: Helpful", "emoji": True},
                            "style": "primary",
                            "action_id": "feedback_positive"
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": ":thumbsdown: Not Helpful", "emoji": True},
                            "action_id": "feedback_negative"
                        }
                    ]
                }
            ]

            response = client.chat_postMessage(
                channel=channel,
                blocks=blocks,
                text="Was this response helpful?",
                thread_ts=thread_ts
            )
            return response
        except Exception as e:
            logger.error(f"Error sending feedback buttons: {e}")
            return None

    def _handle_feedback(self, body: Dict[str, Any], rating: str, client):
        """Handle feedback button click"""
        try:
            message = body.get("message", {})
            msg_ts = message.get("ts")
            channel = body.get("channel", {}).get("id")
            user = body.get("user", {}).get("id")

            feedback_info = self.message_feedback_map.get(msg_ts)

            if not feedback_info:
                logger.warning(f"No feedback info found for message {msg_ts}")
                client.chat_update(
                    channel=channel,
                    ts=msg_ts,
                    blocks=[{
                        "type": "context",
                        "elements": [{"type": "mrkdwn", "text": ":warning: _Unable to submit feedback. Please try asking a new question._"}]
                    }],
                    text="Unable to submit feedback"
                )
                return

            conversation_id, message_id = feedback_info

            success = self.genie_client.send_message_feedback(
                conversation_id=conversation_id,
                message_id=message_id,
                rating=rating
            )

            if success:
                emoji = ":thumbsup:" if rating == "positive" else ":thumbsdown:"
                client.chat_update(
                    channel=channel,
                    ts=msg_ts,
                    blocks=[{
                        "type": "context",
                        "elements": [{"type": "mrkdwn", "text": f"{emoji} _Thanks for your feedback!_"}]
                    }],
                    text="Thanks for your feedback!"
                )
                logger.info(f"User {user} gave {rating} feedback for message {message_id}")
            else:
                client.chat_update(
                    channel=channel,
                    ts=msg_ts,
                    blocks=[{
                        "type": "context",
                        "elements": [{"type": "mrkdwn", "text": ":x: _Failed to submit feedback. Please try again._"}]
                    }],
                    text="Failed to submit feedback"
                )
        except Exception as e:
            logger.error(f"Error handling feedback: {e}", exc_info=True)

    def _format_data_array(self, column_names: list, data_array: list) -> str:
        """Format data array for display in Slack"""
        if not data_array:
            return "No data"

        col_widths = []
        col_types = []

        for i in range(len(column_names)):
            max_width = len(column_names[i])
            is_numeric = True

            for row in data_array:
                val_str = str(row[i]) if row[i] is not None else ""
                max_width = max(max_width, len(val_str))
                if row[i] is not None and not self._is_numeric(row[i]):
                    is_numeric = False

            col_widths.append(min(max_width + 2, 30))
            col_types.append(is_numeric)

        lines = []

        header_parts = [name[:width].strip().center(width) for name, width in zip(column_names, col_widths)]
        lines.append("|".join(header_parts))

        separator_parts = ["-" * width for width in col_widths]
        lines.append("+".join(separator_parts))

        for row in data_array:
            row_parts = []
            for val, width, is_numeric in zip(row, col_widths, col_types):
                val_str = str(val) if val is not None else ""
                truncated = val_str[:width].strip()
                if is_numeric and self._is_numeric(val):
                    formatted = truncated.rjust(width)
                else:
                    formatted = truncated.ljust(width)
                row_parts.append(formatted)
            lines.append("|".join(row_parts))

        return "\n".join(lines)

    def _is_numeric(self, value) -> bool:
        """Check if a value is numeric"""
        if value is None:
            return False
        try:
            float(str(value))
            return True
        except (ValueError, TypeError):
            return False

    def start(self):
        """Start the Slack bot in socket mode"""
        handler = SocketModeHandler(self.app, self.slack_app_token)
        logger.info("Starting Slack bot in socket mode...")
        handler.start()
