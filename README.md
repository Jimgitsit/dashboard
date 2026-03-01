# Dashboard

The last dashboard you'll ever need. A web dashboard for managing and orchestrating a team of AI agents built on the [Upsonic](https://upsonic.ai) framework. Designed around a human-in-the-loop workflow where you define agents, kick off tasks, and monitor results — while the agents coordinate among themselves.

## Features

- **Agent management** — create and configure agents with model selection, tools, roles, goals, and instructions
- **Multi-agent orchestration** — a Project Manager agent can spawn specialized sub-agents (Architect, Developer, Code Reviewer, Tester, DevOps) via the `SpawnAgents` tool
- **Tool integrations** — GitHub, Jira, Trello (via MCP), web search, and code execution
- **Run history** — full log of every task run with token usage, cost, duration, and output
- **Metrics** — token and cost charts by day and by model, per-agent performance stats
- **Scheduled polling** — the Project Manager can be configured to check Trello on a timer and take autonomous workflow actions
- **Streaming execution** — run any agent from the UI with live heartbeat feedback

## Stack

- **Backend**: FastAPI + SQLite (WAL mode)
- **Frontend**: Single-file SPA (`dashboard/static/index.html`)
- **Agent runtime**: [Upsonic](https://upsonic.ai)
- **Server**: Uvicorn, managed via launchd on macOS

## Setup

### Prerequisites

- Python 3.11+
- An [Anthropic API key](https://console.anthropic.com)
- Upsonic installed in your environment

### Install

```bash
git clone https://github.com/Jimgitsit/dashboard.git
cd dashboard
python -m venv .venv
source .venv/bin/activate
pip install upsonic fastapi uvicorn python-dotenv
```

### Configure

Create a `.env` file in the project root:

```
ANTHROPIC_API_KEY=your_key_here
```

External tool credentials (GitHub, Jira, Trello) are configured from the Settings page in the UI after the server is running.

### Run

```bash
python -m dashboard.run
```

The dashboard will be available at [http://127.0.0.1:8765](http://127.0.0.1:8765).

### Run as a macOS service

To run the server automatically at login, create a launchd plist at `~/Library/LaunchAgents/dashboard.plist`. Adjust the paths to match your install location:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>local.dashboard</string>
    <key>ProgramArguments</key>
    <array>
        <string>/path/to/dashboard/.venv/bin/python3</string>
        <string>-m</string>
        <string>dashboard.run</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/path/to/dashboard</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/path/to/dashboard/dashboard.log</string>
    <key>StandardErrorPath</key>
    <string>/path/to/dashboard/dashboard.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/path/to/dashboard/.venv/bin:/usr/local/bin:/usr/bin:/bin</string>
        <key>VIRTUAL_ENV</key>
        <string>/path/to/dashboard/.venv</string>
    </dict>
</dict>
</plist>
```

Then load it:

```bash
launchctl load ~/Library/LaunchAgents/dashboard.plist
```

## Project structure

```
dashboard/
├── api.py          # FastAPI routes + agent execution engine
├── db.py           # SQLite schema and connection helper
├── run.py          # Uvicorn entrypoint
├── tracker.py      # Records run results to the database
└── static/
    └── index.html  # Single-file SPA
setup_agents.py     # Script to seed the default workflow agent team
workflow.svg        # Diagram of the multi-agent workflow
```

## Workflow agents

`setup_agents.py` seeds a ready-to-use software development team:

| Agent | Role |
|---|---|
| Project Manager | Coordinates the team, manages Trello, spawns agents |
| Architect | Reviews design docs, answers technical questions |
| DevOps | Provisions GitHub repos and CI/CD |
| Developer | Implements task cards, opens PRs |
| Code Reviewer | Reviews PRs for correctness, security, and quality |
| Tester | Validates implementations and merges approved PRs |

Run it against a live dashboard to create or update all agents:

```bash
python setup_agents.py
```
