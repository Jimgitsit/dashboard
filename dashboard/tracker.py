"""track(result, agent_name, task) — synchronous, <1ms, never raises."""

import json
import traceback
from datetime import datetime, timezone

from .db import get_conn, init_db


def track(result, agent_name: str = None, task=None) -> None:
    """Record an AgentRunOutput to the runs table. Safe to call anywhere."""
    try:
        init_db()

        usage = getattr(result, "usage", None)
        task_desc = None
        if task is not None:
            task_desc = getattr(task, "description", str(task))

        row = {
            "run_id": getattr(result, "run_id", None),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "agent_name": agent_name,
            "model_name": getattr(result, "model_name", None),
            "model_provider": getattr(result, "model_provider", None),
            "task_description": task_desc,
            "status": _status(result),
            "input_tokens": _attr(usage, "input_tokens", 0),
            "output_tokens": _attr(usage, "output_tokens", 0),
            "cache_read_tokens": _attr(usage, "cache_read_tokens", 0),
            "cache_write_tokens": _attr(usage, "cache_write_tokens", 0),
            "reasoning_tokens": _attr(usage, "reasoning_tokens", 0),
            "cost_usd": _attr(usage, "cost", None),
            "duration_s": _attr(usage, "duration", None),
            "time_to_first_token_s": _attr(usage, "time_to_first_token", None),
            "llm_requests": _attr(usage, "requests", 0),
            "tool_calls": _attr(usage, "tool_calls", 0),
            "output_text": getattr(result, "output", None),
            "extra_json": None,
        }

        with get_conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO runs
                    (run_id, recorded_at, agent_name, model_name, model_provider,
                     task_description, status, input_tokens, output_tokens,
                     cache_read_tokens, cache_write_tokens, reasoning_tokens,
                     cost_usd, duration_s, time_to_first_token_s,
                     llm_requests, tool_calls, output_text, extra_json)
                VALUES
                    (:run_id, :recorded_at, :agent_name, :model_name, :model_provider,
                     :task_description, :status, :input_tokens, :output_tokens,
                     :cache_read_tokens, :cache_write_tokens, :reasoning_tokens,
                     :cost_usd, :duration_s, :time_to_first_token_s,
                     :llm_requests, :tool_calls, :output_text, :extra_json)
                """,
                row,
            )
    except Exception:
        traceback.print_exc()


def _status(result) -> str:
    s = getattr(result, "status", None)
    if s is None:
        return "unknown"
    if hasattr(s, "value"):
        return s.value
    return str(s)


def _attr(obj, name: str, default=None):
    if obj is None:
        return default
    return getattr(obj, name, default)
