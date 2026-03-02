"""FastAPI: metrics + agent management routes."""

import asyncio
import json
import logging
import queue
import re
import sys
import threading
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Thread-local stdout capture (routes agent output to per-run queues)
# ---------------------------------------------------------------------------

_ANSI_ESCAPE = re.compile(r'\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')


class _ThreadedStdout:
    """sys.stdout proxy that routes per-thread writes to registered queues.

    Threads without a registered queue write through to the real stdout.
    Install once at module level: sys.stdout = _ThreadedStdout(sys.stdout)
    """

    def __init__(self, real):
        self._real = real
        self._lock = threading.Lock()
        self._queues: dict = {}   # thread_id -> queue.Queue
        self._buffers: dict = {}  # thread_id -> str (partial line accumulator)

    def register(self, q: queue.Queue) -> None:
        tid = threading.current_thread().ident
        with self._lock:
            self._queues[tid] = q
            self._buffers[tid] = ''

    def unregister(self) -> None:
        tid = threading.current_thread().ident
        with self._lock:
            q = self._queues.pop(tid, None)
            buf = self._buffers.pop(tid, '')
        # Flush any partial (no-newline) line remaining in the buffer
        if q is not None and buf:
            clean = _ANSI_ESCAPE.sub('', buf).strip()
            if clean:
                q.put(clean)

    def write(self, s: str) -> int:
        tid = threading.current_thread().ident
        with self._lock:
            q = self._queues.get(tid)
            if q is None:
                return self._real.write(s)
            self._buffers[tid] = self._buffers.get(tid, '') + s
            buf = self._buffers[tid]
            parts = buf.split('\n')
            self._buffers[tid] = parts[-1]  # keep the incomplete tail
            complete = parts[:-1]
        for line in complete:
            clean = _ANSI_ESCAPE.sub('', line).strip()
            if clean:
                q.put(clean)
        return len(s)

    def flush(self) -> None:
        self._real.flush()

    def __getattr__(self, name: str):
        return getattr(self._real, name)


_threaded_stdout = _ThreadedStdout(sys.stdout)
sys.stdout = _threaded_stdout

logger = logging.getLogger("upsonic.pm_watcher")

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .db import get_conn, init_db
from .tracker import track

TOOL_MAP = {}
try:
    from upsonic.tools import WebSearchTool, CodeExecutionTool  # type: ignore
    TOOL_MAP = {
        "WebSearchTool": WebSearchTool(),
        "CodeExecutionTool": CodeExecutionTool,
    }
except Exception:
    pass

app = FastAPI(title="Upsonic Dashboard")

# In-memory registry of running tasks (agent_id → {agent, task})
_running_tasks: dict = {}
_running_lock = threading.Lock()

STATIC_DIR = Path(__file__).parent / "static"

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def on_startup():
    init_db()
    asyncio.create_task(_pm_watcher_loop())


async def _pm_watcher_loop():
    """Background task: periodically runs the Project Manager's Trello check."""
    while True:
        try:
            with get_conn() as conn:
                def _s(k, default=None):
                    r = conn.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
                    return r["value"] if r else default

                enabled = _s("pm_poll_enabled", "0")
                if enabled != "1":
                    await asyncio.sleep(30)
                    continue

                interval_min = int(_s("pm_poll_interval", "15"))
                last_poll = _s("pm_last_poll", "")

            # Check if enough time has passed
            if last_poll:
                last_dt = datetime.fromisoformat(last_poll)
                elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
                if elapsed < interval_min * 60:
                    await asyncio.sleep(30)
                    continue

            logger.info("PM watcher: triggering scheduled Trello check")
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _run_pm_poll)

        except Exception:
            logger.exception("PM watcher error")
            await asyncio.sleep(60)


