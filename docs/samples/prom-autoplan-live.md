# Prometheus auto-plan, live runs (2026-11-15)

Real planning against the user's Prometheus (`prometheus.tcloud.monster`, GET requests only, nothing
written to the live monitor) with the user's Ollama (`gemma4:e4b`). Three runs, each in a throw-away
database:

1. `scripts/dev/prom_autoplan_live.py`: 12 queries accepted, 94 s, 10 LLM calls, nothing rejected. Two
   flaws showed up in the result, both fixed afterwards: queries were not scoped to their job (a
   Windows-GPU query also returned the Linux GPU exporter's series, because both export the same
   metric names), and a model-guessed threshold (`warn > 0.01` on a disk latency that was 0.027)
   would have raised a false alarm on a healthy disk.
2. Same script after the fixes (job selector required and checked, thresholds the current value
   already crosses are dropped): 12 queries, 94 s, 9 calls, nothing rejected. It showed a third flaw:
   a query the model called `kind: "alert"` without any filter (`rate(...)`, always has a value) raised
   a warn flag with value 0. Fix: `kind: alert` only stands when the query filters (`>`, `==`, ...).
3. The real app (`scripts/dev/prom_auto_demo.py --replan`, the "Re-plan now" path) with all fixes: the
   result below, plus the screenshot `docs/samples/ui/settings-prom-auto.png`.

## Catalog slices (what the model was shown)

Whole catalog after filtering ≈ 13k tokens; one slice per job instead:

| job | metrics after filtering | slice |
|---|---|---|
| cadvisor | 80 | ≈ 750 tokens |
| linux-gpu | 117 | ≈ 690 tokens |
| linux-processes | 18 | ≈ 460 tokens |
| linux-server | 292 | ≈ 580 tokens |
| windows-gpu (other host 192.168.88.30) | 88 | ≈ 640 tokens |
| windows-pc (other host 192.168.88.30) | 144 | ≈ 630 tokens |

## Run 3: outcome

`ok` — 12 queries chosen from 6 jobs; model `gemma4:e4b`, 10 LLM calls, 111.6 s; rejected: 0.

Every accepted query was run against Prometheus first (it parsed, answered in time, returned data,
at most 20 series, and selects its own job); the last result is the one that run produced.

| name | job | query | limits | result when validated |
|---|---|---|---|---|
| ecc_error_rate | linux-gpu | `rate(nvidia_smi_ecc_errors_uncorrected_aggregate_sram_l2{job="linux-gpu"}[5m])` | warn > 0, crit > 0.01 | {"instance": "nvidia-gpu-exporter:9835", "job": "linux-gpu", "uuid": "20d08d3c-e9f7-7f83-9494-19c2b1e43a3b"} 0.0 (1 series) |
| encoder_average_latency | windows-gpu | `avg by (instance) (nvidia_smi_encoder_stats_average_latency{job="windows-gpu"})` | warn > 0.1 | {"instance": "192.168.88.30:9835"} 0.0 (1 series) |
| fd_saturation_ratio | linux-processes | `max by (instance) (namedprocess_namegroup_worst_fd_ratio{job="linux-processes"})` | warn > 0.8 | {"instance": "process-exporter:9256"} 0.388671875 (1 series) |
| gpu_hardware_slowdown_rate | windows-gpu | `avg by (instance) (nvidia_smi_clocks_event_reasons_hw_slowdown{job="windows-gpu"})` | warn > 0 | {"instance": "192.168.88.30:9835"} 0.0 (1 series) |
| io_stalling_rate | linux-server | `avg by (instance) (rate(node_pressure_io_stalled_seconds_total{job="linux-server"}[5m]))` | warn > 0.1, crit > 0.5 | {"instance": "node-exporter:9100"} 0.021698845614035057 (1 series) |
| io_waiting_rate | cadvisor | `max by (instance) (rate(container_pressure_io_waiting_seconds_total{job="cadvisor"}[5m]))` |  | {"instance": "cadvisor:8080"} 0.021716239021929692 (1 series) |
| network_receive_drop_rate | linux-server | `sum by (name) (rate(node_network_receive_drop_total{job="linux-server"}[5m]))` | warn > 0, crit > 10 | {} 0.0 (1 series) |
| network_receive_drops_rate | cadvisor | `max by (instance) (rate(container_network_receive_packets_dropped_total{job="cadvisor"}[5m]))` | warn > 0 | {"instance": "cadvisor:8080"} 0.0 (1 series) |
| outbound_network_errors_rate | windows-pc | `rate(windows_net_packets_outbound_errors_total{job="windows-pc"}[5m])` | warn > 0 | {"instance": "192.168.88.30:9182", "job": "windows-pc", "nic": "Intel[R] Ethernet Controller [3] I225-V"} 0.0; {"instance": "192.168.88.30:9182", "job": "windows-pc", "nic": "Intel[R] Wi-Fi 6E AX210 160MHz"} 0.0 (3 series) |
| physical_disk_queue_depth | windows-pc | `max by (instance) (windows_physical_disk_requests_queued{job="windows-pc"})` | warn > 10, crit > 50 | {"instance": "192.168.88.30:9182"} 1.0 (1 series) |
| scrape_errors_total | linux-processes | `sum(namedprocess_scrape_errors{job="linux-processes"})` | warn > 0 | {} 0.0 (1 series) |
| thermal_throttling_duration | linux-gpu | `max by (instance) (nvidia_smi_clocks_event_reasons_counters_hw_thermal_slowdown_seconds{job="linux-gpu"})` | warn > 0.1, crit > 1 | {"instance": "nvidia-gpu-exporter:9835"} 0.0 (1 series) |

