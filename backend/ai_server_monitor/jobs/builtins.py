# ruff: noqa: E501  (prompt text: the commands are quoted whole)
"""The predefined jobs and summaries, seeded into the `jobs` table. A builtin the user never
edited follows changes made here in later releases (prompt, schedule, name, a summary's
members); an edited one keeps the user's version until "reset to default".

Each job checks one narrow thing, written for a small local model: the usual commands and what
ok / warn / crit mean for it. The jobs have no schedule of their own: the summaries run them
one after another and then write the overall picture."""

from __future__ import annotations

from dataclasses import dataclass

from ..agent.prompts import (
    DOCKER_PS,
    DOCKER_STATS,
    DU,
    LOADAVG,
    LOADAVG_FIELDS,
    THERMAL,
    UNIT_LOG,
    VARLINK_FAILED,
    VARLINK_STATE,
)


@dataclass(frozen=True)
class JobDef:
    slug: str
    name: str
    kind: str
    model: str
    prompt: str
    interval_s: int | None = None
    cron: str | None = None
    max_steps: int | None = None
    origin: str | None = None
    """The 0.9.1 builtin this job came out of: its MCP domains are copied on the upgrade."""
    members: tuple[str, ...] = ()
    """A summary's jobs, in the order they run."""
    notify: str = "warn_crit"
    """Telegram: always, changed, warn_crit or never (set once when seeded; not part of the
    definition)."""


HOUR = 3600

DOCKER_UPDATES = (
    'h=${DOCKER_HOST:-unix:///var/run/docker.sock}; s=; b=$h; case "$h" in '
    "tcp://*) b=http://${h#tcp://};; unix://*) s=${h#unix://}; b=http://localhost;; esac; "
    "for i in $(docker ps --format '{{.Image}}' | sort -u); do "
    'l=$(docker image inspect --format \'{{join .RepoDigests " "}}\' "$i" 2>/dev/null); '
    'r=$(curl -sS ${s:+--unix-socket "$s"} "$b/distribution/$i/json" 2>&1); '
    'd=$(echo "$r" | sed -n \'s/.*"Descriptor":{[^}]*"digest":"\\(sha256:[0-9a-f]*\\)".*/\\1/p\'); '
    'case "$d" in sha256:*) case "$l" in *"$d"*) echo "current  $i";; *) echo "newer    $i";; esac;; '
    '*) echo "unknown  $i: $(echo "$r" | head -1)";; esac; done'
    ' | awk \'{n[$1]++; g[$1] = g[$1] $0 "\\n"} '
    'END {printf "%s%s%s", g["newer"], g["unknown"], g["current"]; '
    'printf "counts: %d current, %d newer, %d unknown\\n", n["current"], n["newer"], n["unknown"]}\''
)
"""One line per image of a running container: "current", "newer" (the registry has another
digest for its tag) or "unknown" with the first line of the answer; the "newer" lines first, then
the "unknown", then the "current", and last one line with the three counts (so the model copies
them instead of counting a long list). The registry's digest comes
from the Docker daemon (Engine API `GET /distribution/<image>/json`, `Descriptor.digest`), asked
with curl at the same $DOCKER_HOST the `docker` commands use: `tcp://` as http, `unix://` through
the socket, unset as Docker's own default socket. Nothing is written on the host."""

JOURNAL_COUNTS = "sort | uniq -c | sort -rn"


def _topic(slug: str, name: str, origin: str, prompt: str) -> JobDef:
    return JobDef(slug=slug, name=name, kind="topic", model="worker", prompt=prompt, origin=origin)