def _run_pm_poll():
    """Synchronously run the Project Manager agent for a scheduled Trello check."""
    import time
    try:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM agents WHERE name = 'Project Manager' AND enabled = 1"
            ).fetchone()
            last_poll_row = conn.execute(
                "SELECT value FROM settings WHERE key = 'pm_last_poll'"
            ).fetchone()
            last_poll = last_poll_row["value"] if last_poll_row else None
        if row is None:
            logger.warning("PM watcher: 'Project Manager' agent not found or disabled")
            return

        sys.path.insert(0, str(Path(__file__).parent.parent))
        from upsonic import Task  # type: ignore

        cfg = dict(row)
        agent_id = cfg["id"]

        if last_poll:
            scope = (
                f"This is an incremental check. The previous check ran at {last_poll} (UTC ISO-8601). "
                f"For each board, fetch board actions using since={last_poll} to get only activity "
                f"since the last run. Extract the unique card IDs from those actions and inspect "
                f"only those cards — skip cards with no activity since the last check. "
                f"If the Trello tool does not support a since parameter on actions, fetch cards and "
                f"filter by their dateLastActivity field, skipping any card whose dateLastActivity "
                f"is older than {last_poll}."
            )
        else:
            scope = "This is the first check — inspect all cards on all boards."

        task_text = (
            f"Scheduled Trello check. Only look at boards that belong to the Upsonic workspace. "
            f"Ignore any boards outside that workspace. {scope}\n\n"
            "For each card in scope, check for: new comments, list changes, description updates, "
            "overdue or approaching due dates, and any unanswered questions. "
            "Then take the appropriate workflow action:\n"
            "- Design doc card with a human green-light comment → break into task cards in 'To Do'\n"
            "- Task card in 'To Do' with no developer assigned → spawn Developer\n"
            "- Task card in 'In Review' (PR opened, awaiting review) → spawn Code Reviewer\n"
            "- Task card where Code Reviewer has approved → spawn Tester\n"
            "- Card with a technical question → spawn Architect to answer it\n"
            "- Card blocked on a human decision → leave a clear comment describing the blocker\n"
            "Do not spawn an agent for a card that already has a pending action in progress. "
            "Summarize every board you checked, every card you acted on, and what action you took."
        )

        temp_run_id = str(uuid.uuid4())
        wall_start = time.monotonic()
        with get_conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO runs (run_id, recorded_at, agent_name, task_description, status) VALUES (?, ?, ?, ?, ?)",
                (temp_run_id, datetime.now(timezone.utc).isoformat(), cfg["name"], task_text, "RUNNING"),
            )

        tools = _build_tools(cfg)
        tool_names = json.loads(cfg.get("tools") or "[]")
        if "SpawnAgents" in tool_names:
            tools.append(SpawnAgentsTool())
        agent = _instantiate_agent(cfg, tools)
        with _running_lock:
            _running_tasks[agent_id] = {"agent": agent, "task": task_text}

        succeeded = False
        try:
            task = Task(task_text)
            result = agent.do(task, return_output=True)
            track(result, agent_name=cfg["name"], task=task)
            wall_duration = time.monotonic() - wall_start
            real_run_id = getattr(result, "run_id", None)
            if real_run_id:
                with get_conn() as conn:
                    conn.execute("UPDATE runs SET duration_s = ? WHERE run_id = ?", (wall_duration, real_run_id))
            succeeded = True
        finally:
            with _running_lock:
                _running_tasks.pop(agent_id, None)
            with get_conn() as conn:
                if succeeded:
                    conn.execute("DELETE FROM runs WHERE run_id = ?", (temp_run_id,))
                else:
                    conn.execute("UPDATE runs SET status = 'FAILED' WHERE run_id = ?", (temp_run_id,))

        now = datetime.now(timezone.utc).isoformat()
        with get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('pm_last_poll', ?)", (now,)
            )
        logger.info("PM watcher: check complete")
    except Exception:
        logger.exception("PM watcher: error during poll")


# ---------------------------------------------------------------------------
# Static SPA
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def root():
    html = (STATIC_DIR / "index.html").read_text()
    return HTMLResponse(content=html)


# ---------------------------------------------------------------------------
# Metrics endpoints
# ---------------------------------------------------------------------------

@app.get("/api/summary")
def summary():
    with get_conn() as conn:
        row = conn.execute("""
            SELECT
                COUNT(*) AS total_runs,
                COALESCE(SUM(input_tokens + output_tokens), 0) AS total_tokens,
                COALESCE(SUM(cost_usd), 0.0) AS total_cost,
                COALESCE(AVG(duration_s), 0.0) AS avg_duration,
                ROUND(
                    100.0 * SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END)
                    / NULLIF(COUNT(*), 0), 1
                ) AS success_rate
            FROM runs
        """).fetchone()
    return dict(row)