## Resulting digest section (run 3, one collector pass later)

```
## Prometheus
Scrape targets: 6 of 6 up; other hosts scraped: windows-gpu (192.168.88.30), windows-pc (192.168.88.30)
Metric ecc_error_rate: 20d08d3c-e9f7-7f83-9494-19c2b1e43a3b 0 per_s — Rate of uncorrected L2 SRAM ECC errors, indicating potential memory instability or hardware…
Metric encoder_average_latency: other host 192.168.88.30 0s — Average latency of the encoder pipeline, indicating video encoding performance.
Metric fd_saturation_ratio: process-exporter 0.3887 ratio — The worst ratio between open file descriptors and maximum allowed file descriptors, indicating…
Metric gpu_hardware_slowdown_rate: other host 192.168.88.30 0s — Average rate of hardware slowdown events (throttling), indicating performance constraints.
Metric io_stalling_rate: node-exporter 0.0217 per_s — Average rate of IO stalling time, indicating storage bottlenecks.
Metric io_waiting_rate: cadvisor 0.02172 per_s — Rate of time tasks are waiting due to I/O pressure, indicating potential disk bottlenecks or slow…
Metric network_receive_drop_rate: 0 per_s — Rate of network receive drops (packet loss) per interface.
Metric network_receive_drops_rate: cadvisor 0 per_s — Rate of packets dropped while receiving network traffic, indicating potential network congestion or…
Metric outbound_network_errors_rate: other host 192.168.88.30 Intel[R] Ethernet Controller [3] I225-V 0 per_s, other host 192.168.88.30 Intel[R] Wi-Fi 6E AX210 160MHz 0 per_s, other host 192.168.88.30 Xbox Wireless Adapter for Windows 0 per_s — Rate of outbound network packet errors (errors/s).
Metric physical_disk_queue_depth: other host 192.168.88.30 1 count — Maximum number of requests waiting in the physical disk queue (saturation indicator).
Metric scrape_errors_total: 0 count — Total count of general scrape errors encountered by the process exporter, indicating exporter…
Metric thermal_throttling_duration: nvidia-gpu-exporter 0s — Maximum duration of hardware thermal slowdown detected, indicating potential overheating or…
```

Flags raised by the Prometheus rule: none (all targets up, nothing crossed a limit).
Results of the Windows PC are labelled "other host 192.168.88.30".

## What the model saw and answered (run 2, first two calls)

### call 1 (9.9s)

<details><summary>last message sent</summary>