TOPICS: tuple[JobDef, ...] = (
    _topic(
        "docker:status",
        "Docker: status",
        "docker",
        f"""\
Containers that are not running as they should.
Command (one call; do not inspect containers one by one):
- `{DOCKER_PS}`
  (Status shows "Up", "Restarting", "(unhealthy)", "Exited (code)" and "Created")
ok: the running containers are up, and healthy where they have a healthcheck.
warn: a container that is restarting, unhealthy, or exited with a non-zero code recently.
crit: an important container (database, reverse proxy, the main services) down, restarting
or unhealthy.
Copy the Status column exactly as printed. "Created" means the container was created but never
started: it is not "Exited" and has no exit code; mention such containers only as one info
finding. Containers that exited with code 0 are one-off tasks: ignore them.""",
    ),
    _topic(
        "docker:issues",
        "Docker: issues",
        "docker",
        """\
Trouble inside the containers: errors in their logs, restarts, out-of-memory kills.
Commands:
- restarts, OOM kills and exit codes of all containers in one call:
  `docker inspect --format '{{.Name}} restarts={{.RestartCount}} oom_killed={{.State.OOMKilled}} exit={{.State.ExitCode}}' $(docker ps -aq)`
- containers with error lines in the last hour:
  `for c in $(docker ps --format '{{.Names}}'); do n=$(docker logs --since 1h "$c" 2>&1 | grep -ciE 'error|fatal|panic|exception'); [ "$n" -gt 0 ] && echo "$n $c"; done | sort -rn | head -10`
- only for the two containers with the most error lines: `docker logs --since 1h NAME 2>&1 | grep -iE 'error|fatal|panic|exception' | tail -15`
ok: no restarts, no OOM kills, no or only a few one-off error lines.
warn: a container with restarts, or one logging the same error again and again.
crit: a container killed for lack of memory (oom_killed=true).
Quote the error lines short, with the container name and how often they occurred.""",
    ),
    _topic(
        "docker:resources",
        "Docker: resources",
        "docker",
        f"""\
CPU and memory of each container, and the disk space Docker uses.
Commands:
- `{DOCKER_STATS}`
- `docker system df`
ok: every container below 90 % of its memory limit.
warn: a container above 90 % of its memory limit.
This check has no crit.
Name the containers that use the most CPU and memory, with the numbers as printed. From
`docker system df`, copy the size and the reclaimable space of images, containers, local
volumes and build cache; much reclaimable space is one info finding.""",
    ),
    _topic(
        "docker:updates",
        "Docker: image updates",
        "docker",
        f"""\
Newer images for the running containers.
Run this ONE command; it prints one line per image, the "newer" and "unknown" ones first, and
as its last line the counts ("counts: 32 current, 5 newer, 4 unknown"):
`{DOCKER_UPDATES}`
"current": the registry has the same image. "newer": the registry has a different image for
that tag (an update). "unknown": the registry could not be asked (the error follows; images
built locally always show this). An answer "403 Forbidden" comes from the Docker proxy: its
service in the compose file lacks the line `DISTRIBUTION: 1`; say so in the finding.
ok: always, unless no image could be checked at all (then: could not check, with the first
error line).
Findings only for the "newer" and "unknown" lines: each "newer" image is one info finding
naming the image; each "unknown" image one info finding with its error. The "current" lines
need nothing. For the summary, take the counts from the last line and copy them as printed;
do not count the lines yourself.""",
    ),
    _topic(
        "resources:load",
        "Resources: load",
        "resources",
        f"""\
CPU load, memory, swap and temperatures right now, and the processes that use the most.
Commands:
- `nproc; {LOADAVG}; free -m` — /proc/loadavg is: {LOADAVG_FIELDS}.
  Name each load by its own position.
- `ps -eo pid,user,comm,%cpu,%mem --sort=-%cpu | head -6; ps -eo pid,user,comm,%cpu,%mem --sort=-%mem | head -6`
- temperatures: `sensors` when it exists; otherwise
  `{THERMAL}`
  (the number is thousandths of °C: 54000 means 54 °C; name the zone type)
ok: 15-minute load below the number of cores, more than 10 % memory available, swap mostly
unused, temperatures below 85 °C.
warn: 15-minute load 1-2 times the cores, less than 10 % memory available, swap over 50 %
used, a temperature at 85-95 °C.
crit: less than 3 % memory available, load above 2 times the cores, any temperature at 95 °C
or more.
Name the processes that use the most CPU and memory.""",
    ),
    _topic(
        "resources:gpu",
        "Resources: GPU",
        "resources",
        """\
GPU load, video memory and temperature.
Command:
- `nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu --format=csv`
When nvidia-smi is not found, the host has no NVIDIA GPU: finish at once with status ok and
the summary "No GPU found (nvidia-smi is not installed)." Do not try other commands.
ok: every GPU below 85 °C.
warn: a GPU at 85-95 °C.
crit: a GPU at 95 °C or more.
Copy the name, load, memory used of total and temperature of each GPU.""",
    ),
    _topic(
        "storage:usage",
        "Storage: usage",
        "storage",
        f"""\
Disk space and inodes of the host's real filesystems.
Commands:
- `df -hT -x tmpfs -x devtmpfs -x overlay -x squashfs -x efivarfs`
- `df -i -x tmpfs -x devtmpfs -x overlay -x squashfs -x efivarfs`
- only for a filesystem at 85 % or more, its biggest directories (short output):
  `{DU.replace("DIR", "MOUNT")}` (pass timeout_s=600)
ok: every filesystem below 85 % of space and of inodes.
warn: a filesystem at 85-95 %.
crit: a filesystem at 95 % or more, or less than 5 GB free on `/`.
For each filesystem at 85 % or more: the mount point, the percentage and the free space from
its df line, and its two biggest directories: `sort -h` puts the biggest last, and the very
last du line is the whole mount (its total, not a directory), so copy the TWO LINES JUST ABOVE
the last line as they are (e.g. "650G .../downloads", "11T .../media"). The disk's size and
use are only in df: never mix them.""",
    ),
    _topic(
        "journal:errors",
        "Journal: errors",
        "journal",
        f"""\
Errors that services logged to the system journal in the last hour.
Run this ONE command first; it prints every section you need:
`echo '== errors by message =='; journalctl -p err -S -1h --no-pager -o cat | {JOURNAL_COUNTS} | head -20; echo '== latest errors (unit, time) =='; journalctl -p err -S -1h --no-pager -o short-iso | tail -25; echo '== rate-limited messages =='; journalctl -S -1h --no-pager -o cat | grep -E 'callbacks suppressed|rate.?limit|Suppressed [0-9]+ messages' | sed -E 's/[0-9]+/N/g' | {JOURNAL_COUNTS} | head -5`
A second command only to follow up on something it showed (e.g. `journalctl -u UNIT -n 30`);
never run variants of the same journalctl again. Kernel messages and logins have their own
checks: leave them out.
ok: no errors, or only a few one-off messages.
warn: one service logging the same error again and again, or errors that point to a failing
component or a broken configuration.
This check has no crit (kernel and hardware errors are the kernel check's).
Every line of the "rate-limited messages" section is a finding (info, or warn above 100),
never "normal". Example: the line `    238 kauditd_printk_skb: N callbacks suppressed` gives the
info finding "Kernel audit messages rate-limited (238 times in the last hour)" with that line
as evidence. The same holds for one message repeated many times.
Quote the important lines short, with the unit name and how often they occurred.""",
    ),
    _topic(
        "journal:kernel",
        "Journal: kernel",
        "journal",
        f"""\
Kernel warnings and errors of the last hour: out-of-memory kills, disk and hardware errors,
crashed programs.
Run this ONE command:
`echo '== kernel warnings by message =='; journalctl -k -p warning -S -1h --no-pager -o cat | {JOURNAL_COUNTS} | head -15; echo '== latest (time) =='; journalctl -k -p warning -S -1h --no-pager -o short-iso | tail -15`
ok: no kernel warnings, or only a few harmless one-off messages.
warn: the same kernel warning again and again.
crit: out-of-memory kills ("Out of memory", "oom-kill"), disk or filesystem I/O errors,
hardware errors (MCE, EDAC, ata errors), segfaults of system services.
Quote the lines short, with how often they occurred.""",
    ),
    _topic(
        "journal:auth",
        "Journal: logins",
        "journal",
        f"""\
Logins and failed login attempts of the last hour (SSH, sudo, su).
Run this ONE command:
`echo '== failed attempts by message =='; journalctl -S -1h --no-pager -o cat SYSLOG_FACILITY=4 SYSLOG_FACILITY=10 | grep -iE 'failed|invalid user|authentication failure|not in sudoers' | sed -E 's/port [0-9]+//' | {JOURNAL_COUNTS} | head -15; echo '== successful logins =='; journalctl -S -1h --no-pager -o short-iso SYSLOG_FACILITY=4 SYSLOG_FACILITY=10 | grep -iE 'Accepted (password|publickey)|session opened for user root' | tail -15`
ok: no failed attempts, or only a few.
warn: many failed attempts from one address or for one user, a refused sudo ("not in
sudoers").
crit: a successful login from an address that failed many times just before.
Name the addresses, the user names and how often, copied from the lines.""",
    ),
    _topic(
        "systemd:failed",
        "Systemd: failed units",
        "systemd",
        f"""\
Failed systemd units. `systemctl` does not work in this shell: do not try it.
Commands (systemd's Varlink socket answers read-only):
- system state and number of failed units:
  `{VARLINK_STATE}`
- the failed units:
  `{VARLINK_FAILED}`
- why one failed (at most three units): `{UNIT_LOG}`
If varlinkctl is missing or fails (systemd older than 257), use instead:
  `journalctl _PID=1 -p warning -S -24h --no-pager -o cat | grep -i 'failed' | {JOURNAL_COUNTS} | head -15`
ok: no failed units, the system is "running".
warn: failed units that are not essential (one-shot jobs, a timer's service), the system is
"degraded".
crit: a failed essential service: ssh, networking, docker or containerd, cron, a mount of a
data disk.
One finding per failed unit: the title names the unit, the detail gives the reason in a few
words copied from its journal error line (for example the "error:" text), not just the name.""",
    ),
    _topic(
        "systemd:timers",
        "Systemd: timers",
        "systemd",
        """\
Whether the systemd timers ran, and whether the services they start failed.
`systemctl` does not work in this shell: do not try it.
Run this ONE command:
`echo '== enabled timers =='; ls /etc/systemd/system/timers.target.wants/ /usr/lib/systemd/system/timers.target.wants/ 2>/dev/null; echo '== last run of persistent timers =='; ls -l --time-style=+%F_%H:%M /var/lib/systemd/timers/ 2>/dev/null; echo '== failed runs, last 24 h =='; journalctl _PID=1 -p warning -S -24h --no-pager -o short-iso | grep -i 'failed with result' | tail -15`
ok: no failed runs in the last 24 hours.
warn: a timer's service failed in the last 24 hours, or a persistent timer whose last run is
much older than its own schedule (a daily timer that has not run for days).
This check has no crit (failed essential services are the failed-units check's).
Name each timer or service and copy its line.""",
    ),
    _topic(
        "updates:packages",
        "Updates: packages",
        "updates",
        """\
Pending package updates.
Run this ONE command (Debian/Ubuntu); it prints every fact you need in labelled sections:
`echo '== upgradable (count) =='; apt list --upgradable 2>/dev/null | tail -n +2 | wc -l; echo '== security updates (count) =='; apt list --upgradable 2>/dev/null | grep -ci security; echo '== security updates =='; apt list --upgradable 2>/dev/null | grep -i security | head -30; echo '== package lists updated =='; stat -c %y /var/cache/apt/pkgcache.bin`
A second command only to follow up on something it showed. Put the two counts (upgradable,
security) in the summary as printed; do not count lines yourself. Other distributions:
`dnf check-update -q`, `checkupdates`.
The shell cannot refresh the package lists: say how old they are.
ok: no security updates pending.
warn: security updates pending.
crit: package lists older than 30 days.""",
    ),
    _topic(
        "updates:reboot",
        "Updates: reboot",
        "updates",
        """\
Whether the server needs a reboot after updates.
Run this ONE command:
`echo '== reboot required =='; cat /run/reboot-required.pkgs 2>/dev/null; ls -l --time-style=+%F /run/reboot-required 2>/dev/null || echo 'no'; echo '== uptime =='; uptime -p`
The date in the `ls` line is when the reboot became required.
ok: no reboot required.
warn: a reboot required.
crit: a reboot required for more than 14 days.
Name the packages that asked for the reboot and copy the uptime.""",
    ),
)

