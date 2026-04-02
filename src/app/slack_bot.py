"""
Slack bot handler for Databricks Genie integration
"""
import io
import re
import logging
from collections import OrderedDict
from typing import Dict, Any, List, Optional
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
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

        self.conversation_map: Dict[str, str] = {}
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
                blocks=self._context_block(":hourglass_flowing_sand: *Analyzing your question...*"),
                text="Analyzing your question...",
            )
            thinking_ts = thinking_msg.get("ts")

            conv_key = f"{channel}:{thread_ts}"
            conversation_id = self.conversation_map.get(conv_key)

            logger.info(f"Asking Genie: {text}")
            result = self.genie_client.ask_question(text, conversation_id)

            if result.get("conversation_id"):
                self.conversation_map[conv_key] = result["conversation_id"]

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
                    self._send_chart(channel, thread_ts, result_data, client, user_question=text)

            # Send feedback buttons (always last)
            if result.get("success"):
                conv_id = result.get("conversation_id")
                msg_id = result.get("message_id")

                if conv_id and msg_id:
                    self._send_feedback_buttons(channel, thread_ts, conv_id, msg_id, client)

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

                # Limit columns to fit Slack's width (max ~5 columns)
                max_cols = min(len(column_names), 5)
                display_names = column_names[:max_cols]
                display_data = [[row[i] if i < len(row) else "" for i in range(max_cols)] for row in data_array]
                col_note = f"\n_{len(column_names) - max_cols} columns hidden_" if len(column_names) > max_cols else ""

                max_rows = 10
                table_text = self._format_data_array(display_names, display_data[:max_rows])
                table_block_text = f"```\n{table_text}\n```{col_note}"

                if row_count > max_rows:
                    table_block_text += f"\n_Showing {max_rows} of {row_count} rows_"

                # Check size limit
                if len(table_block_text) > MAX_SLACK_TEXT:
                    for fewer_rows in range(max_rows - 1, 0, -1):
                        table_text = self._format_data_array(display_names, display_data[:fewer_rows])
                        table_block_text = f"```\n{table_text}\n```{col_note}\n_Showing {fewer_rows} of {row_count} rows_"
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
            blocks.extend(self._context_block(f":bulb: *Try asking:*\n{questions_text}"))

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
        # Bold: **text** → *text*
        text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
        text = re.sub(r'__(.+?)__', r'*\1*', text)
        # Headers → bold
        text = re.sub(r'^#{1,6}\s+(.+)$', r'*\1*', text, flags=re.MULTILINE)
        # Links
        text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<\2|\1>', text)
        # Bullet points
        text = re.sub(r'^[\-\*]\s+', '• ', text, flags=re.MULTILINE)
        # Slack mrkdwn: *bold* needs a word boundary after closing *.
        # Full-width chars (e.g., （） don't count as boundaries.
        # Insert a space after closing * when followed by non-boundary chars.
        text = re.sub(
            r'\*([^*\n]+)\*(?=[^\s*.,;:!?\-)\]\n])',
            lambda m: f'*{m.group(1)}* ',
            text,
        )
        return text

    def _send_chart(self, channel: str, thread_ts: str, result_data: dict, client, title: Optional[str] = None, user_question: Optional[str] = None):
        """Generate and send a chart image to Slack."""
        try:
            data = result_data.get("data", {})
            schema = result_data.get("schema", {})

            data_array = data.get("data_array", [])
            columns = schema.get("columns", [])
            column_names = [col.get("name", f"col_{i}") for i, col in enumerate(columns)]

            logger.info(f"Generating chart: {len(column_names)} cols, {len(data_array)} rows")
            png_bytes = generate_chart(
                column_names, data_array, columns, title=title,
                llm_client=self.genie_client.workspace_client,
                user_question=user_question,
            )
            if not png_bytes:
                logger.warning(f"generate_chart returned None. cols={column_names}, rows={len(data_array)}, types={[c.get('type_name','?') for c in columns]}")
                return

            client.files_upload_v2(
                channel=channel,
                thread_ts=thread_ts,
                file_uploads=[{
                    "file": io.BytesIO(png_bytes),
                    "filename": "chart.png",
                    "title": title or "Query Result Chart",
                }],
            )
            logger.info("Chart uploaded to Slack")
        except Exception as e:
            logger.error(f"Error sending chart: {e}", exc_info=True)

    def _send_feedback_buttons(self, channel: str, thread_ts: str, conversation_id: str, message_id: str, client):
        """Send feedback buttons with conversation/message IDs embedded in button values."""
        try:
            value = f"{conversation_id}:{message_id}"
            blocks = [
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": ":thumbsup: Helpful", "emoji": True},
                            "style": "primary",
                            "action_id": "feedback_positive",
                            "value": value
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": ":thumbsdown: Not Helpful", "emoji": True},
                            "action_id": "feedback_negative",
                            "value": value
                        }
                    ]
                }
            ]

            client.chat_postMessage(
                channel=channel,
                blocks=blocks,
                text="Was this response helpful?",
                thread_ts=thread_ts
            )
        except Exception as e:
            logger.error(f"Error sending feedback buttons: {e}")

    @staticmethod
    def _parse_button_value(body: dict) -> Optional[tuple]:
        """Parse conversation_id and message_id from button action value."""
        actions = body.get("actions", [])
        value = actions[0].get("value", "") if actions else ""
        parts = value.split(":", 1)
        if len(parts) != 2 or not all(parts):
            return None
        return parts[0], parts[1]

    @staticmethod
    def _context_block(text: str) -> list:
        """Build a single context block for Slack messages."""
        return [{"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}]

    def _handle_feedback(self, body: Dict[str, Any], rating: str, client):
        """Handle feedback button click. Reads conversation/message IDs from button value."""
        try:
            message = body.get("message", {})
            msg_ts = message.get("ts")
            channel = body.get("channel", {}).get("id")
            user = body.get("user", {}).get("id")

            # Extract conversation_id and message_id from button value
            parsed = self._parse_button_value(body)
            if parsed is None:
                actions = body.get("actions", [])
                logger.warning(f"Invalid feedback value: {actions[0].get('value', '') if actions else 'no actions'}")
                client.chat_update(
                    channel=channel,
                    ts=msg_ts,
                    blocks=self._context_block(":warning: _Unable to submit feedback. Please try asking a new question._"),
                    text="Unable to submit feedback"
                )
                return

            conversation_id, message_id = parsed

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
                    blocks=self._context_block(f"{emoji} _Thanks for your feedback!_"),
                    text="Thanks for your feedback!"
                )
                logger.info(f"User {user} gave {rating} feedback for message {message_id}")
            else:
                client.chat_update(
                    channel=channel,
                    ts=msg_ts,
                    blocks=self._context_block(":x: _Failed to submit feedback. Please try again._"),
                    text="Failed to submit feedback"
                )
        except Exception as e:
            logger.error(f"Error handling feedback: {e}", exc_info=True)

    def _format_data_array(self, column_names: list, data_array: list) -> str:
        """Format data array as a Slack-friendly table with pipe separators."""
        if not data_array:
            return "No data"

        # Calculate column widths
        col_widths = []
        for i in range(len(column_names)):
            max_w = len(column_names[i])
            for row in data_array:
                val_str = str(row[i]) if row[i] is not None else "-"
                max_w = max(max_w, len(val_str))
            col_widths.append(min(max_w, 25))

        def _pad(val, width, align_right=False):
            s = str(val) if val is not None else "-"
            s = s[:width]
            return s.rjust(width) if align_right else s.ljust(width)

        # Detect numeric columns
        num_flags = []
        for i in range(len(column_names)):
            is_num = all(
                row[i] is None or self._is_numeric(row[i])
                for row in data_array
            )
            num_flags.append(is_num)

        lines = []
        # Header
        header = " | ".join(_pad(name, col_widths[i]) for i, name in enumerate(column_names))
        lines.append(header)
        # Separator
        sep = "-|-".join("-" * w for w in col_widths)
        lines.append(sep)
        # Rows
        for row in data_array:
            cells = " | ".join(
                _pad(row[i], col_widths[i], align_right=num_flags[i])
                for i in range(len(column_names))
            )
            lines.append(cells)

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