```
<catalog>
JOB cadvisor: 1 target(s) [cadvisor:8080 up], 80 metrics
families: container_memory_* 20, container_fs_* 17, container_network_* 8, container_pressure_* 6, container_cpu_* 5, container_spec_* 5, machine_cpu_* 5, machine_nvm_* 2, container_blkio_* 1, container_creation_* 1
metrics [g=gauge c=counter, xN = series count]:
container_fs_inodes_free [g x123 aggregate!] Number of available Inodes
container_fs_inodes_total [g x123 aggregate!] Number of Inodes
container_health_state [g x150 aggregate!] The result of the container's health check
container_memory_failcnt [c x150 aggregate!] Number of memory usage hits limits
container_network_receive_errors_total [c x129 aggregate!] Cumulative count of errors encountered while receiving
container_network_receive_packets_dropped_total [c x129 aggregate!] Cumulative count of packets dropped while receiving
container_network_transmit_errors_total [c x129 aggregate!] Cumulative count of errors encountered while transmitting
container_network_transmit_packets_dropped_total [c x129 aggregate!] Cumulative count of packets dropped while transmitting
container_oom_events_total [c x150 aggregate!] Count of out of memory events observed for the container
container_pressure_cpu_stalled_seconds_total [c x150 aggregate!] Total time duration no tasks in the container could make pro
container_pressure_cpu_waiting_seconds_total [c x150 aggregate!] Total time duration tasks in the container have waited due t
container_pressure_io_stalled_seconds_total [c x150 aggregate!] Total time duration no tasks in the container could make pro
container_pressure_io_waiting_seconds_total [c x150 aggregate!] Total time duration tasks in the container have waited due t
container_pressure_memory_stalled_seconds_total [c x150 aggregate!] Total time duration no tasks in the container could make pro
container_pressure_memory_waiting_seconds_total [c x150 aggregate!] Total time duration tasks in the container have waited due t
container_scrape_error [g] 1 if there was an error while getting container metrics, 0 o
machine_cpu_sockets [g] Number of CPU sockets.
machine_nvm_avg_power_budget_watts [g] NVM power budget.
machine_scrape_error [g] 1 if there was an error while getting machine metrics, 0 oth
container_memory_failures_total [c x600 aggregate!] Cumulative count of memory allocation failures.
container_tasks_state [g x750 aggregate!] Number of tasks in given state
container_cpu_load_average_10s [g x150 aggregate!] Value of container cpu load average over the last 10 seconds
container_cpu_load_d_average_10s [g x150 aggregate!] Value of container cpu load.d average over the last 10 secon
container_cpu_system_seconds_total [c x150 aggregate!] Cumulative system cpu time consumed in seconds.
container_cpu_usage_seconds_total [c x148 aggregate!] Cumulative cpu time consumed in seconds.
container_cpu_user_seconds_total [c x150 aggregate!] Cumulative user cpu time consumed in seconds.
container_creation_time_seconds [g x150 aggregate!] Container cr
</catalog>

Choose the queries now.
```
</details>

Reply:

```
```json
[
  {
    "name": "network_receive_drops_rate",
    "query": "rate(container_network_receive_packets_dropped_total{job=\"cadvisor\"}[5m])",
    "description": "Rate of packets dropped while receiving network traffic, indicating potential network saturation or misconfiguration.",
    "unit": "per_s",
    "kind": "alert",
    "warn_if": "> 0",
    "crit_if": "> 10"
  },
  {
    "name": "io_waiting_rate",
    "query": "rate(container_pressure_io_waiting_seconds_total{job=\"cadvisor\"}[5m])",
    "description": "Rate of time tasks spent waiting for I/O, indicating potential disk or storage saturation/bottleneck.",
    "unit": "per_s",
    "kind": "value",
    "warn_if": "> 0.1",
    "crit_if": "> 0.5"
  },
  {
    "name": "inodes_free_count",
    "query": "avg by (name) (container_fs_inodes_free{job=\"cadvisor\"})",
    "description": "Average number of free Inodes remaining on the filesystem. Low values indicate potential inode exhaustion, even if disk space remains.",
    "unit": "count",
    "kind": "value",
    "warn_if": "< 1000",
    "crit_if": "< 100"
  }
]
```
```

### call 2 (8.6s)

<details><summary>last message sent</summary>

```
These queries were checked against the real Prometheus and failed:
- network_receive_drops_rate: returns 129 series; aggregate with sum, max or topk(3, ...)
- io_waiting_rate: returns 150 series; aggregate with sum, max or topk(3, ...)
Reply with ONLY a JSON array of corrected versions of these queries (same format). Leave out any you cannot fix.
```
</details>

Reply:

```
```json
[
  {
    "name": "network_receive_drops_rate",
    "query": "sum(rate(container_network_receive_packets_dropped_total{job=\"cadvisor\"}[5m]))",
    "description": "Total rate of packets dropped while receiving network traffic, indicating potential network saturation or misconfiguration.",
    "unit": "per_s",
    "kind": "alert",
    "warn_if": "> 0",
    "crit_if": "> 10"
  },
  {
    "name": "io_waiting_rate",
    "query": "sum(rate(container_pressure_io_waiting_seconds_total{job=\"cadvisor\"}[5m]))",
    "description": "Total rate of time tasks spent waiting for I/O, indicating potential disk or storage saturation/bottleneck.",
    "unit": "per_s",
    "kind": "value",
    "warn_if": "> 0.1",
    "crit_if": "> 0.5"
  },
  {
    "name": "inodes_free_count",
    "query": "sum(container_fs_inodes_free{job=\"cadvisor\"})",
    "description": "Total number of free Inodes remaining across all monitored filesystems. Low values indicate potential inode exhaustion.",
    "unit": "count",
    "kind": "value",
    "warn_if": "< 1000",
    "crit_if": "< 100"
  }
]
```
```