@app.get("/api/daily")
def daily(days: int = 30):
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT
                DATE(recorded_at) AS day,
                SUM(input_tokens + output_tokens) AS tokens,
                SUM(cost_usd) AS cost,
                COUNT(*) AS runs
            FROM runs
            WHERE recorded_at >= DATE('now', :offset)
            GROUP BY day
            ORDER BY day
        """, {"offset": f"-{days} days"}).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/by_model")
def by_model():
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT
                COALESCE(model_name, 'unknown') AS model,
                COUNT(*) AS runs,
                SUM(input_tokens + output_tokens) AS tokens,
                COALESCE(SUM(cost_usd), 0.0) AS cost,
                COALESCE(AVG(duration_s), 0.0) AS avg_duration
            FROM runs
            GROUP BY model_name
            ORDER BY runs DESC
        """).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/runs")
def runs(limit: int = 50, offset: int = 0):
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT r.id, r.run_id, r.recorded_at, r.agent_name, r.model_name, r.task_description,
                   r.status, r.input_tokens, r.output_tokens, r.cost_usd, r.duration_s,
                   r.llm_requests, r.tool_calls,
                   a.id AS agent_id
            FROM runs r
            LEFT JOIN agents a ON a.name = r.agent_name
            ORDER BY r.recorded_at DESC
            LIMIT :limit OFFSET :offset
        """, {"limit": limit, "offset": offset}).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    return {"total": total, "rows": [dict(r) for r in rows]}


@app.get("/api/agent_perf")
def agent_perf():
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT
                COALESCE(agent_name, 'unknown') AS agent_name,
                COUNT(*) AS runs,
                COALESCE(AVG(duration_s), 0.0) AS avg_duration,
                ROUND(
                    100.0 * SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END)
                    / NULLIF(COUNT(*), 0), 1
                ) AS success_rate,
                COALESCE(SUM(cost_usd), 0.0) AS total_cost,
                MAX(recorded_at) AS last_run
            FROM runs
            GROUP BY agent_name
            ORDER BY runs DESC
        """).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Settings endpoints
# ---------------------------------------------------------------------------

class SettingsUpdate(BaseModel):
    data: dict


@app.post("/api/pm/poll")
async def trigger_pm_poll():
    """Manually trigger a Project Manager Trello check immediately."""
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _run_pm_poll)
    return {"ok": True, "message": "PM check triggered"}


@app.post("/api/server/restart")
async def restart_server():
    """Restart the dashboard server process using os.execv."""
    import os

    async def _do_restart():
        # Brief delay to allow the HTTP response to be sent before we replace the process
        await asyncio.sleep(0.5)
        os.execv(sys.executable, [sys.executable, "-m", "dashboard.run"])

    asyncio.create_task(_do_restart())
    return {"ok": True, "message": "Server is restarting…"}


@app.get("/api/settings")
def get_settings():
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


@app.put("/api/settings")
def update_settings(body: SettingsUpdate = Body(...)):
    with get_conn() as conn:
        for key, value in body.data.items():
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Agent management schemas
# ---------------------------------------------------------------------------


class AgentCreate(BaseModel):
    name: str
    model: str = "claude-sonnet-4-6"
    system_prompt: Optional[str] = None
    tools: Optional[list] = None
    agent_type: str = "standard"
    workspace: Optional[str] = None
    max_instances: Optional[int] = 1
    role: Optional[str] = None
    goal: Optional[str] = None
    instructions: Optional[str] = None
    education: Optional[str] = None
    work_experience: Optional[str] = None
    reflection: Optional[bool] = None
    enable_thinking_tool: Optional[bool] = None
    enable_reasoning_tool: Optional[bool] = None
    reasoning_effort: Optional[str] = None
    thinking_budget: Optional[int] = None
    tool_call_limit: Optional[int] = None


class AgentUpdate(BaseModel):
    name: Optional[str] = None
    model: Optional[str] = None
    system_prompt: Optional[str] = None
    enabled: Optional[int] = None
    tools: Optional[list] = None
    agent_type: Optional[str] = None
    workspace: Optional[str] = None
    max_instances: Optional[int] = None
    role: Optional[str] = None
    goal: Optional[str] = None
    instructions: Optional[str] = None
    education: Optional[str] = None
    work_experience: Optional[str] = None
    reflection: Optional[bool] = None
    enable_thinking_tool: Optional[bool] = None
    enable_reasoning_tool: Optional[bool] = None
    reasoning_effort: Optional[str] = None
    thinking_budget: Optional[int] = None
    tool_call_limit: Optional[int] = None


