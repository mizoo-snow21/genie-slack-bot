"""Unified Genie Conversation API client.

Combines:
- Sync quick-answer methods (from slack-bot's DatabricksGenieClient)
- Async research methods with retry/heartbeat (from research-api's GenieClient)

Low-level methods are sync; research methods are async (use asyncio.to_thread
for blocking SDK calls so the event loop stays free).
"""

import asyncio
import logging
import random
import time
from typing import Any, Callable, Dict, Optional

from databricks.sdk import WorkspaceClient

from config import Config

logger = logging.getLogger(__name__)


class GenieClient:
    """Genie Conversation API client for both quick-answer and research modes."""

    def __init__(self, ws: WorkspaceClient, space_id: str):
        self._ws = ws
        self._api = ws.api_client
        self.space_id = space_id
        self._max_retries = Config.GENIE_MAX_RETRIES
        self._schema_cache: dict[str, str] = {}

    @property
    def ws(self) -> WorkspaceClient:
        """Public access to WorkspaceClient (for chart generator LLM calls)."""
        return self._ws

    @property
    def api_client(self):
        """Public access to API client (for LLM chart generation)."""
        return self._api

    # ------------------------------------------------------------------
    # Low-level helpers (sync)
    # ------------------------------------------------------------------

    def _make_request(
        self, method: str, path: str, data: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Make request with 429 retry + exponential backoff + jitter."""
        for attempt in range(self._max_retries + 1):
            try:
                response = self._api.do(method, path, body=data)
                return {"ok": True, "data": response}
            except Exception as e:
                error_str = str(e)
                is_429 = "429" in error_str or "rate limit" in error_str.lower()

                if is_429 and attempt < self._max_retries:
                    base_wait = min(2 ** attempt, 30)
                    jitter = random.uniform(0, base_wait * 0.5)
                    wait = base_wait + jitter
                    logger.warning(
                        f"429 rate limited, retry {attempt + 1}/{self._max_retries} "
                        f"after {wait:.1f}s"
                    )
                    time.sleep(wait)
                    continue

                logger.exception(f"Genie API request failed: {method} {path}")
                return {"ok": False, "error": error_str}

        return {"ok": False, "error": "Max retries exceeded"}

    def send_message(
        self, conversation_id: Optional[str], message: str,
    ) -> Optional[Dict[str, Any]]:
        """Send a message to the Genie space (start or continue conversation)."""
        if conversation_id:
            path = (
                f"/api/2.0/genie/spaces/{self.space_id}"
                f"/conversations/{conversation_id}/messages"
            )
        else:
            path = f"/api/2.0/genie/spaces/{self.space_id}/start-conversation"

        resp = self._make_request("POST", path, data={"content": message})
        if not resp["ok"]:
            return None

        result = resp["data"]
        message_data = result.get("message", {})

        message_id = (
            message_data.get("id")
            or result.get("message_id")
            or result.get("id")
        )
        actual_conv_id = (
            message_data.get("conversation_id")
            or result.get("conversation_id")
            or conversation_id
        )

        logger.info(f"Sent message {message_id} to conversation {actual_conv_id}")

        return {
            "id": message_id,
            "message_id": message_id,
            "conversation_id": actual_conv_id,
            "status": message_data.get("status") or result.get("status"),
            "content": message_data.get("content") or result.get("content"),
            "raw_response": result,
        }

    def get_message_status(
        self, conversation_id: str, message_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Get the status and response of a message."""
        path = (
            f"/api/2.0/genie/spaces/{self.space_id}"
            f"/conversations/{conversation_id}/messages/{message_id}"
        )
        resp = self._make_request("GET", path)
        if not resp["ok"]:
            return None

        result = resp["data"]
        return result.get("message", result)

    def wait_for_response(
        self,
        conversation_id: str,
        message_id: str,
        max_wait_time: int = 60,
        heartbeat_callback: Optional[Callable] = None,
    ) -> Optional[Dict[str, Any]]:
        """Poll for a message response with exponential backoff.

        Args:
            heartbeat_callback: Optional callable invoked every ~HEARTBEAT_INTERVAL
                seconds while waiting (used by research mode to update job heartbeat).
        """
        start_time = time.time()
        attempt = 0
        last_heartbeat = time.time()

        while time.time() - start_time < max_wait_time:
            status = self.get_message_status(conversation_id, message_id)

            if status:
                state = status.get("status")
                if state == "COMPLETED":
                    logger.info(f"Message {message_id} completed")
                    return status
                elif state in ("FAILED", "CANCELLED"):
                    error_detail = status.get("error", {})
                    logger.error(
                        f"Message {message_id} failed with state: {state}, "
                        f"error: {error_detail}"
                    )
                    return status
            else:
                logger.warning(
                    f"Transient error polling message {message_id}, retrying..."
                )

            if heartbeat_callback and time.time() - last_heartbeat > Config.HEARTBEAT_INTERVAL:
                heartbeat_callback()
                last_heartbeat = time.time()

            sleep_seconds = min(1 * (2 ** attempt), 10)
            time.sleep(sleep_seconds)
            attempt += 1

        logger.warning(f"Timeout waiting for message {message_id}")
        return None

    def get_statement_result(
        self, statement_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Get the actual query result data from a SQL statement.

        Handles chunked/external-link responses.  Polls until the statement
        reaches a terminal status, then collects all chunks via
        next_chunk_index.
        """
        stmt = self._make_request("GET", f"/api/2.0/sql/statements/{statement_id}")
        if not stmt["ok"]:
            return None

        stmt_data = stmt["data"]

        # Wait for terminal status if still running
        status = stmt_data.get("status", {}).get("state", "")
        poll_count = 0
        while status in ("PENDING", "RUNNING") and poll_count < 60:
            time.sleep(1)
            stmt = self._make_request(
                "GET", f"/api/2.0/sql/statements/{statement_id}"
            )
            if not stmt["ok"]:
                return None
            stmt_data = stmt["data"]
            status = stmt_data.get("status", {}).get("state", "")
            poll_count += 1

        result_schema = stmt_data.get("manifest", {}).get("schema", {})
        result_obj = stmt_data.get("result", {})

        # Collect inline data_array across chunks
        all_rows = result_obj.get("data_array", [])
        next_chunk = result_obj.get("next_chunk_index")

        while next_chunk is not None:
            chunk_resp = self._make_request(
                "GET",
                f"/api/2.0/sql/statements/{statement_id}/result/chunks/{next_chunk}",
            )
            if not chunk_resp["ok"]:
                break
            chunk_data = chunk_resp["data"]
            all_rows.extend(chunk_data.get("data_array", []))
            next_chunk = chunk_data.get("next_chunk_index")

        if not all_rows and result_obj.get("external_links"):
            logger.warning(
                f"Statement {statement_id} returned external links; "
                "inline data not available. Row count from metadata only."
            )

        result_data = {
            "data_array": all_rows,
            "row_count": (
                stmt_data.get("manifest", {}).get("total_row_count")
                or result_obj.get("row_count")
                or len(all_rows)
            ),
        }

        return {
            "data": result_data,
            "schema": result_schema,
        }

    # ------------------------------------------------------------------
    # Sync quick-answer methods (slack-bot mode)
    # ------------------------------------------------------------------

    def ask_question(
        self, question: str, conversation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Ask a question and get the full response (sync, for quick-answer)."""
        message_result = self.send_message(conversation_id, question)
        if not message_result:
            return {
                "success": False,
                "conversation_id": conversation_id,
                "error": "Failed to send message to Genie API",
            }

        actual_conversation_id = message_result.get(
            "conversation_id", conversation_id
        )
        message_id = (
            message_result.get("id") or message_result.get("message_id")
        )

        response = self.wait_for_response(
            actual_conversation_id, message_id
        )

        if not response:
            return {
                "success": False,
                "conversation_id": actual_conversation_id,
                "error": "Genie API response timeout",
            }

        status = response.get("status")

        if status == "COMPLETED":
            attachments = response.get("attachments", [])
            query_result = response.get("query_result")

            response_text = ""
            statement_id = None

            for attachment in attachments:
                if "text" in attachment:
                    text_content = attachment["text"].get("content", "")
                    if text_content:
                        response_text += text_content + "\n\n"
                elif "query" in attachment:
                    query_data = attachment["query"]
                    description = query_data.get("description", "")
                    statement_id = query_data.get("statement_id")
                    if description:
                        response_text += description + "\n\n"

            result_data = None
            if statement_id:
                statement_result = self.get_statement_result(statement_id)
                if statement_result:
                    result_data = statement_result

            if not response_text.strip():
                response_text = response.get("content", "No response generated")

            suggested_questions = response.get("suggested_questions", [])

            return {
                "success": True,
                "conversation_id": actual_conversation_id,
                "message_id": message_id,
                "response": response_text.strip(),
                "attachments": attachments,
                "query_result": query_result,
                "result_data": result_data,
                "suggested_questions": suggested_questions,
            }

        error_obj = response.get("error", {})
        error_msg = (
            error_obj.get("message")
            or error_obj.get("error")
            or "Unknown error"
        )
        return {
            "success": False,
            "conversation_id": actual_conversation_id,
            "error": f"Query failed: {error_msg}",
        }

    def send_message_feedback(
        self,
        conversation_id: str,
        message_id: str,
        rating: str,
    ) -> bool:
        """Send feedback (POSITIVE/NEGATIVE) for a message."""
        path = (
            f"/api/2.0/genie/spaces/{self.space_id}"
            f"/conversations/{conversation_id}/messages/{message_id}/feedback"
        )
        resp = self._make_request("POST", path, data={"rating": rating.upper()})

        if resp["ok"]:
            logger.info(f"Sent {rating.upper()} feedback for message {message_id}")
            return True
        return False

    # ------------------------------------------------------------------
    # Async research methods
    # ------------------------------------------------------------------

    async def get_schema(self) -> str:
        """Get available tables and columns from Genie Space.

        Starts a conversation asking Genie for its schema, caches the result
        per space_id so it's only fetched once per process lifetime.

        Returns:
            Schema description string, or empty string on failure.
        """
        if self.space_id in self._schema_cache:
            return self._schema_cache[self.space_id]

        try:
            resp = await asyncio.to_thread(
                self._make_request,
                "POST",
                f"/api/2.0/genie/spaces/{self.space_id}/start-conversation",
                {
                    "content": (
                        "List all available tables and their columns. "
                        "Output as: table_name: col1, col2, ..."
                    )
                },
            )
            if not resp["ok"]:
                logger.warning(f"Schema fetch failed: {resp['error']}")
                return ""

            result = resp["data"]
            message = result.get("message", {})
            conv_id = (
                message.get("conversation_id")
                or result.get("conversation_id")
            )
            msg_id = (
                message.get("id")
                or result.get("message_id")
                or result.get("id")
            )

            if not conv_id or not msg_id:
                return ""

            # Poll for completion (max 30s)
            response = await self._wait_for_response_async(
                conv_id, msg_id, max_wait_time=30, heartbeat_callback=None,
            )
            if not response or response.get("status") != "COMPLETED":
                return ""

            # Extract text from attachments
            schema_text = ""
            for att in response.get("attachments", []):
                if "text" in att:
                    schema_text += att["text"].get("content", "") + "\n"

            schema_text = schema_text.strip()
            if schema_text:
                self._schema_cache[self.space_id] = schema_text
                logger.info(
                    f"Cached schema for space {self.space_id} "
                    f"({len(schema_text)} chars)"
                )
            return schema_text

        except Exception as e:
            logger.warning(f"Schema fetch error for space {self.space_id}: {e}")
            return ""

    async def ask_question_async(
        self,
        question: str,
        max_wait_time: int = 60,
        heartbeat_callback: Optional[Callable] = None,
    ) -> Dict[str, Any]:
        """Ask a question to Genie, starting a new conversation (async).

        Each call starts a fresh conversation to prevent context leakage
        between research sub-questions.

        Returns dict with keys:
            success, conversation_id, message_id, description,
            sql_query, statement_id, result_data, result_schema, error
        """
        path = f"/api/2.0/genie/spaces/{self.space_id}/start-conversation"
        resp = await asyncio.to_thread(
            self._make_request, "POST", path, {"content": question},
        )

        if not resp["ok"]:
            return {"success": False, "error": resp["error"]}

        result = resp["data"]
        message_data = result.get("message", {})
        conversation_id = (
            message_data.get("conversation_id")
            or result.get("conversation_id")
        )
        message_id = (
            message_data.get("id")
            or result.get("message_id")
            or result.get("id")
        )

        if not conversation_id or not message_id:
            return {
                "success": False,
                "error": "Missing conversation_id or message_id in response",
            }

        # Poll for completion
        response = await self._wait_for_response_async(
            conversation_id, message_id, max_wait_time, heartbeat_callback,
        )

        if not response:
            return {
                "success": False,
                "conversation_id": conversation_id,
                "error": "Genie response timeout",
            }

        status = response.get("status")
        if status != "COMPLETED":
            error_msg = response.get("error", {}).get(
                "message", f"Status: {status}"
            )
            return {
                "success": False,
                "conversation_id": conversation_id,
                "error": error_msg,
            }

        # Extract results (includes get_statement_result which may poll with
        # time.sleep — must run in thread to avoid blocking event loop)
        return await asyncio.to_thread(
            self._extract_research_result, response, conversation_id, message_id,
        )

    async def _wait_for_response_async(
        self,
        conversation_id: str,
        message_id: str,
        max_wait_time: int,
        heartbeat_callback: Optional[Callable],
    ) -> Optional[Dict[str, Any]]:
        """Poll for message completion with exponential backoff (async)."""
        start_time = time.time()
        attempt = 0
        last_heartbeat = time.time()

        while time.time() - start_time < max_wait_time:
            path = (
                f"/api/2.0/genie/spaces/{self.space_id}"
                f"/conversations/{conversation_id}/messages/{message_id}"
            )
            resp = await asyncio.to_thread(self._make_request, "GET", path)

            if resp["ok"]:
                data = resp["data"]
                message = data.get("message", data)
                state = message.get("status")

                if state == "COMPLETED":
                    return message
                elif state in ("FAILED", "CANCELLED"):
                    return message

            # Heartbeat callback (run in thread to avoid blocking event loop)
            if (
                heartbeat_callback
                and time.time() - last_heartbeat > Config.HEARTBEAT_INTERVAL
            ):
                await asyncio.to_thread(heartbeat_callback)
                last_heartbeat = time.time()

            sleep_seconds = min(1 * (2 ** attempt), 10)
            await asyncio.sleep(sleep_seconds)
            attempt += 1

        return None

    def _extract_research_result(
        self,
        response: Dict[str, Any],
        conversation_id: str,
        message_id: str,
    ) -> Dict[str, Any]:
        """Extract SQL, description, and result data from a completed Genie response."""
        attachments = response.get("attachments", [])
        description = ""
        sql_query = ""
        statement_id = None

        for att in attachments:
            if "text" in att:
                text_content = att["text"].get("content", "")
                if text_content:
                    description += text_content + "\n"
            elif "query" in att:
                query_data = att["query"]
                sql_query = query_data.get("query", "")
                statement_id = query_data.get("statement_id")
                desc = query_data.get("description", "")
                if desc:
                    description += desc + "\n"

        result_data = None
        result_schema = None
        if statement_id:
            stmt_result = self.get_statement_result(statement_id)
            if stmt_result:
                result_data = stmt_result["data"]
                result_schema = stmt_result["schema"]

        return {
            "success": True,
            "conversation_id": conversation_id,
            "message_id": message_id,
            "description": description.strip(),
            "sql_query": sql_query,
            "statement_id": statement_id,
            "result_data": result_data,
            "result_schema": result_schema,
        }
