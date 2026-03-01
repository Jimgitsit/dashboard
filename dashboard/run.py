"""Launcher: python -m dashboard.run"""

import uvicorn
from dotenv import load_dotenv
from .db import init_db

load_dotenv()

if __name__ == "__main__":
    init_db()
    uvicorn.run(
        "dashboard.api:app",
        host="0.0.0.0",
        port=8765,
        reload=False,
    )
