#!/usr/bin/env bash
# Starts final_demo.py in a Linux container with the scratch host of chat_tools_live.sh (same
# fixtures), the current working tree and frontend/dist, UI on http://127.0.0.1:8096.
#
#   scripts/dev/final_demo.sh [BASE_URL] [MODEL]      # runs until `docker rm -f asm-final-demo`
set -euo pipefail

BASE_URL=${1:-https://ollama.tcloud.monster/v1}
MODEL=${2:-gemma4:e4b}
REPO=$(cd "$(dirname "$0")/../.." && pwd)
WORK=$(mktemp -d)
export MSYS_NO_PATHCONV=1
win() { if command -v cygpath >/dev/null; then cygpath -m "$1"; else echo "$1"; fi; }

mkdir "$WORK/src"
(cd "$REPO" && tar --exclude=.venv --exclude=__pycache__ --exclude=.pytest_cache --exclude=.ruff_cache -cf - backend scripts/dev docs/samples/collectors-live.json) | tar -x -C "$WORK/src"
cp "$REPO/scripts/dev/fake_journal_export.py" "$WORK/gen.py"

# the scratch host: the setup part of chat_tools_live.sh's container script, unchanged
sed -n '/^cat > "\$WORK\/inner.sh" <<.EOF.$/,/^EOF$/p' "$REPO/scripts/dev/chat_tools_live.sh" | sed '1d;$d' | sed '/^cd \/tmp$/,$d' > "$WORK/inner.sh"
cat >> "$WORK/inner.sh" <<'EOF'
mkdir -p /repo/scripts /repo/docs
cp -a /src/scripts/dev /repo/scripts/dev
cp -a /src/docs/samples /repo/docs/samples
cp -a /backend-copy /repo/backend
pip install -q mcp 2>&1 | tail -1
cd /repo/backend
python /repo/scripts/dev/final_demo.py "$BASE_URL" "$MODEL" 8096
EOF
# (the original script installs from /backend and copies it to /backend-copy)
docker rm -f asm-final-demo >/dev/null 2>&1 || true
docker run -d --name asm-final-demo --cap-add SYS_ADMIN --security-opt apparmor=unconfined -p 8096:8096 \
  -e BASE_URL="$BASE_URL" -e MODEL="$MODEL" \
  -v "$(win "$WORK/src/backend"):/backend:ro" -v "$(win "$WORK/src"):/src:ro" \
  -v "$(win "$REPO/frontend/dist"):/frontend-dist:ro" \
  -v "$(win "$WORK/inner.sh"):/inner.sh:ro" -v "$(win "$WORK/gen.py"):/gen.py:ro" \
  python:3.13-slim bash /inner.sh
echo "started asm-final-demo; logs: docker logs -f asm-final-demo"
