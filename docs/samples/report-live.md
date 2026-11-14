<!-- Real report from the live instance's LLM, input = docs/samples/digest-live.txt
model: gemma4:e4b @ https://ollama.tcloud.monster/v1; 13s; tokens in/out: 1813/1854
parsed header: health=warn score=82 parsed_ok=True
summary: The server requires attention due to multiple warnings regarding disk space, failed services, and application errors.
findings: [{"severity": "warn", "title": "Root disk space is high", "detail": "The root partition (/) is 88% used, with 28.4GB free of 244GB."}, {"severity": "warn", "title": "Secondary disk space is high", "detail": "The /mnt/disks/disk14TB partition is 87% full, leaving 1.8TB free of 14.0TB."}, {"severity": "warn", "title": "logrotate service failed", "detail": "The logrotate.service failed with an exit-code."}, {"severity": "warn", "title": "remmina ssh-agent service failed", "detail": "The snap.remmina.ssh-agent.service failed with an exit-code."}, {"severity": "warn", "title": "Home Assistant logging errors", "detail": "The container homeassistant is logging many errors, specifically 60 error lines in the last check."}, {"severity": "info", "title": "Zombie processes detected", "detail": "There are 130 zombie processes, indicating parents are not reaping their children."}, {"severity": "info", "title": "Unused Docker images available", "detail": "There are 142GB of unused Docker images, which could be cleared using 'docker image prune -a'."}] -->

## Overview
The server is currently in a warning state and requires attention. The primary concerns are high disk utilization on multiple partitions, several failed system services, and persistent logging errors within the Home Assistant container.

## Findings
*   **Disk Space Critical:** Both the root partition (/) and the `/mnt/disks/disk14TB` partition are running at high utilization (88% and 87%, respectively).
*   **Service Failures:** The `logrotate.service` and `snap.remmina.ssh-agent.service` are both reporting failures with an exit-code.
*   **Application Errors:** The Home Assistant container is logging many errors, with 60 error lines noted in the last check.
*   **Process Health:** There are 130 zombie processes, which suggests that parent processes are not properly reaping their children.
*   **Cleanup Opportunity:** There are 142GB of unused Docker images that can be cleared using `docker image prune -a`.

## Resources
**CPU:** 8.9% busy on 12 threads. Load averages are 0.27/0.49/0.50 for the last 1/5/15 minutes.
**Memory:** 8.8% used (11.6GB of 133GB).
**Disks:**
*   `/`: 88% used (28.4GB free of 244GB).
*   `/mnt/disks/disk14TB`: 87% used (1.8TB free of 14.0TB).
*   Top Consumers: The `ollama` container is the top RAM consumer at 3.9GB, while `grafana-stack-cadvisor-1` is the top CPU consumer at 30%.

## Changes
No previous report.

## Recommendations
1.  **Address Disk Space:** Immediately run `docker image prune -a` to clear the 142GB of unused Docker images, which will help alleviate the high usage on the root partition.
2.  **Investigate Services:** Check the logs for `logrotate.service` and `snap.remmina.ssh-agent.service` to determine the root cause of their failures and attempt to restart them.
3.  **Review Home Assistant:** Investigate the 60 error lines in the Home Assistant logs to resolve the underlying API or configuration issues.
4.  **Monitor Processes:** Monitor the zombie processes and identify the parent processes that need attention.
