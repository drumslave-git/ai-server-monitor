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
Typical commands:
- `df -hT -x tmpfs -x devtmpfs -x overlay -x squashfs -x efivarfs`
- `df -i -x tmpfs -x devtmpfs -x overlay -x squashfs -x efivarfs`
- for a filesystem at 85 % or more, its biggest directories:
  `du -xh --max-depth=2 MOUNT 2>/dev/null | sort -h | tail -15`
- Docker's share: `docker system df`
ok: every filesystem below 85 % of space and of inodes.
warn: a filesystem at 85-95 %.
crit: a filesystem at 95 % or more, or less than 5 GB free on `/`.
Name the mount point, the percentage, the free space and the biggest directories.""",
    ),
    JobDef(
        slug="journal",
        name="Journal",
        kind="topic",
        model="worker",
        interval_s=HOUR,
        prompt="""\
Errors in the system journal of the last hour.
Typical commands:
- `journalctl -p err -S -1h --no-pager -o cat | sort | uniq -c | sort -rn | head -20`
- `journalctl -p err -S -1h --no-pager -o short-iso | tail -40` (which units, when)
- `journalctl -k -p warning -S -1h --no-pager | tail -30` (kernel)
ok: no errors, or only a few one-off messages.
warn: one service logging the same error again and again, or errors that point to a failing
component or a broken configuration.
crit: out-of-memory kills, disk or filesystem I/O errors, hardware errors (MCE, EDAC, ata
errors), segfaults of system services.
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
Typical commands:
- `uptime`, `nproc`, `free -m`
- `ps -eo pid,user,comm,%cpu,%mem --sort=-%cpu | head -8` and `--sort=-%mem`
- `cat /proc/pressure/memory /proc/pressure/cpu 2>/dev/null`
- GPU (skip when missing): `nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu --format=csv`
- temperatures: `sensors 2>/dev/null` or `cat /sys/class/thermal/thermal_zone*/temp`
  (thousandths of °C)
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
Typical commands:
- `docker ps -a --format '{{.Names}}\\t{{.Status}}\\t{{.Image}}'`
- `docker inspect -f '{{.Name}} restarts={{.RestartCount}} health={{if .State.Health}}{{.State.Health.Status}}{{end}}' $(docker ps -aq)`
- for a container with a problem: `docker logs --tail 30 NAME 2>&1`
- `docker stats --no-stream --format '{{.Name}} {{.CPUPerc}} {{.MemPerc}}'`
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
State of the systemd services.
Typical commands:
- `systemctl is-system-running`
- `systemctl --failed --no-legend --plain`
- for each failed unit: `systemctl status UNIT --no-pager -n 15`
ok: no failed units, the system is "running".
warn: failed units that are not essential (one-shot jobs, a timer's service), the system is
"degraded".
crit: a failed essential service: ssh, networking, docker or containerd, cron, a mount of a
data disk.
Name each failed unit with the reason from its status.""",
    ),
    JobDef(
        slug="updates",
        name="Updates",
        kind="topic",
        model="worker",
        cron="0 6 * * *",
        prompt="""\
Pending package updates and whether a reboot is needed.
Typical commands (Debian/Ubuntu):
- `apt list --upgradable 2>/dev/null | tail -n +2 | wc -l`
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
- status: the worst current status of the topics (crit > warn > ok). A topic whose last run
  failed (error) counts as warn. A result older than three times its schedule is stale: say so.
- Compare with the previous summary: say what is new and what got better.
- Use `bash` (or `delegate`) only to double-check a crit finding or a contradiction; do not
  redo the checks.
- summary: one headline sentence, then at most 5 short lines.
- findings: only what needs the owner's attention, merged across topics, most severe first.""",
    ),
)

BY_SLUG = {d.slug: d for d in BUILTINS}