class RunTaskRequest(BaseModel):
    task: str


# ---------------------------------------------------------------------------
# Agent management endpoints
# ---------------------------------------------------------------------------

@app.get("/api/agents")
def list_agents():
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT a.id, a.name, a.model, a.system_prompt, a.tools, a.agent_type, a.workspace,
                   a.max_instances, a.enabled, a.created_at, a.updated_at,
                   a.role, a.goal, a.instructions, a.education, a.work_experience,
                   a.reflection, a.enable_thinking_tool, a.enable_reasoning_tool,
                   a.reasoning_effort, a.thinking_budget, a.tool_call_limit,
                   COUNT(r.id) AS run_count,
                   MAX(r.recorded_at) AS last_run
            FROM agents a
            LEFT JOIN runs r ON r.agent_name = a.name
            GROUP BY a.id
            ORDER BY a.name
        """).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/agents", status_code=201)
def create_agent(body: AgentCreate):
    now = datetime.now(timezone.utc).isoformat()
    tools_json = json.dumps(body.tools) if body.tools else None
    try:
        with get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO agents (name, model, system_prompt, tools, agent_type, workspace, max_instances, enabled, created_at, updated_at, "
                "role, goal, instructions, education, work_experience, reflection, enable_thinking_tool, enable_reasoning_tool, reasoning_effort, thinking_budget, tool_call_limit) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (body.name, body.model, body.system_prompt, tools_json,
                 body.agent_type, body.workspace or None, body.max_instances, now, now,
                 body.role, body.goal, body.instructions, body.education, body.work_experience,
                 int(body.reflection) if body.reflection is not None else None,
                 int(body.enable_thinking_tool) if body.enable_thinking_tool is not None else None,
                 int(body.enable_reasoning_tool) if body.enable_reasoning_tool is not None else None,
                 body.reasoning_effort, body.thinking_budget, body.tool_call_limit),
            )
            agent_id = cur.lastrowid
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
        return dict(row)
    except Exception as e:
        if "UNIQUE" in str(e):
            raise HTTPException(status_code=409, detail=f"Agent '{body.name}' already exists")
        raise HTTPException(status_code=500, detail=str(e))


@app.patch("/api/agents/{agent_id}")
def update_agent(agent_id: int, body: AgentUpdate):
    fields = {}
    if body.name is not None:
        fields["name"] = body.name
    if body.model is not None:
        fields["model"] = body.model
    if body.system_prompt is not None:
        fields["system_prompt"] = body.system_prompt
    if body.enabled is not None:
        fields["enabled"] = body.enabled
    if body.tools is not None:
        fields["tools"] = json.dumps(body.tools)
    if body.agent_type is not None:
        fields["agent_type"] = body.agent_type
    if body.workspace is not None:
        fields["workspace"] = body.workspace or None
    _fields_set = getattr(body, 'model_fields_set', None) or getattr(body, '__fields_set__', set())
    if "max_instances" in _fields_set:
        fields["max_instances"] = body.max_instances
    if body.role is not None:
        fields["role"] = body.role
    if body.goal is not None:
        fields["goal"] = body.goal
    if body.instructions is not None:
        fields["instructions"] = body.instructions
    if body.education is not None:
        fields["education"] = body.education
    if body.work_experience is not None:
        fields["work_experience"] = body.work_experience
    if body.reflection is not None:
        fields["reflection"] = int(body.reflection)
    if body.enable_thinking_tool is not None:
        fields["enable_thinking_tool"] = int(body.enable_thinking_tool)
    if body.enable_reasoning_tool is not None:
        fields["enable_reasoning_tool"] = int(body.enable_reasoning_tool)
    if body.reasoning_effort is not None:
        fields["reasoning_effort"] = body.reasoning_effort
    if "thinking_budget" in _fields_set:
        fields["thinking_budget"] = body.thinking_budget
    if "tool_call_limit" in _fields_set:
        fields["tool_call_limit"] = body.tool_call_limit
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")

    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [agent_id]

    with get_conn() as conn:
        conn.execute(f"UPDATE agents SET {set_clause} WHERE id = ?", values)
        row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return dict(row)


@app.delete("/api/agents/{agent_id}", status_code=204)
def delete_agent(agent_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))


@app.get("/api/agents/{agent_id}/runs")
def agent_runs(agent_id: int, limit: int = 50, offset: int = 0):
    with get_conn() as conn:
        agent = conn.execute("SELECT name FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if agent is None:
            raise HTTPException(status_code=404, detail="Agent not found")
        rows = conn.execute("""
            SELECT id, run_id, recorded_at, model_name, task_description,
                   status, input_tokens, output_tokens, cost_usd, duration_s,
                   llm_requests, tool_calls, output_text
            FROM runs
            WHERE agent_name = ?
            ORDER BY recorded_at DESC
            LIMIT ? OFFSET ?
        """, (agent["name"], limit, offset)).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE agent_name = ?", (agent["name"],)
        ).fetchone()[0]
    return {"total": total, "rows": [dict(r) for r in rows]}


@app.get("/api/agents/{agent_id}/stats")
def agent_stats(agent_id: int):
    with get_conn() as conn:
        agent = conn.execute("SELECT name FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if agent is None:
            raise HTTPException(status_code=404, detail="Agent not found")
        row = conn.execute("""
            SELECT
                COUNT(*) AS total_runs,
                COALESCE(SUM(input_tokens + output_tokens), 0) AS total_tokens,
                COALESCE(SUM(cost_usd), 0.0) AS total_cost,
                COALESCE(AVG(duration_s), 0.0) AS avg_duration,
                ROUND(
                    100.0 * SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END)
                    / NULLIF(COUNT(*), 0), 1
                ) AS success_rate
            FROM runs
            WHERE agent_name = ?
        """, (agent["name"],)).fetchone()
    return dict(row)


def _build_tools(agent_cfg: dict) -> list:
    """Build the tool list for an agent config dict."""
    tool_names = json.loads(agent_cfg.get("tools") or "[]")
    tools = [TOOL_MAP[t] for t in tool_names if t in TOOL_MAP]
    if "GitHub" in tool_names or "Jira" in tool_names or "Trello" in tool_names:
        from upsonic.tools import MCPHandler  # type: ignore
        from upsonic.tools.builtin_tools import MCPServerTool  # type: ignore
        with get_conn() as conn:
            def _s(k):
                r = conn.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
                return r["value"] if r else None
            gh_token = _s("github_token")
            gh_url = _s("github_mcp_url")
            jira_token = _s("jira_token")
            jira_url = _s("jira_mcp_url")
            trello_key = _s("trello_api_key")
            trello_token = _s("trello_token")
        if "GitHub" in tool_names and gh_token and gh_url:
            tools.append(MCPServerTool(id="github", url=gh_url, authorization_token=gh_token))
        if "Jira" in tool_names and jira_token and jira_url:
            tools.append(MCPServerTool(id="jira", url=jira_url, authorization_token=jira_token))
        if "Trello" in tool_names and trello_key and trello_token:
            tools.append(MCPHandler(
                command="/Users/doug/.bun/bin/bun x @delorenj/mcp-server-trello",
                env={"TRELLO_API_KEY": trello_key, "TRELLO_TOKEN": trello_token},
                transport="stdio",
            ))
    return tools


def _instantiate_agent(cfg: dict, tools: list):
    """Instantiate the right agent class based on cfg['agent_type']."""
    agent_type = cfg.get("agent_type") or "standard"
    kwargs = {"name": cfg["name"]}
    if cfg.get("system_prompt"):
        kwargs["system_prompt"] = cfg["system_prompt"]
    if tools:
        kwargs["tools"] = tools
    # High-value attributes
    for attr in ("role", "goal", "instructions", "education", "work_experience"):
        if cfg.get(attr):
            kwargs[attr] = cfg[attr]
    # Reasoning & quality
    if cfg.get("reflection"):
        kwargs["reflection"] = True
    if cfg.get("enable_thinking_tool"):
        kwargs["enable_thinking_tool"] = True
    if cfg.get("enable_reasoning_tool"):
        kwargs["enable_reasoning_tool"] = True
    if cfg.get("reasoning_effort"):
        kwargs["reasoning_effort"] = cfg["reasoning_effort"]
    if cfg.get("thinking_budget"):
        kwargs["thinking_budget"] = cfg["thinking_budget"]
    if cfg.get("tool_call_limit") is not None:
        kwargs["tool_call_limit"] = cfg["tool_call_limit"]
    model = cfg["model"]
    if agent_type == "autonomous":
        from upsonic import AutonomousAgent  # type: ignore
        # Callers must supply a fresh per-invocation workspace path via cfg["workspace"].
        workspace = cfg.get("workspace") or "."
        return AutonomousAgent(model, workspace=str(workspace), **kwargs)
    elif agent_type == "deep":
        from upsonic.agent import DeepAgent  # type: ignore
        return DeepAgent(model, **kwargs)
    else:
        from upsonic import Agent  # type: ignore
        return Agent(model, **kwargs)


# ---------------------------------------------------------------------------
# Per-invocation workspace helpers
# ---------------------------------------------------------------------------

_WORKSPACES_ROOT = Path(__file__).parent.parent / "workspaces"
# Match Trello card short-link (most reliable — it's always a card, never a board)
_TRELLO_SHORT_LINK_RE = re.compile(r'trello\.com/c/([A-Za-z0-9]+)')
# Match "Card ID:" or "(ID: <hex>)" patterns (card-specific context)
_TRELLO_CARD_ID_RE = re.compile(r'(?:Card\s+ID|Card:[^\n]*\(ID)[:\s]*([0-9a-f]{24})', re.IGNORECASE)


def _workspace_key(task: str) -> str | None:
    """Extract a stable per-card key from a task description, or None."""
    # Prefer the URL short-link — it's unambiguous and always a card
    m = _TRELLO_SHORT_LINK_RE.search(task)
    if m:
        return m.group(1)
    # Fall back to a card-ID-in-context pattern
    m = _TRELLO_CARD_ID_RE.search(task)
    if m:
        return m.group(1)
    return None


def _make_workspace(task: str = "") -> str:
    """Return the workspace path for this task, creating it if needed.

    Tasks with a Trello card ID get a stable workspace directory that persists
    across re-invocations so agents can resume where they left off.
    All other tasks get a fresh unique workspace each time.
    """
    _WORKSPACES_ROOT.mkdir(exist_ok=True)
    key = _workspace_key(task)
    ws = _WORKSPACES_ROOT / key if key else _WORKSPACES_ROOT / uuid.uuid4().hex[:12]
    ws.mkdir(exist_ok=True)
    return str(ws)


class SpawnAgentsTool:
    """Tool that lets an agent spawn Dev and Tester agents by name."""

    def __init__(self):
        self._spawn_counts: dict = {}

    def run_agent(self, agent_name: str, task: str) -> str:
        """Spawn a named agent to work on a task in the background.
        The agent runs independently — do not wait for it to finish.
        agent_name: Exact name of the agent to spawn — 'Architect', 'DevOps', 'Developer', 'Code Reviewer', or 'Tester'.
        task: Full task description including all context the agent needs.
        """
        import threading
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from upsonic import Task  # type: ignore

        with get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM agents WHERE name = ? AND enabled = 1", (agent_name,)
            ).fetchone()
        if row is None:
            return f"Error: agent '{agent_name}' not found or is disabled."

        cfg = dict(row)

        # Check if this agent is already running
        agent_id_spawned = cfg["id"]
        with _running_lock:
            if agent_id_spawned in _running_tasks:
                return f"Agent '{agent_name}' is already running. It will pick up further work on the next polling cycle."

        # Give autonomous agents a stable per-task workspace.
        workspace_path = None
        if cfg.get("agent_type") == "autonomous":
            workspace_path = _make_workspace(task)
            cfg["workspace"] = workspace_path

        tools = _build_tools(cfg)
        agent = _instantiate_agent(cfg, tools)
        t = Task(task)
        temp_run_id = str(uuid.uuid4())
        with get_conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO runs (run_id, recorded_at, agent_name, task_description, status, workspace) VALUES (?, ?, ?, ?, ?, ?)",
                (temp_run_id, datetime.now(timezone.utc).isoformat(), cfg["name"], task, "RUNNING", workspace_path),
            )
        with _running_lock:
            _running_tasks[agent_id_spawned] = {"agent": agent, "task": task}

        def _run():
            succeeded = False
            error_tb = None
            try:
                result = agent.do(t, return_output=True)
                track(result, agent_name=cfg["name"], task=t)
                # Stamp the real run record with the workspace path.
                real_run_id = getattr(result, "run_id", None)
                if real_run_id and workspace_path:
                    with get_conn() as conn:
                        conn.execute("UPDATE runs SET workspace = ? WHERE run_id = ?", (workspace_path, real_run_id))
                succeeded = True
            except Exception:
                error_tb = traceback.format_exc()
                logger.error("Spawned agent '%s' raised an exception:\n%s", agent_name, error_tb)
            finally:
                with _running_lock:
                    _running_tasks.pop(agent_id_spawned, None)
                with get_conn() as conn:
                    if succeeded:
                        conn.execute("DELETE FROM runs WHERE run_id = ?", (temp_run_id,))
                    else:
                        conn.execute(
                            "UPDATE runs SET status = 'FAILED', output_text = ? WHERE run_id = ?",
                            (error_tb, temp_run_id),
                        )

        threading.Thread(target=_run, daemon=True).start()
        return f"Agent '{agent_name}' spawned successfully and is running in the background."

    def cleanup_workspace(self, trello_card_url: str) -> str:
        """Delete the local workspace directory for a completed Trello card.
        Call this when a card is moved to 'Done'.
        trello_card_url: The Trello card URL (e.g. https://trello.com/c/XXXXX/...) or its short-link ID.
        """
        import shutil
        key = _workspace_key(trello_card_url)
        if not key:
            return f"Could not extract workspace key from: {trello_card_url}"
        ws = _WORKSPACES_ROOT / key
        if ws.exists():
            shutil.rmtree(ws)
            return f"Workspace '{key}' deleted."
        return f"Workspace '{key}' does not exist (already cleaned up)."


@app.post("/api/agents/{agent_id}/run")
async def run_agent_task(agent_id: int, body: RunTaskRequest):
    with get_conn() as conn:
        agent_row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if agent_row is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    if not agent_row["enabled"]:
        raise HTTPException(status_code=400, detail="Agent is disabled")

    agent_cfg = dict(agent_row)

    async def stream():
        import time
        loop = asyncio.get_event_loop()
        result_holder: dict = {}
        temp_run_id = str(uuid.uuid4())
        wall_start = time.monotonic()
        log_q: queue.Queue = queue.Queue()

        # Resolve workspace before inserting the RUNNING row so we can record it.
        workspace_path = None
        if agent_cfg.get("agent_type") == "autonomous":
            workspace_path = _make_workspace(body.task)

        # Insert RUNNING row immediately so it shows in the task list
        with get_conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO runs (run_id, recorded_at, agent_name, task_description, status, workspace) VALUES (?, ?, ?, ?, ?, ?)",
                (temp_run_id, datetime.now(timezone.utc).isoformat(), agent_cfg["name"], body.task, "RUNNING", workspace_path),
            )

        def run_sync():
            _threaded_stdout.register(log_q)
            try:
                sys.path.insert(0, str(Path(__file__).parent.parent))
                from upsonic import Task  # type: ignore

                cfg = dict(agent_cfg)
                if workspace_path:
                    cfg["workspace"] = workspace_path

                tools = _build_tools(cfg)
                tool_names = json.loads(cfg.get("tools") or "[]")
                if "SpawnAgents" in tool_names:
                    tools.append(SpawnAgentsTool())
                agent = _instantiate_agent(cfg, tools)

                # For the PM, expand vague polling requests into the full structured task
                task_text = body.task
                if agent_cfg.get("name") == "Project Manager":
                    _poll_keywords = {"poll", "check", "backlog", "trello", "workflow", "board"}
                    if any(kw in task_text.lower() for kw in _poll_keywords):
                        task_text = (
                            "Polling Mode: inspect every card on every project board. "
                            "This is your authorization to take ALL applicable workflow actions immediately — "
                            "do not ask for permission. For each card in scope, take the appropriate action:\n"
                            "- Task card in 'Backlog' or 'To Do' with no developer assigned → call run_agent('Developer', ...) now\n"
                            "- Task card in 'In Review' (PR opened) → call run_agent('Code Reviewer', ...) now\n"
                            "- Task card where Code Reviewer approved → call run_agent('Tester', ...) now\n"
                            "- Card with a technical question → call run_agent('Architect', ...) now\n"
                            "- Card blocked on a human decision → leave a comment describing the blocker\n"
                            "Do not announce what you are about to do. Call run_agent and then report what you did. "
                            "Do not spawn an agent for a card that already has a pending action in progress. "
                            "Summarize every board checked, every card acted on, and what action was taken."
                        )

                with _running_lock:
                    _running_tasks[agent_id] = {"agent": agent, "task": task_text}
                task = Task(task_text)
                result = agent.do(task, return_output=True)
                track(result, agent_name=agent_cfg["name"], task=task)
                # Stamp workspace onto the real run record created by track().
                real_run_id = getattr(result, "run_id", None)
                if real_run_id and workspace_path:
                    with get_conn() as conn:
                        conn.execute("UPDATE runs SET workspace = ? WHERE run_id = ?", (workspace_path, real_run_id))
                result_holder["result"] = result
                result_holder["error"] = None
            except Exception as e:
                result_holder["result"] = None
                result_holder["error"] = str(e)
                result_holder["traceback"] = traceback.format_exc()
            finally:
                _threaded_stdout.unregister()
                with _running_lock:
                    _running_tasks.pop(agent_id, None)
                with get_conn() as conn:
                    if result_holder.get("error"):
                        conn.execute("UPDATE runs SET status = 'FAILED' WHERE run_id = ?", (temp_run_id,))
                    else:
                        conn.execute("DELETE FROM runs WHERE run_id = ?", (temp_run_id,))

        _POLL = 0.5
        future = loop.run_in_executor(None, run_sync)
        elapsed = 0.0
        last_heartbeat = 0.0
        while True:
            try:
                await asyncio.wait_for(asyncio.shield(future), timeout=_POLL)
                break
            except asyncio.TimeoutError:
                elapsed += _POLL
                lines: list = []
                try:
                    while True:
                        lines.append(log_q.get_nowait())
                except queue.Empty:
                    pass
                if lines:
                    yield json.dumps({"type": "log", "lines": lines, "elapsed_s": round(elapsed, 1)}) + "\n"
                elif elapsed - last_heartbeat >= 5.0:
                    last_heartbeat = elapsed
                    yield json.dumps({"type": "progress", "event_kind": "heartbeat", "elapsed_s": int(elapsed)}) + "\n"

        # Drain any remaining lines buffered after run_sync finishes
        remaining: list = []
        try:
            while True:
                remaining.append(log_q.get_nowait())
        except queue.Empty:
            pass
        if remaining:
            yield json.dumps({"type": "log", "lines": remaining, "elapsed_s": round(elapsed, 1)}) + "\n"

        wall_duration = time.monotonic() - wall_start

        if result_holder.get("error"):
            yield json.dumps({
                "type": "error",
                "message": result_holder["error"],
                "traceback": result_holder.get("traceback", ""),
            }) + "\n"
            return

        result = result_holder.get("result")
        if result is None:
            yield json.dumps({"type": "error", "message": "Agent returned no result"}) + "\n"
            return

        usage = getattr(result, "usage", None)

        def _u(name, default=None):
            return getattr(usage, name, default) if usage else default

        # Update the tracked row with the real wall-clock duration
        real_run_id = getattr(result, "run_id", None)
        if real_run_id:
            with get_conn() as conn:
                conn.execute("UPDATE runs SET duration_s = ? WHERE run_id = ?", (wall_duration, real_run_id))

        yield json.dumps({
            "type": "output",
            "output": getattr(result, "output", ""),
            "status": _status_str(result),
            "model_name": getattr(result, "model_name", None),
            "model_provider": getattr(result, "model_provider", None),
            "input_tokens": _u("input_tokens", 0),
            "output_tokens": _u("output_tokens", 0),
            "cost_usd": _u("cost"),
            "duration_s": wall_duration,
            "llm_requests": _u("requests", 0),
            "tool_calls": _u("tool_calls", 0),
        }) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@app.post("/api/agents/{agent_id}/cancel")
def cancel_agent_task(agent_id: int):
    with _running_lock:
        entry = _running_tasks.get(agent_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="No running task for this agent")
    try:
        entry["agent"].cancel_run()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Cancel failed: {e}")
    return {"ok": True, "message": "Cancellation requested"}


@app.get("/api/agents/{agent_id}/running")
def agent_running_status(agent_id: int):
    with _running_lock:
        entry = _running_tasks.get(agent_id)
    if entry:
        return {"running": True, "task": entry["task"]}
    return {"running": False, "task": None}


def _status_str(result) -> str:
    s = getattr(result, "status", None)
    if s is None:
        return "unknown"
    if hasattr(s, "value"):
        return s.value
    return str(s)
