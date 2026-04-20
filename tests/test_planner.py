"""Unit tests for SiliconFlowPlanner — all HTTP calls are mocked."""

import json
import pytest
from unittest.mock import MagicMock, patch
from agent_runtime.contracts import Goal
from agent_runtime.planner.llm import SiliconFlowPlanner


def make_goal():
    return Goal(
        id="goal-test",
        content="请创建 hello.txt，内容为 Hello",
        success_criteria="文件存在",
        parent_goal_id=None,
        root_goal_id="goal-test",
        session_id="session-test",
    )


def make_response_body(text: str) -> dict:
    return {
        "id": "msg_001",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }


def mock_http_response(body: dict, status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body
    resp.text = json.dumps(body)
    return resp


class TestSiliconFlowPlanner:
    def _planner(self):
        return SiliconFlowPlanner(api_key="sk-test", model="test-model")

    def test_success_returns_action_request(self):
        plan_json = json.dumps({"tool_id": "write_file", "params": {"path": "hello.txt", "content": "Hello"}})
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.post.return_value = mock_http_response(make_response_body(plan_json))

            planner = self._planner()
            action = planner(make_goal(), workspace=None)

        assert action.tool_id == "write_file"
        assert action.params["path"] == "hello.txt"
        assert action.params["content"] == "Hello"
        assert action.goal_ref == "goal-test"

    def test_strips_markdown_code_block(self):
        plan_json = "```json\n" + json.dumps({"tool_id": "write_file", "params": {"path": "x.txt", "content": "y"}}) + "\n```"
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.post.return_value = mock_http_response(make_response_body(plan_json))

            action = self._planner()(make_goal(), workspace=None)

        assert action.tool_id == "write_file"

    def test_non_json_raises_value_error(self):
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.post.return_value = mock_http_response(make_response_body("I can help you with that!"))

            with pytest.raises(ValueError, match="non-JSON"):
                self._planner()(make_goal(), workspace=None)

    def test_missing_tool_id_raises_value_error(self):
        bad = json.dumps({"params": {"path": "x.txt"}})
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.post.return_value = mock_http_response(make_response_body(bad))

            with pytest.raises(ValueError, match="missing 'tool_id'"):
                self._planner()(make_goal(), workspace=None)

    def test_http_500_raises_runtime_error(self):
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.post.return_value = mock_http_response({"error": "server error"}, status_code=500)

            with pytest.raises(RuntimeError, match="500"):
                self._planner()(make_goal(), workspace=None)

    def test_http_401_raises_runtime_error(self):
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_client.post.return_value = mock_http_response({"error": "unauthorized"}, status_code=401)

            with pytest.raises(RuntimeError, match="401"):
                self._planner()(make_goal(), workspace=None)
