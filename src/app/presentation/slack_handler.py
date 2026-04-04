"""Async Slack handler for Databricks Genie — quick-answer + research modes.

Replaces the original SlackGenieBot with:
- AsyncApp for non-blocking event handling
- Mode selection buttons (quick vs research) when ENABLE_RESEARCH=true
- Research job lifecycle (create, poll progress, cancel, PDF upload)
- Orphan recovery on startup

All research-specific imports are lazy (inside method bodies) so that
ENABLE_RESEARCH=false never requires research dependencies.
"""

import asyncio
import io
import json
import logging
import re
import time
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional

from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

from config import Config

logger = logging.getLogger(__name__)

MAX_SLACK_TEXT = 3500
MAX_DEDUP_EVENTS = 1000
_POLL_INTERVAL = 5  # seconds between progress polls


class SlackHandler:
    """Async Slack handler with quick-answer and optional research mode."""

    def __init__(
        self,
        slack_bot_token: str,
        slack_signing_secret: str,
        slack_app_token: str,
        genie_client,
        chart_generator_func: Optional[Callable] = None,
        orchestrator=None,
        job_store=None,
        step_store=None,
        pdf_renderer=None,
    ):
        self.app = AsyncApp(
            token=slack_bot_token,
            signing_secret=slack_signing_secret,
        )
        self._slack_app_token = slack_app_token
        self._genie = genie_client
        self._chart_generator_func = chart_generator_func
        self._orchestrator = orchestrator
        self._job_store = job_store
        self._step_store = step_store
        self._pdf_renderer = pdf_renderer

        self._research_enabled: bool = (
            Config.ENABLE_RESEARCH and orchestrator is not None
        )

        # conversation_id cache for quick-answer follow-ups
        self.conversation_map: Dict[str, str] = {}
        self._processed_events: OrderedDict[str, bool] = OrderedDict()

        # Tracks running research tasks: job_id -> (orch_task, poll_task)
        self._active_research: Dict[str, tuple[asyncio.Task, asyncio.Task]] = {}

        self._register_handlers()

    # ------------------------------------------------------------------
    # Handler registration
    # ------------------------------------------------------------------

    def _register_handlers(self):
        @self.app.event("app_mention")
        async def handle_app_mention(event, say, client):
            await self._handle_message(event, say, client)

        @self.app.event("message")
        async def handle_message_events(event, say, client):
            if event.get("channel_type") == "im" or event.get("thread_ts"):
                await self._handle_message(event, say, client)

        @self.app.action("feedback_positive")
        async def handle_positive_feedback(ack, body, client):
            await ack()
            await self._handle_feedback(body, "positive", client)

        @self.app.action("feedback_negative")
        async def handle_negative_feedback(ack, body, client):
            await ack()
            await self._handle_feedback(body, "negative", client)

        if self._research_enabled:
            @self.app.action("mode_quick")
            async def handle_mode_quick(ack, body, client):
                await ack()
                await self._handle_mode_quick(body, client)

            @self.app.action("mode_research")
            async def handle_mode_research(ack, body, client):
                await ack()
                await self._handle_mode_research(body, client)

            @self.app.action("cancel_research")
            async def handle_cancel_research(ack, body, client):
                await ack()
                await self._handle_cancel_research(body, client)

    # ------------------------------------------------------------------
    # Event deduplication
    # ------------------------------------------------------------------

    def _is_duplicate_event(self, event: Dict[str, Any]) -> bool:
        key = event.get("client_msg_id") or f"{event.get('channel')}:{event.get('ts')}"
        if key in self._processed_events:
            return True
        self._processed_events[key] = True
        while len(self._processed_events) > MAX_DEDUP_EVENTS:
            self._processed_events.popitem(last=False)
        return False

    # ------------------------------------------------------------------
    # Message entry point
    # ------------------------------------------------------------------

    async def _handle_message(self, event: Dict[str, Any], say, client):
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
                await say("Please ask me a question about your data!", thread_ts=thread_ts)
                return

            if self._research_enabled:
                # Show mode selection buttons
                from presentation.mode_selector import build_mode_selection_blocks

                blocks = build_mode_selection_blocks(text)
                await client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    blocks=blocks,
                    text="How would you like to explore this?",
                )
            else:
                # Direct quick-answer (no mode selection)
                await self._run_quick_answer(channel, thread_ts, text, client)

        except Exception as e:
            logger.error(f"Error handling message: {e}", exc_info=True)
            thread_ts = event.get("thread_ts") or event.get("ts")
            await say(f"Sorry, I encountered an error: {str(e)}", thread_ts=thread_ts)

    # ------------------------------------------------------------------
    # Mode handlers
    # ------------------------------------------------------------------

    async def _handle_mode_quick(self, body: Dict[str, Any], client):
        """User selected quick-answer mode."""
        try:
            actions = body.get("actions", [])
            question = actions[0].get("value", "") if actions else ""
            channel = body.get("channel", {}).get("id")
            msg_ts = body.get("message", {}).get("ts")
            thread_ts = body.get("message", {}).get("thread_ts") or msg_ts

            # Delete mode selection message
            if msg_ts and channel:
                try:
                    await client.chat_delete(channel=channel, ts=msg_ts)
                except Exception:
                    pass

            await self._run_quick_answer(channel, thread_ts, question, client)

        except Exception as e:
            logger.error(f"Error in mode_quick: {e}", exc_info=True)

    async def _handle_mode_research(self, body: Dict[str, Any], client):
        """User selected research mode. Launch background orchestration."""
        try:
            actions = body.get("actions", [])
            question = actions[0].get("value", "") if actions else ""
            channel = body.get("channel", {}).get("id")
            msg_ts = body.get("message", {}).get("ts")
            thread_ts = body.get("message", {}).get("thread_ts") or msg_ts

            # Delete mode selection message
            if msg_ts and channel:
                try:
                    await client.chat_delete(channel=channel, ts=msg_ts)
                except Exception:
                    pass

            # Post initial progress message
            from presentation.progress_view import build_progress_blocks

            initial_blocks = build_progress_blocks("planning")
            progress_msg = await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                blocks=initial_blocks,
                text="Starting research...",
            )
            progress_ts = progress_msg.get("ts")
            if not progress_ts:
                logger.error("Failed to post progress message — no ts returned")
                return

            # Create job in Delta (blocking SQL call -> run in thread)
            job_config = {
                "max_steps": Config.MAX_STEPS,
                "max_duration_seconds": Config.MAX_DURATION,
                "max_result_rows_per_query": Config.MAX_RESULT_ROWS,
                "enable_charts": Config.ENABLE_CHARTS,
            }
            job_id = await asyncio.to_thread(
                self._job_store.create_job,
                space_id=Config.DATABRICKS_GENIE_SPACE_ID,
                question=question,
                config=job_config,
                slack_channel_id=channel,
                slack_thread_ts=thread_ts,
            )

            # Transition queued → planning (worker/loop.py does this via claim_next_job,
            # but we skip the worker and call orchestrator directly)
            await asyncio.to_thread(
                self._job_store.transition_status, job_id, "queued", "planning")

            # Launch orchestrator as background task
            orch_task = asyncio.create_task(
                self._orchestrator.run(job_id),
                name=f"orch-{job_id}",
            )

            # Launch progress poller as background task
            poll_task = asyncio.create_task(
                self._poll_progress(
                    job_id=job_id,
                    channel=channel,
                    thread_ts=thread_ts,
                    progress_ts=progress_ts,
                    question=question,
                    orch_task=orch_task,
                    client=client,
                ),
                name=f"poll-{job_id}",
            )

            self._active_research[job_id] = (orch_task, poll_task)

            # Add done callbacks to log unhandled exceptions
            orch_task.add_done_callback(
                lambda t: self._log_task_exception(t, f"orch-{job_id}")
            )
            poll_task.add_done_callback(
                lambda t: self._log_task_exception(t, f"poll-{job_id}")
            )

        except Exception as e:
            logger.error(f"Error in mode_research: {e}", exc_info=True)

    async def _handle_cancel_research(self, body: Dict[str, Any], client):
        """User clicked cancel on a research job."""
        try:
            actions = body.get("actions", [])
            job_id = actions[0].get("value", "") if actions else ""
            channel = body.get("channel", {}).get("id")
            msg_ts = body.get("message", {}).get("ts")

            if not job_id:
                return

            # Update message immediately to show cancel in progress
            from presentation.progress_view import build_cancel_requested_blocks

            cancel_blocks = build_cancel_requested_blocks()
            if msg_ts and channel:
                try:
                    await client.chat_update(
                        channel=channel,
                        ts=msg_ts,
                        blocks=cancel_blocks,
                        text="Cancelling...",
                    )
                except Exception:
                    pass

            # Set cancel flag in Delta (worker checks at step boundaries)
            await asyncio.to_thread(self._job_store.set_cancel_requested, job_id)

        except Exception as e:
            logger.error(f"Error in cancel_research: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # Progress polling
    # ------------------------------------------------------------------

    async def _poll_progress(
        self,
        job_id: str,
        channel: str,
        thread_ts: str,
        progress_ts: str,
        question: str,
        orch_task: asyncio.Task,
        client,
    ):
        """Poll job status and update Slack progress message until completion."""
        from presentation.progress_view import build_progress_blocks

        start_time = time.time()
        last_status = ""

        try:
            while not orch_task.done():
                await asyncio.sleep(_POLL_INTERVAL)

                # Fetch job + steps from Delta (blocking -> thread)
                job = await asyncio.to_thread(self._job_store.get_job, job_id)
                if not job:
                    break

                status = job.get("status", "")
                steps = await asyncio.to_thread(self._step_store.get_steps, job_id)
                step_list = [
                    {"question": s.get("question", ""), "status": s.get("status", "pending")}
                    for s in steps
                ]

                elapsed = time.time() - start_time
                blocks = build_progress_blocks(
                    status=status,
                    steps=step_list,
                    job_id=job_id,
                    elapsed_seconds=elapsed,
                )

                try:
                    await client.chat_update(
                        channel=channel,
                        ts=progress_ts,
                        blocks=blocks,
                        text=f"Research status: {status}",
                    )
                except Exception as e:
                    logger.warning(f"Failed to update progress message: {e}")

                last_status = status

            # Orchestrator finished -- get final state
            job = await asyncio.to_thread(self._job_store.get_job, job_id)
            if not job:
                return

            final_status = job.get("status", "")
            elapsed = time.time() - start_time

            if final_status == "completed":
                # Get report summary for the completion message
                report_md = await asyncio.to_thread(
                    self._step_store.get_report, job_id
                )
                summary = None
                if report_md:
                    summary = self._extract_summary_for_slack(report_md)

                blocks = build_progress_blocks(
                    status="completed",
                    elapsed_seconds=elapsed,
                    summary=summary,
                )
                try:
                    await client.chat_update(
                        channel=channel,
                        ts=progress_ts,
                        blocks=blocks,
                        text="Research completed!",
                    )
                except Exception:
                    pass

                # Upload PDF
                await self._upload_pdf(job_id, channel, thread_ts, client)

            elif final_status == "cancelled":
                blocks = build_progress_blocks(status="cancelled")
                try:
                    await client.chat_update(
                        channel=channel,
                        ts=progress_ts,
                        blocks=blocks,
                        text="Research cancelled",
                    )
                except Exception:
                    pass

            elif final_status == "failed":
                error = job.get("error", "Unknown error")
                blocks = build_progress_blocks(status="failed", error=error)
                try:
                    await client.chat_update(
                        channel=channel,
                        ts=progress_ts,
                        blocks=blocks,
                        text=f"Research failed: {error}",
                    )
                except Exception:
                    pass

        except Exception as e:
            logger.error(f"Error in poll_progress for {job_id}: {e}", exc_info=True)
        finally:
            self._active_research.pop(job_id, None)

    # ------------------------------------------------------------------
    # PDF upload
    # ------------------------------------------------------------------

    async def _upload_pdf(self, job_id: str, channel: str, thread_ts: str, client):
        """Generate and upload PDF report to Slack thread."""
        try:
            if not self._pdf_renderer:
                logger.warning("PDF renderer not available, skipping upload")
                return

            # Lazy import for research-only dependency
            from domain.report_renderer import ReportRenderer

            # Get steps and report from Delta
            steps = await asyncio.to_thread(self._step_store.get_steps, job_id)
            report_record = await asyncio.to_thread(
                self._step_store.get_report_record, job_id
            )
            if not report_record:
                logger.warning(f"No report found for job {job_id}, skipping PDF")
                return

            narrative = report_record.get("report_narrative", "")
            if not narrative:
                # Fallback: use the merged markdown
                narrative = report_record.get("report_markdown", "")

            # Collect chart images from Volume
            chart_images: Dict[str, bytes] = {}
            completed_steps = [s for s in steps if s.get("status") == "completed"]
            for step in completed_steps:
                chart_path = step.get("chart_volume_path")
                if chart_path:
                    try:
                        resp = await asyncio.to_thread(
                            self._genie.ws.files.download, chart_path
                        )
                        chart_images[step["step_id"]] = resp.contents.read()
                    except Exception as e:
                        logger.warning(f"Failed to download chart {chart_path}: {e}")

            # Re-merge narrative with evidence for PDF (charts above tables, no SQL)
            evidence = ReportRenderer.build_evidence(completed_steps)
            pdf_markdown = ReportRenderer.merge(
                narrative, evidence, job_id=job_id, for_pdf=True
            )

            # Render PDF (blocking CPU work -> thread)
            pdf_bytes = await asyncio.to_thread(
                self._pdf_renderer.render,
                pdf_markdown,
                job_id,
                chart_images,
            )

            # Save PDF to Volume for persistence (Slack files may expire)
            try:
                pdf_dir = f"/Volumes/{Config.RESEARCH_CATALOG}/{Config.RESEARCH_SCHEMA}/charts/{job_id}"
                try:
                    await asyncio.to_thread(self._genie.ws.files.create_directory, pdf_dir)
                except Exception:
                    pass
                pdf_volume_path = f"{pdf_dir}/report.pdf"
                await asyncio.to_thread(
                    self._genie.ws.files.upload, pdf_volume_path, io.BytesIO(pdf_bytes), True
                )
                logger.info(f"PDF saved to Volume: {pdf_volume_path}")
            except Exception as e:
                logger.warning(f"Failed to save PDF to Volume for {job_id}: {e}")

            # Upload to Slack
            await client.files_upload_v2(
                channel=channel,
                thread_ts=thread_ts,
                file_uploads=[{
                    "file": io.BytesIO(pdf_bytes),
                    "filename": f"research_{job_id}.pdf",
                    "title": "Research Report",
                }],
            )
            logger.info(f"PDF uploaded for job {job_id}")

        except Exception as e:
            logger.error(f"Error uploading PDF for {job_id}: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # Quick-answer flow
    # ------------------------------------------------------------------

    async def _run_quick_answer(
        self, channel: str, thread_ts: str, text: str, client,
    ):
        """Execute the quick-answer flow (Genie call + rich response + chart)."""
        try:
            # Thinking indicator
            thinking_msg = await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                blocks=self._context_block(
                    ":hourglass_flowing_sand: *Analyzing your question...*"
                ),
                text="Analyzing your question...",
            )
            thinking_ts = thinking_msg.get("ts")

            # Genie call (sync/blocking -> run in thread)
            conv_key = f"{channel}:{thread_ts}"
            conversation_id = self.conversation_map.get(conv_key)

            result = await asyncio.to_thread(
                self._genie.ask_question, text, conversation_id
            )

            if result.get("conversation_id"):
                self.conversation_map[conv_key] = result["conversation_id"]

            # Delete thinking indicator
            if thinking_ts:
                try:
                    await client.chat_delete(channel=channel, ts=thinking_ts)
                except Exception:
                    pass

            # Send rich response
            await self._send_rich_response(channel, thread_ts, result, client)

            # Send chart
            result_data = result.get("result_data")
            if result_data:
                data = result_data.get("data", {})
                if data.get("data_array"):
                    await self._send_chart(
                        channel, thread_ts, result_data, client,
                        user_question=text,
                    )

            # Send feedback buttons
            if result.get("success"):
                conv_id = result.get("conversation_id")
                msg_id = result.get("message_id")
                if conv_id and msg_id:
                    await self._send_feedback_buttons(
                        channel, thread_ts, conv_id, msg_id, client
                    )

            # Research upgrade button (if research is enabled)
            if self._research_enabled and result.get("success"):
                from presentation.mode_selector import build_research_upgrade_blocks

                upgrade_blocks = build_research_upgrade_blocks(text)
                await client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    blocks=upgrade_blocks,
                    text="Want a deeper analysis?",
                )

        except Exception as e:
            logger.error(f"Error in quick answer: {e}", exc_info=True)
            try:
                await client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=f"Sorry, I encountered an error: {str(e)}",
                )
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Rich response (preserved from original)
    # ------------------------------------------------------------------

    async def _send_rich_response(
        self, channel: str, thread_ts: str, result: Dict[str, Any], client,
    ):
        """Send a rich Block Kit response combining answer, table, and suggestions."""
        blocks: List[Dict] = []

        # --- Error case ---
        if not result.get("success"):
            error = result.get("error", "Unknown error")
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f":x: *Error*\n{error}"},
            })
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                blocks=blocks,
                text=f"Error: {error}",
            )
            return

        # --- Answer text ---
        response_text = result.get("response", "")
        if response_text:
            answer = self._markdown_to_slack(response_text)
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f":sparkles: *Answer*\n{answer}"},
            })

        # --- Query results table ---
        result_data = result.get("result_data")
        if result_data:
            data = result_data.get("data", {})
            schema = result_data.get("schema", {})
            data_array = data.get("data_array", [])

            if data_array:
                columns = schema.get("columns", [])
                column_names = [
                    col.get("name", f"col_{i}") for i, col in enumerate(columns)
                ]
                row_count = data.get("row_count", len(data_array))

                max_cols = min(len(column_names), 5)
                display_names = column_names[:max_cols]
                display_data = [
                    [row[i] if i < len(row) else "" for i in range(max_cols)]
                    for row in data_array
                ]
                col_note = (
                    f"\n_{len(column_names) - max_cols} columns hidden_"
                    if len(column_names) > max_cols
                    else ""
                )

                max_rows = 10
                table_text = self._format_data_array(display_names, display_data[:max_rows])
                table_block_text = f"```\n{table_text}\n```{col_note}"

                if row_count > max_rows:
                    table_block_text += f"\n_Showing {max_rows} of {row_count} rows_"

                if len(table_block_text) > MAX_SLACK_TEXT:
                    for fewer_rows in range(max_rows - 1, 0, -1):
                        table_text = self._format_data_array(
                            display_names, display_data[:fewer_rows]
                        )
                        table_block_text = (
                            f"```\n{table_text}\n```{col_note}"
                            f"\n_Showing {fewer_rows} of {row_count} rows_"
                        )
                        if len(table_block_text) <= MAX_SLACK_TEXT:
                            break
                    else:
                        table_block_text = (
                            f"_Result set too large to display inline ({row_count} rows)._"
                        )

                blocks.append({"type": "divider"})
                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":bar_chart: *Query Results*\n{table_block_text}",
                    },
                })

        # --- Suggested follow-up questions ---
        suggested_questions = result.get("suggested_questions", [])
        if suggested_questions:
            questions_text = "\n".join(
                f"  {i}. {q}" for i, q in enumerate(suggested_questions, 1)
            )
            blocks.append({"type": "divider"})
            blocks.extend(
                self._context_block(f":bulb: *Try asking:*\n{questions_text}")
            )

        if not blocks:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": ":white_check_mark: Query executed successfully.",
                },
            })

        fallback = response_text or "Query executed successfully"

        await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            blocks=blocks,
            text=fallback,
        )

    # ------------------------------------------------------------------
    # Chart (quick-answer)
    # ------------------------------------------------------------------

    async def _send_chart(
        self,
        channel: str,
        thread_ts: str,
        result_data: dict,
        client,
        title: Optional[str] = None,
        user_question: Optional[str] = None,
    ):
        """Generate and send a chart image to Slack."""
        try:
            data = result_data.get("data", {})
            schema = result_data.get("schema", {})

            data_array = data.get("data_array", [])
            columns = schema.get("columns", [])
            column_names = [
                col.get("name", f"col_{i}") for i, col in enumerate(columns)
            ]

            logger.info(
                f"Generating chart: {len(column_names)} cols, {len(data_array)} rows"
            )

            if self._chart_generator_func:
                # Async chart generator (if provided)
                png_bytes = await asyncio.to_thread(
                    self._chart_generator_func,
                    column_names,
                    data_array,
                    columns,
                    title=title,
                    llm_client=self._genie.ws,
                    user_question=user_question,
                )
            else:
                # Fallback: import chart_generator_quick directly
                from presentation.chart_generator_quick import generate_chart

                png_bytes = await asyncio.to_thread(
                    generate_chart,
                    column_names,
                    data_array,
                    columns,
                    title=title,
                    llm_client=self._genie.ws,
                    user_question=user_question,
                )

            if not png_bytes:
                logger.warning(
                    f"generate_chart returned None. "
                    f"cols={column_names}, rows={len(data_array)}, "
                    f"types={[c.get('type_name', '?') for c in columns]}"
                )
                return

            await client.files_upload_v2(
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

    # ------------------------------------------------------------------
    # Feedback buttons
    # ------------------------------------------------------------------

    async def _send_feedback_buttons(
        self,
        channel: str,
        thread_ts: str,
        conversation_id: str,
        message_id: str,
        client,
    ):
        """Send feedback buttons with conversation/message IDs in button values."""
        try:
            value = f"{conversation_id}:{message_id}"
            blocks = [
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": ":thumbsup: Helpful",
                                "emoji": True,
                            },
                            "style": "primary",
                            "action_id": "feedback_positive",
                            "value": value,
                        },
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": ":thumbsdown: Not Helpful",
                                "emoji": True,
                            },
                            "action_id": "feedback_negative",
                            "value": value,
                        },
                    ],
                }
            ]

            await client.chat_postMessage(
                channel=channel,
                blocks=blocks,
                text="Was this response helpful?",
                thread_ts=thread_ts,
            )
        except Exception as e:
            logger.error(f"Error sending feedback buttons: {e}")

    async def _handle_feedback(self, body: Dict[str, Any], rating: str, client):
        """Handle feedback button click."""
        try:
            message = body.get("message", {})
            msg_ts = message.get("ts")
            channel = body.get("channel", {}).get("id")
            user = body.get("user", {}).get("id")

            parsed = self._parse_button_value(body)
            if parsed is None:
                actions = body.get("actions", [])
                logger.warning(
                    f"Invalid feedback value: "
                    f"{actions[0].get('value', '') if actions else 'no actions'}"
                )
                await client.chat_update(
                    channel=channel,
                    ts=msg_ts,
                    blocks=self._context_block(
                        ":warning: _Unable to submit feedback. "
                        "Please try asking a new question._"
                    ),
                    text="Unable to submit feedback",
                )
                return

            conversation_id, message_id = parsed

            success = await asyncio.to_thread(
                self._genie.send_message_feedback,
                conversation_id=conversation_id,
                message_id=message_id,
                rating=rating,
            )

            if success:
                emoji = ":thumbsup:" if rating == "positive" else ":thumbsdown:"
                await client.chat_update(
                    channel=channel,
                    ts=msg_ts,
                    blocks=self._context_block(
                        f"{emoji} _Thanks for your feedback!_"
                    ),
                    text="Thanks for your feedback!",
                )
                logger.info(
                    f"User {user} gave {rating} feedback for message {message_id}"
                )
            else:
                await client.chat_update(
                    channel=channel,
                    ts=msg_ts,
                    blocks=self._context_block(
                        ":x: _Failed to submit feedback. Please try again._"
                    ),
                    text="Failed to submit feedback",
                )
        except Exception as e:
            logger.error(f"Error handling feedback: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # Orphan recovery
    # ------------------------------------------------------------------

    async def recover_orphaned_jobs(self):
        """On startup, fail orphaned research jobs and notify Slack threads."""
        if not self._research_enabled:
            return

        try:
            # Query orphaned jobs before marking them failed
            orphaned = await asyncio.to_thread(
                self._job_store._query_rows,
                f"""
                SELECT job_id, status, slack_channel_id, slack_thread_ts
                FROM {Config.table_name('research_jobs')}
                WHERE status IN ('running_subquestion', 'planning', 'evaluating', 'synthesizing')
                  AND heartbeat_at < TIMESTAMPADD(SECOND, -{Config.ORPHAN_THRESHOLD}, CURRENT_TIMESTAMP())
                """,
            )

            # Mark as failed
            await asyncio.to_thread(self._job_store.recover_orphaned_jobs)

            # Notify Slack threads
            for job in orphaned:
                ch = job.get("slack_channel_id")
                ts = job.get("slack_thread_ts")
                if ch and ts:
                    try:
                        await self.app.client.chat_postMessage(
                            channel=ch,
                            thread_ts=ts,
                            text="⚠️ サーバー再起動によりリサーチが中断されました。再度お試しください。",
                        )
                    except Exception:
                        pass
                logger.info("Recovered orphaned job %s", job.get("job_id"))

            if orphaned:
                logger.info("Orphan recovery: %d jobs recovered", len(orphaned))
            else:
                logger.info("Orphan recovery: no orphaned jobs found")
        except Exception as e:
            logger.error(f"Orphan recovery failed: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # Helpers (preserved from original)
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_summary_for_slack(report_md: str) -> Optional[str]:
        """Extract the summary section from a Markdown report and convert to Slack mrkdwn.

        Looks for ## 要約 / ## Summary section, extracts its content,
        and converts Markdown formatting to Slack mrkdwn.
        """
        lines = report_md.split("\n")
        summary_lines = []
        in_summary = False

        for line in lines:
            stripped = line.strip()
            # Detect summary section header
            if re.match(r'^#{1,3}\s*(要約|Summary|サマリー)', stripped, re.IGNORECASE):
                in_summary = True
                continue
            # Stop at next heading
            if in_summary and re.match(r'^#{1,3}\s+', stripped):
                break
            if in_summary:
                # Skip tables and empty lines at start
                if not summary_lines and (not stripped or stripped.startswith("|")):
                    continue
                summary_lines.append(line)

        if summary_lines:
            text = "\n".join(summary_lines).strip()
        else:
            # Fallback: first non-heading, non-table paragraph
            for line in lines:
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and not stripped.startswith("|") and not stripped.startswith("---"):
                    text = stripped
                    break
            else:
                return None

        # Convert Markdown to Slack mrkdwn
        text = SlackHandler._markdown_to_slack(text)
        # Truncate for Slack
        if len(text) > 2900:
            text = text[:2900] + "..."
        return text

    @staticmethod
    def _clean_message_text(text: str) -> str:
        """Remove bot mention and clean up message text."""
        text = re.sub(r"<@[A-Z0-9]+>", "", text)
        return text.strip()

    @staticmethod
    def _markdown_to_slack(text: str) -> str:
        """Convert Markdown to Slack mrkdwn format."""
        # Bold: **text** -> *text*
        text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
        text = re.sub(r"__(.+?)__", r"*\1*", text)
        # Headers -> bold
        text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)
        # Links
        text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", text)
        # Bullet points
        text = re.sub(r"^[\-\*]\s+", "\u2022 ", text, flags=re.MULTILINE)
        # Slack mrkdwn: *bold* requires whitespace or punctuation at boundaries.
        # Japanese/CJK chars don't count as word boundaries, so Slack ignores
        # the formatting. Fix by ensuring spaces outside * delimiters.
        def _fix_bold_boundaries(m):
            pre = m.group(1) or ""
            content = m.group(2)
            post = m.group(3) or ""
            # Add space before * if preceded by non-boundary char
            if pre and pre not in " \t\n*.,;:!?-([{":
                pre = pre + " "
            # Add space after * if followed by non-boundary char
            if post and post not in " \t\n*.,;:!?-)]}\n":
                post = " " + post
            return f"{pre}*{content}*{post}"
        text = re.sub(
            r"(.)?\*([^*\n]+?)\*(.)?",
            _fix_bold_boundaries,
            text,
        )
        return text

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

    def _format_data_array(self, column_names: list, data_array: list) -> str:
        """Format data array as a Slack-friendly table with pipe separators."""
        if not data_array:
            return "No data"

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

        num_flags = []
        for i in range(len(column_names)):
            is_num = all(
                row[i] is None or self._is_numeric(row[i]) for row in data_array
            )
            num_flags.append(is_num)

        lines = []
        header = " | ".join(
            _pad(name, col_widths[i]) for i, name in enumerate(column_names)
        )
        lines.append(header)
        sep = "-|-".join("-" * w for w in col_widths)
        lines.append(sep)
        for row in data_array:
            cells = " | ".join(
                _pad(row[i], col_widths[i], align_right=num_flags[i])
                for i in range(len(column_names))
            )
            lines.append(cells)

        return "\n".join(lines)

    @staticmethod
    def _is_numeric(value) -> bool:
        """Check if a value is numeric."""
        if value is None:
            return False
        try:
            float(str(value))
            return True
        except (ValueError, TypeError):
            return False

    @staticmethod
    def _log_task_exception(task: asyncio.Task, label: str):
        """Done callback: log unhandled exceptions from background tasks."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error(f"Background task {label} failed: {exc}", exc_info=exc)

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def start(self):
        """Start the Slack bot: recover orphans, then run socket mode."""
        await self.recover_orphaned_jobs()
        handler = AsyncSocketModeHandler(self.app, self._slack_app_token)
        logger.info("Starting Slack bot in async socket mode...")
        await handler.start_async()
