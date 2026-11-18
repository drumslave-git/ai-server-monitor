# ruff: noqa: E501  (prompt text: the commands are quoted whole)
"""The predefined jobs, seeded into the `jobs` table. A builtin the user never edited follows
changes made here in later releases (prompt, schedule, name); an edited one keeps the user's
version until "reset to default".

Prompts are written for a small local model: one topic, the usual commands, and what ok /
warn / crit mean for it."""

from __future__ import annotations

from dataclasses import dataclass


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


HOUR = 3600

BUILTINS: tuple[JobDef, ...] = (
    JobDef(
        slug="storage",
        name="Storage",
        kind="topic",
        model="worker",
        interval_s=4 * HOUR,
        prompt="""\
Disk space and inodes of the host's real filesystems.
Commands:
- `df -hT -x tmpfs -x devtmpfs -x overlay -x squashfs -x efivarfs`
- `df -i -x tmpfs -x devtmpfs -x overlay -x squashfs -x efivarfs`
- only for a filesystem at 85 % or more, its biggest directories (short output):
  `du -xh --max-depth=1 MOUNT 2>/dev/null | sort -h | tail -8` (pass timeout_s=600)
ok: every filesystem below 85 % of space and of inodes.
warn: a filesystem at 85-95 %.
crit: a filesystem at 95 % or more, or less than 5 GB free on `/`.
For each filesystem at 85 % or more: the mount point, the percentage and the free space from
its df line, and its two biggest directories: `sort -h` puts the biggest last, and the very
last du line is the whole mount (its total, not a directory), so copy the TWO LINES JUST ABOVE
the last line as they are (e.g. "650G .../telegram-downloader", "11T .../media"). The disk's
size and use are only in df: never mix them.""",
    ),
    JobDef(
        slug="journal",
        name="Journal",
        kind="topic",
        model="worker",
        interval_s=HOUR,
        prompt="""\
Errors in the system journal of the last hour.
Commands:
- `journalctl -p err -S -1h --no-pager -o cat | sort | uniq -c | sort -rn | head -20`
- `journalctl -p err -S -1h --no-pager -o short-iso | tail -40` (which units, when)
- `journalctl -k -p warning -S -1h --no-pager -o cat | sort | uniq -c | sort -rn | head -20` (kernel)
ok: no errors, or only a few one-off messages.
warn: one service logging the same error again and again, or errors that point to a failing
component or a broken configuration.
crit: out-of-memory kills, disk or filesystem I/O errors, hardware errors (MCE, EDAC, ata
errors), segfaults of system services.
Repeated or rate-limited messages ("callbacks suppressed", "rate limit", the same line many
times) are never just normal: report them as an info finding with the count from uniq -c.
Quote the important lines short, with the unit name and how often they occurred.""",
    ),
    JobDef(
        slug="resources",
        name="Resources",
        kind="topic",
        model="worker",
        interval_s=30 * 60,
        prompt="""\
CPU, memory, load, GPU and temperatures right now.
Commands:
- `nproc; cat /proc/loadavg; free -m` — /proc/loadavg is: 1-minute load, 5-minute load,
  15-minute load, running/total tasks, last PID. Name each load by its own position.
- `ps -eo pid,user,comm,%cpu,%mem --sort=-%cpu | head -6; ps -eo pid,user,comm,%cpu,%mem --sort=-%mem | head -6`
- GPU (skip when missing): `nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu --format=csv`
- temperatures: `sensors` when it exists; otherwise
  `for z in /sys/class/thermal/thermal_zone*; do echo "$(cat $z/type) $(cat $z/temp)"; done`
  (the number is thousandths of °C: 54000 means 54 °C; name the zone type)
ok: 15-minute load below the number of cores, more than 10 % memory available, swap mostly
unused, temperatures below 85 °C.
warn: 15-minute load 1-2 times the cores, less than 10 % memory available, swap over 50 %
used, CPU or GPU at 85-95 °C.
crit: less than 3 % memory available, load above 2 times the cores, any temperature at 95 °C
or more.
Name the processes that use the most CPU and memory.""",
    ),
    JobDef(
        slug="docker",
        name="Docker",
        kind="topic",
        model="worker",
        interval_s=30 * 60,
        prompt="""\
Health of the Docker containers.
Commands (one call each; do not inspect containers one by one):
- `docker ps -a --format '{{.Names}}\\t{{.Status}}\\t{{.Image}}'` (Status shows "Restarting",
  "(unhealthy)" and "Exited (code)")
- `docker stats --no-stream --format '{{.Name}}\\t{{.CPUPerc}}\\t{{.MemUsage}}\\t{{.MemPerc}}'`
- only for a container with a problem, at most two: `docker logs --tail 30 NAME 2>&1`
ok: the running containers are up, and healthy where they have a healthcheck.
warn: a container that restarts again and again, is unhealthy, or exited with a non-zero
code recently; a container above 90 % of its memory limit.
crit: an important container (database, reverse proxy, the main services) down, in a
restart loop or unhealthy.
Containers that exited with code 0 are one-off tasks: ignore them.""",
    ),
    JobDef(
        slug="systemd",
        name="Systemd services",
        kind="topic",
        model="worker",
        interval_s=HOUR,
        prompt="""\
State of the systemd services. `systemctl` does not work in this shell: do not try it.
Commands (systemd's Varlink socket answers read-only):
- system state and number of failed units:
  `varlinkctl call /run/systemd/io.systemd.Manager io.systemd.Manager.Describe '{}' | grep -oE '"(SystemState|NFailedUnits)":[^,}]*'`
- the failed units:
  `varlinkctl call --more /run/systemd/io.systemd.Manager io.systemd.Unit.List '{}' | grep '"ActiveState":"failed"' | grep -oE '"ID":"[^"]*"'`
- why one failed (at most three units): `journalctl -u UNIT -n 15 --no-pager -o short-iso`
If varlinkctl is missing or fails (systemd older than 257), use instead:
  `journalctl _PID=1 -p warning -S -24h --no-pager -o cat | grep -i 'failed' | sort | uniq -c | sort -rn | head -15`
ok: no failed units, the system is "running".
warn: failed units that are not essential (one-shot jobs, a timer's service), the system is
"degraded".
crit: a failed essential service: ssh, networking, docker or containerd, cron, a mount of a
data disk.
One finding per failed unit: the title names the unit, the detail gives the reason in a few
words copied from its journal error line (for example the "error:" text), not just the name.""",
    ),
    JobDef(
        slug="updates",
        name="Updates",
        kind="topic",
        model="worker",
        cron="0 6 * * *",
        prompt="""\
Pending package updates and whether a reboot is needed.
Commands (Debian/Ubuntu):
- `apt list --upgradable 2>/dev/null | tail -n +2 | wc -l` (put this count in the summary)
- `apt list --upgradable 2>/dev/null | grep -i security | head -30`
- `cat /run/reboot-required.pkgs 2>/dev/null; ls -l /run/reboot-required 2>/dev/null`
- `uptime -p`
- how old the package lists are: `stat -c %y /var/cache/apt/pkgcache.bin`
Other distributions: `dnf check-update -q`, `checkupdates`.
The shell cannot refresh the package lists: say how old they are.
ok: no security updates pending and no reboot required.
warn: security updates pending, or a reboot required.
crit: a reboot required for more than 14 days, or package lists older than 30 days.""",
    ),
    JobDef(
        slug="summary",
        name="Summary",
        kind="summary",
        model="main",
        interval_s=HOUR,
        max_steps=6,
        prompt="""\
Write the overall picture of the server from the latest results of the topic checks below.
- First, loudly: every topic that was NOT checked (its last run is an error, or it reported
  "could not check") is a warn finding "not checked: <topic> (<reason>)", listed first, and
  counts as warn for the status. Never make it an info finding. A result older than three
  times its schedule is stale: say so the same way.
- status: the worst current status of the topics (crit > warn > ok).
- Keep the concrete numbers and counts of the topics (sizes, percentages, how many updates),
  copied as they are. Compare with the previous summary: what is new, what got better.
- Before you repeat a crit finding, re-run the one command that proves it (with `bash`) and
  quote its line as evidence. Otherwise do not redo the checks; at most 4 commands in all.
- summary: one headline sentence, then at most 5 short lines.
- findings: only what needs the owner's attention, merged across topics, most severe first,
  each with the evidence line copied from the topic result or your command.""",
    ),
)

BY_SLUG = {d.slug: d for d in BUILTINS}
