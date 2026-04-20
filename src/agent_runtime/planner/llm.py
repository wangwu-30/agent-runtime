"""
SiliconFlow LLM Planner

将 Goal 转换为 ActionRequest，通过 SiliconFlow API 调用 LLM 进行规划。
API 文档: https://docs.siliconflow.cn/cn/api-reference/chat-completions/messages
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

from agent_runtime.contracts import ActionRequest, Goal

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
你是一个任务规划器。根据用户目标，选择合适的工具并返回 JSON。

可用工具：
- write_file: 写入文件到 workspace。参数: {"path": "相对路径（不含 workspace 前缀）", "content": "文件内容"}

规则：
1. 只返回一个 JSON 对象，不要有任何其他文字、代码块标记或解释。
2. 格式必须严格为：{"tool_id": "write_file", "params": {"path": "...", "content": "..."}}
3. path 使用相对路径，不要以 / 开头。
"""


class SiliconFlowPlanner:
    """通过 SiliconFlow API 将 Goal 规划为 ActionRequest。"""

    def __init__(
        self,
        api_key: str,
        model: str = "Pro/MiniMaxAI/MiniMax-M2.5",
        base_url: str = "https://api.siliconflow.cn/v1",
        timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def __call__(self, goal: Goal, workspace: Any) -> ActionRequest:
        user_content = goal.content
        if goal.success_criteria:
            user_content += f"\n\n成功条件：{goal.success_criteria}"

        payload: Dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "user", "content": user_content},
            ],
            "system": _SYSTEM_PROMPT,
            "max_tokens": 512,
            "temperature": 0.1,
        }

        logger.debug("SiliconFlowPlanner: calling %s with goal=%s", self._model, goal.id)

        with httpx.Client(timeout=self._timeout) as client:
            response = client.post(
                f"{self._base_url}/messages",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )

        if response.status_code != 200:
            raise RuntimeError(
                f"SiliconFlow API error {response.status_code}: {response.text}"
            )

        data = response.json()
        raw_text = self._extract_text(data)
        logger.debug("SiliconFlowPlanner: raw response text: %s", raw_text)

        plan = self._parse_plan(raw_text)

        return ActionRequest(
            id=f"act-{uuid.uuid4().hex[:12]}",
            tool_id=plan["tool_id"],
            params=plan["params"],
            goal_ref=goal.id,
            span_id=f"span-{goal.id}",
            resources=[],
        )

    def _extract_text(self, data: Dict[str, Any]) -> str:
        try:
            content = data["content"]
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        return block["text"].strip()
            if isinstance(content, str):
                return content.strip()
        except (KeyError, IndexError, TypeError):
            pass
        raise ValueError(f"Unexpected API response structure: {data}")

    def _parse_plan(self, text: str) -> Dict[str, Any]:
        # 去除可能的 markdown 代码块包裹
        cleaned = text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            cleaned = "\n".join(
                line for line in lines if not line.startswith("```")
            ).strip()

        try:
            plan = json.loads(cleaned)
        except json.JSONDecodeError as e:
            raise ValueError(f"LLM returned non-JSON response: {text!r}") from e

        if "tool_id" not in plan:
            raise ValueError(f"LLM response missing 'tool_id': {plan}")
        if "params" not in plan:
            raise ValueError(f"LLM response missing 'params': {plan}")

        return plan
