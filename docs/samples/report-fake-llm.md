<!-- Report from the fake streaming LLM used in tests (canned answer, live digest as input)
model: fake
parsed header: health=warn score=74 parsed_ok=True
summary: Two disks are over 85% full and two systemd units have failed; everything else looks healthy.
findings: [{"severity": "warn", "title": "Disk / filling up", "detail": "88% used, 28.4GB free."}, {"severity": "warn", "title": "Failed units", "detail": "logrotate.service and snap.remmina.ssh-agent.service failed with exit-code."}, {"severity": "info", "title": "130 zombie processes", "detail": "Parents are not reaping them."}] -->

## Overview
The server is up and mostly healthy, but needs attention soon: the root disk is 88% full
and two services have failed.

## Findings
- **Disk / filling up** — 88% used, 28.4GB free.
- **Failed units** — `logrotate.service` and `snap.remmina.ssh-agent.service` (exit-code).
- 130 zombie processes (info).

## Resources
CPU 8.9% busy, load 0.49 on 12 threads. Memory 8.8% used. GPU idle (RTX 3090, 25°C).

## Changes
No previous report.

## Recommendations
Free space on `/`, check `journalctl -u logrotate.service`.
