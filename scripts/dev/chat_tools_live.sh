#!/usr/bin/env bash
# Live proof of the chat host tools: builds a scratch "host" inside a Linux container, then lets
# the real chat engine talk to a real LLM endpoint and use the tools against it.
#
#   scripts/dev/chat_tools_live.sh OUT.md [BASE_URL] [MODEL] ["question" ...]
#
# Needs Docker and network access to the LLM endpoint. The container gets SYS_ADMIN only to
# bind-mount /proc into the scratch root (the real deployment needs none of that).
set -euo pipefail

OUT=${1:?output file}
BASE_URL=${2:-https://ollama.tcloud.monster/v1}
MODEL=${3:-gemma4:e4b}
if [ $# -gt 3 ]; then
  shift 3
  QUESTIONS=("$@")
else
  QUESTIONS=(
    "What is /mnt/disks/disk14TB occupied with?"
    "Why is logrotate failing?"
    "What is listening on port 8080?"
  )
fi

REPO=$(cd "$(dirname "$0")/../.." && pwd)
WORK=$(mktemp -d)
cp "$REPO/scripts/dev/chat_tools_live.py" "$WORK/live.py"
cp "$REPO/scripts/dev/fake_journal_export.py" "$WORK/gen.py"
# the committed backend (other people's uncommitted edits stay out)
mkdir "$WORK/src"
git -C "$REPO" archive HEAD backend | tar -x -C "$WORK/src"
export MSYS_NO_PATHCONV=1
win() { if command -v cygpath >/dev/null; then cygpath -m "$1"; else echo "$1"; fi; }
cat > "$WORK/inner.sh" <<'EOF'
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null
apt-get install -y -qq --no-install-recommends systemd systemd-journal-remote iproute2 procps >/dev/null
cp -a /backend /backend-copy
pip install -q /backend-copy 2>&1 | tail -1

# ---- the scratch host: a copy of this container's system files plus fixtures
mkdir /fakehost
cp -a /usr /etc /fakehost/
for d in bin sbin lib lib64; do [ -e /$d ] && cp -a /$d /fakehost/ || true; done
mkdir -p /fakehost/proc && mount --bind /proc /fakehost/proc
echo 0123456789abcdef0123456789abcdef > /fakehost/etc/machine-id

D=/fakehost/mnt/disks/disk14TB
mkdir -p $D/media/movies $D/media/tv $D/downloads/incomplete $D/appdata/nextcloud $D/lost+found
head -c 220M /dev/zero | tr '\0' 'm' > $D/media/movies/Some.Movie.2021.1080p.mkv
head -c 90M  /dev/zero | tr '\0' 't' > $D/media/movies/Another.Movie.2019.mkv
head -c 130M /dev/zero | tr '\0' 'v' > $D/media/tv/Show.S01.mkv
head -c 70M  /dev/zero | tr '\0' 'd' > $D/downloads/incomplete/linux.iso.part
head -c 12M  /dev/zero | tr '\0' 'a' > $D/appdata/nextcloud/nextcloud.db
chmod -R a+rX $D

# logrotate: a broken config, a unit that failed, and its journal
cat > /fakehost/etc/logrotate.d/nginx <<'CONF'
/var/log/nginx/*.log {
    weekly
    rotate 8
    compress
    postrotate
        invoke-rc.d nginx rotate >/dev/null 2>&1
CONF
chmod a+r /fakehost/etc/logrotate.d/nginx
python /gen.py > /tmp/journal.export
mkdir -p /fakehost/var/log/journal/0123456789abcdef0123456789abcdef
/usr/lib/systemd/systemd-journal-remote -o /fakehost/var/log/journal/0123456789abcdef0123456789abcdef/system.journal - < /tmp/journal.export >/dev/null 2>&1
chgrp -R systemd-journal /fakehost/var/log/journal; chmod -R g+rX,o-rwx /fakehost/var/log/journal

# no systemd runs in this container: `systemctl` is a stand-in that prints what the real one would
mkdir -p /fakehost/usr/local/bin
cat > /fakehost/usr/local/bin/systemctl <<'STUB'
#!/bin/sh
case "$*" in
  *status*logrotate*)
cat <<TXT
x logrotate.service - Rotate log files
     Loaded: loaded (/lib/systemd/system/logrotate.service; static)
     Active: failed (Result: exit-code) since Sat 2026-11-14 00:00:02 UTC; 1 day ago
TriggeredBy: * logrotate.timer
       Docs: man:logrotate(8)
    Process: 1432 ExecStart=/usr/sbin/logrotate /etc/logrotate.conf (code=exited, status=1/FAILURE)
   Main PID: 1432 (code=exited, status=1/FAILURE)
TXT
  ;;
  *) echo "Unit not found." >&2; exit 4;;
esac
STUB
chmod a+rx /fakehost/usr/local/bin/systemctl

# something listening on 8080 (not visible by name to the unprivileged inspector)
python -m http.server 8080 --directory /tmp >/dev/null 2>&1 &
sleep 1

cd /tmp
python /live.py "$BASE_URL" "$MODEL" "$@"
EOF

docker run --rm --cap-add SYS_ADMIN --security-opt apparmor=unconfined \
  -e BASE_URL="$BASE_URL" -e MODEL="$MODEL" \
  -v "$(win "$WORK/src/backend"):/backend:ro" -v "$(win "$WORK/inner.sh"):/inner.sh:ro" -v "$(win "$WORK/live.py"):/live.py:ro" -v "$(win "$WORK/gen.py"):/gen.py:ro" \
  python:3.13-slim bash /inner.sh "${QUESTIONS[@]}" > "$OUT"
rm -rf "$WORK"
echo "wrote $OUT"
