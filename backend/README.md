# ai-server-monitor backend

Python backend for AI Server Monitor (package `ai_server_monitor`). See `../docs/DESIGN.md`.

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"   # Windows: .venv\Scripts\pip
pytest
ASM_DATA_DIR=./data python -m ai_server_monitor   # settings are edited in the web UI
```