SUMMARY_PROMPT = """\
Write the overall picture of the server from the results of the checks below. They ran one
after another just now, in this order.
- First, loudly: every check that did NOT work (its run is an error, or it reported "could
  not check") is a warn finding "not checked: <check> (<reason>)", listed first, and counts
  as warn for the status. Never make it an info finding.
- status: the worst current status of the checks (crit > warn > ok).
- Keep the concrete numbers and counts of the checks (sizes, percentages, how many updates),
  copied as they are. Compare with the previous summary: what is new, what got better.
- Before you repeat a crit finding, re-run the one command that proves it (with `bash`) and
  quote its line as evidence. Otherwise do not redo the checks; at most 4 commands in all.
- Only facts from the check results or your commands: no words they do not contain (no
  "unchanged" unless you compared with the previous summary, no thresholds they do not state).
  Call a thing by the word its check used: failed units are "failed".
- summary: one headline sentence of facts from the checks, then at most 4 short lines on what
  needs attention.
- topics: one entry per check, the fine ones too: its slug, its status copied, and one clause
  with its key number (e.g. "38 updates pending, 3 security", "no errors; kernel audit
  messages rate-limited 238 times").
- findings: only what needs the owner's attention, merged across checks, most severe first,
  each with the evidence line copied from the check result or your command."""

DAILY = ("docker:updates", "updates:packages", "updates:reboot")

SUMMARIES: tuple[JobDef, ...] = (
    JobDef(
        slug="summary:health",
        name="Health",
        kind="summary",
        model="main",
        prompt=SUMMARY_PROMPT,
        interval_s=HOUR,
        max_steps=6,
        members=tuple(t.slug for t in TOPICS if t.slug not in DAILY),
        notify="changed",  # as send_summary's default in 0.9.1
    ),
    JobDef(
        slug="summary:daily",
        name="Daily",
        kind="summary",
        model="main",
        prompt=SUMMARY_PROMPT,
        cron="0 6 * * *",
        max_steps=6,
        members=DAILY,
        notify="changed",  # as send_summary's default in 0.9.1
    ),
)

BUILTINS: tuple[JobDef, ...] = TOPICS + SUMMARIES

BY_SLUG = {d.slug: d for d in BUILTINS}

OLD_BUILTINS = ("docker", "journal", "resources", "storage", "systemd", "updates", "summary")
"""The builtins of 0.9.1, which migration 0022 retired (unedited) or handed to the owner."""
