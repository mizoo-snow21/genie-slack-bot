"""
Databricks Genie API Client for conversational interactions
"""
import time
import logging
from typing import Optional, Dict, Any
from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)


class DatabricksGenieClient:
    """Client for interacting with Databricks Genie conversational APIs."""

    def __init__(self, space_id: str):
        self.space_id = space_id

        logger.info("Initializing Databricks SDK with OAuth M2M authentication")
        self.workspace_client = WorkspaceClient()
        self.api_client = self.workspace_client.api_client
        self.host = self.workspace_client.config.host.rstrip('/')

        logger.info(f"Connected to Databricks workspace: {self.host}")
        logger.info(f"Genie space ID: {self.space_id}")

    def _make_request(self, method: str, path: str, data: Optional[Dict] = None) -> Dict[str, Any]:
        """Make an authenticated API request. Returns {"ok": True, "data": ...} or {"ok": False, "error": ...}."""
        try:
            response = self.api_client.do(method, path, body=data)
            return {"ok": True, "data": response}
        except Exception as e:
            logger.exception(f"API request failed: {method} {path}")
            return {"ok": False, "error": str(e)}

    def send_message(self, conversation_id: str, message: str) -> Optional[Dict[str, Any]]:
        """Send a message to the Genie space."""
        if conversation_id:
            path = f"/api/2.0/genie/spaces/{self.space_id}/conversations/{conversation_id}/messages"
        else:
            path = f"/api/2.0/genie/spaces/{self.space_id}/start-conversation"

        payload = {"content": message}
        resp = self._make_request("POST", path, data=payload)

        if not resp["ok"]:
            return None

        result = resp["data"]
        message_id = result.get("message_id") or result.get("id")
        message_data = result.get("message", {})

        if message_data:
            actual_conv_id = message_data.get("conversation_id")
            message_id = message_data.get("id") or message_id
        else:
            actual_conv_id = result.get("conversation_id", conversation_id)

        logger.info(f"Sent message {message_id} to conversation {actual_conv_id}")

        return {
            "id": message_id,
            "message_id": message_id,
            "conversation_id": actual_conv_id,
            "status": message_data.get("status") or result.get("status"),
            "content": message_data.get("content") or result.get("content"),
            "raw_response": result
        }

    def get_message_status(self, conversation_id: str, message_id: str) -> Optional[Dict[str, Any]]:
        """Get the status and response of a message."""
        path = f"/api/2.0/genie/spaces/{self.space_id}/conversations/{conversation_id}/messages/{message_id}"
        resp = self._make_request("GET", path)

        if not resp["ok"]:
            return None

        result = resp["data"]
        if "message" in result:
            return result["message"]
        return result

    def wait_for_response(
        self,
        conversation_id: str,
        message_id: str,
        max_wait_time: int = 60,
    ) -> Optional[Dict[str, Any]]:
        """Poll for a message response with exponential backoff."""
        start_time = time.time()
        attempt = 0

        while time.time() - start_time < max_wait_time:
            status = self.get_message_status(conversation_id, message_id)

            if status:
                state = status.get("status")
                if state == "COMPLETED":
                    logger.info(f"Message {message_id} completed")
                    return status
                elif state in ["FAILED", "CANCELLED"]:
                    logger.error(f"Message {message_id} failed with state: {state}")
                    return status
            else:
                # Transient failure - continue polling instead of giving up
                logger.warning(f"Transient error polling message {message_id}, retrying...")

            sleep_seconds = min(1 * (2 ** attempt), 10)
            time.sleep(sleep_seconds)
            attempt += 1

        logger.warning(f"Timeout waiting for message {message_id}")
        return None

    def get_statement_result(self, statement_id: str) -> Optional[Dict[str, Any]]:
        """Get the actual query result data from a SQL statement."""
        path = f"/api/2.0/sql/statements/{statement_id}"
        resp = self._make_request("GET", path)

        if not resp["ok"]:
            return None

        logger.info(f"Retrieved statement result for {statement_id}")
        return resp["data"]

    def wait_for_statement(
        self,
        statement_id: str,
        max_wait_time: int = 60,
    ) -> Optional[Dict[str, Any]]:
        """Poll a SQL statement until completion. Returns the full statement
        response on success, None on timeout or failure."""
        start_time = time.time()
        attempt = 0

        while time.time() - start_time < max_wait_time:
            result = self.get_statement_result(statement_id)

            if result:
                state = result.get("status", {}).get("state")
                if state == "SUCCEEDED":
                    logger.info(f"Statement {statement_id} succeeded")
                    return result
                elif state in ("FAILED", "CANCELED", "CLOSED"):
                    logger.error(f"Statement {statement_id} ended with state: {state}")
                    return result

            sleep_seconds = min(1 * (2 ** attempt), 10)
            time.sleep(sleep_seconds)
            attempt += 1

        logger.warning(f"Timeout waiting for statement {statement_id}")
        return None

    def execute_corrected_query(
        self,
        conversation_id: str,
        message_id: str,
        query: str,
    ) -> Optional[str]:
        """Execute a corrected SQL query via Genie's execute-query endpoint.
        Returns statement_id on success, None on failure."""
        path = (
            f"/api/2.0/genie/spaces/{self.space_id}"
            f"/conversations/{conversation_id}"
            f"/messages/{message_id}/execute-query"
        )
        resp = self._make_request("POST", path, data={"query": query})

        if not resp["ok"]:
            return None

        result = resp["data"]
        statement_id = (
            result.get("statement_response", {}).get("statement_id")
        )

        if statement_id:
            logger.info(f"Corrected query submitted: statement {statement_id}")

        return statement_id

    def ask_question(self, question: str, conversation_id: Optional[str] = None) -> Dict[str, Any]:
        """High-level method to ask a question and get the response."""
        message_result = self.send_message(conversation_id, question)
        if not message_result:
            return {
                "success": False,
                "conversation_id": conversation_id,
                "error": "Failed to send message to Genie API"
            }

        actual_conversation_id = message_result.get("conversation_id", conversation_id)
        message_id = message_result.get("id") or message_result.get("message_id")

        response = self.wait_for_response(actual_conversation_id, message_id)

        if not response:
            return {
                "success": False,
                "conversation_id": actual_conversation_id,
                "error": "Genie API response timeout"
            }

        status = response.get("status")

        if status == "COMPLETED":
            attachments = response.get("attachments", [])
            query_result = response.get("query_result")

            response_text = ""
            statement_id = None

            if attachments:
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
                    result_data = {
                        "data": statement_result.get("result", {}),
                        "schema": statement_result.get("manifest", {}).get("schema", {})
                    }

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
                "suggested_questions": suggested_questions
            }
        else:
            error_msg = response.get("error", {}).get("message", "Unknown error")
            return {
                "success": False,
                "conversation_id": actual_conversation_id,
                "error": f"Query failed: {error_msg}"
            }

    def send_message_feedback(
        self,
        conversation_id: str,
        message_id: str,
        rating: str,
    ) -> bool:
        """Send feedback (POSITIVE/NEGATIVE) for a message."""
        path = f"/api/2.0/genie/spaces/{self.space_id}/conversations/{conversation_id}/messages/{message_id}/feedback"

        resp = self._make_request("POST", path, data={"rating": rating.upper()})

        if resp["ok"]:
            logger.info(f"Sent {rating.upper()} feedback for message {message_id}")
            return True

        return False
