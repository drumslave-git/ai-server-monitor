<!-- Real report from the live instance's LLM, input = docs/samples/digest-live.txt
model: gemma4:e4b @ https://ollama.tcloud.monster/v1; 12s; tokens in/out: 1878/1569
parsed header: health=warn score=75 parsed_ok=True
summary: The server is operational but requires attention regarding disk space, failed services, and container errors.
findings: [{"severity": "warn", "title": "Root disk space filling up", "detail": "The root filesystem (/) is 88% used, with only 28.4GB free."}, {"severity": "warn", "title": "Secondary disk filling up", "detail": "The /mnt/disks/disk14TB partition is 87% used, with 1.8TB free."}, {"severity": "warn", "title": "Homeassistant logging errors", "detail": "The container homeassistant is logging many errors, specifically 60 error lines in the last check."}, {"severity": "warn", "title": "logrotate service failed", "detail": "The logrotate.service unit failed, resulting in an exit-code."}, {"severity": "warn", "title": "remmina ssh-agent service failed", "detail": "The snap.remmina.ssh-agent.service unit failed, resulting in an exit-code."}] -->

## Overview
The server is currently running but exhibits several warning signs that require immediate attention. The most critical issues involve filling disk partitions and multiple failed system services.
## Findings
*   **Disk Space:** Both the root filesystem (/) and the /mnt/disks/disk14TB partition are approaching capacity (88% and 87% used, respectively).
*   **Application Errors:** The `homeassistant` container is logging many errors, with 60 error lines noted in the last check.
*   **System Services:** Two services are failing: `logrotate.service` and `snap.remmina.ssh-agent.service`.
*   **Zombie Processes:** There are 130 zombie processes detected, indicating that parent processes are not reaping their children.
*   **Exited Containers:** Two containers, `docker-model-runner-dashboard-dashboard-1` and `llm-tg-bot-nextjs-app`, have exited with an error (exit code 143) in the past.
## Resources
**CPU:** The CPU is 8.9% busy on 12 threads, with a load average of 0.27/0.49/0.50 over the last 1/5/15 minutes.
**Memory:** Memory usage is low at 8.8% (11.6GB used of 133GB).
**Disks:** The root partition (/) is 88% full. The /mnt/disks/disk14TB is 87% full.
**Top Consumers:** The `ollama` container is the top RAM consumer at 3.9GB, while `containerd` (31%) and `dockerd` (29%) are the top CPU consumers.
## Changes
No previous report.
## Recommendations
1.  **Disk Cleanup:** Immediately investigate and clear space on the root filesystem (/) and the /mnt/disks/disk14TB partition.
2.  **Service Check:** Investigate why `logrotate.service` and `snap.remmina.ssh-agent.service` are failing and attempt to restart or fix them.
3.  **Container Review:** Review the `homeassistant` container logs to address the 60 reported errors.
4.  **Process Management:** Investigate the source of the 130 zombie processes to ensure proper resource cleanup.
