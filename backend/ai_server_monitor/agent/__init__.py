"""The agent runtime: one loop (Pydantic AI) for chat, delegated sub-tasks and jobs, the
read-only host shell, and the two model profiles (main, worker)."""

import os

# Pydantic AI prints an advertising banner on the first run unless told not to.
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
