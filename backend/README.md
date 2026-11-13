# servermon backend

Python backend for AI Server Monitor. See `../docs/DESIGN.md`.

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"   # Windows: .venv\Scripts\pip
pytest
ASM_CONFIG=../config.example.yaml python -m servermon
```
