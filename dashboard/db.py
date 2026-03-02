"""SQLite schema + helpers for runs and agents tables."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "dashboard.db"


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS runs (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id                TEXT UNIQUE,
                recorded_at           TEXT NOT NULL,
                agent_name            TEXT,
                model_name            TEXT,
                model_provider        TEXT,
                task_description      TEXT,
                status                TEXT NOT NULL,
                input_tokens          INTEGER DEFAULT 0,
                output_tokens         INTEGER DEFAULT 0,
                cache_read_tokens     INTEGER DEFAULT 0,
                cache_write_tokens    INTEGER DEFAULT 0,
                reasoning_tokens      INTEGER DEFAULT 0,
                cost_usd              REAL,
                duration_s            REAL,
                time_to_first_token_s REAL,
                llm_requests          INTEGER DEFAULT 0,
                tool_calls            INTEGER DEFAULT 0,
                output_text           TEXT,
                extra_json            TEXT
            );

            CREATE TABLE IF NOT EXISTS agents (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT UNIQUE NOT NULL,
                model         TEXT NOT NULL DEFAULT 'claude-sonnet-4-6',
                system_prompt TEXT,
                enabled       INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            );
        """)
        # Migrations: add columns if missing
        cols = [r[1] for r in conn.execute("PRAGMA table_info(agents)").fetchall()]
        if "tools" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN tools TEXT")
        if "agent_type" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN agent_type TEXT NOT NULL DEFAULT 'standard'")
        if "workspace" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN workspace TEXT")
        if "max_instances" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN max_instances INTEGER")
        conn.execute("UPDATE agents SET max_instances = 1 WHERE max_instances IS NULL")
        if "role" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN role TEXT")
        if "goal" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN goal TEXT")
        if "instructions" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN instructions TEXT")
        if "education" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN education TEXT")
        if "work_experience" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN work_experience TEXT")
        if "reflection" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN reflection INTEGER DEFAULT 0")
        if "enable_thinking_tool" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN enable_thinking_tool INTEGER DEFAULT 0")
        if "enable_reasoning_tool" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN enable_reasoning_tool INTEGER DEFAULT 0")
        if "reasoning_effort" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN reasoning_effort TEXT")
        if "thinking_budget" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN thinking_budget INTEGER")
        if "tool_call_limit" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN tool_call_limit INTEGER")
        # Runs table migrations
        run_cols = [r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()]
        if "workspace" not in run_cols:
            conn.execute("ALTER TABLE runs ADD COLUMN workspace TEXT")
        # Settings table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
