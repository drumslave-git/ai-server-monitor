# Jobs lane: hand-over (2026-11-18)

Scheduled LLM jobs: topic checks on the worker model, a summary on the main model, stored
runs, APScheduler, chat tools, `prometheus_client` metrics. Code: `backend/ai_server_monitor/
jobs/` (store, builtins, prompts, runner, schemas, api, tools), `schedule.py`, `outputs/metrics.py`.

## API for the frontend (types: `frontend/src/api-types.ts`, schemas `Job`, `JobRun`)

    Job     = {slug, name, kind: "topic"|"summary", prompt, interval_s: int|null, cron: str|null,
               model: "worker"|"main", enabled, builtin, edited, timeout_s: int|null,
               max_steps: int|null, created_by: "builtin"|"user"|"llm", created, updated,
               next_run: float|null, last_run: JobRun|null}
    JobRun  = {id, job_slug, trigger: "schedule"|"manual"|"llm", started, finished|null,
               duration_s|null, status: "running"|"ok"|"warn"|"crit"|"error",
               summary (markdown)|null, findings: [{severity: "info"|"warn"|"crit", title, detail}],
               error|null, model, prompt_tokens, completion_tokens, steps, trace_turn}
    (times are Unix seconds; exactly one of interval_s / cron is set)

    GET    /api/jobs                    -> {items: [Job]}   builtins first; last_run = newest finished
    POST   /api/jobs                    body {slug, name, prompt, interval_s | cron, model?, enabled?,
                                        timeout_s?, max_steps?} -> 201 Job (kind topic, created_by user)
                                        409 slug taken; 422 bad slug / cron / both or no schedule
    GET    /api/jobs/{slug}             -> Job | 404
    PUT    /api/jobs/{slug}             body: any of the fields above except slug -> Job
                                        (interval_s clears cron and back; editing a builtin's prompt,
                                        schedule, model or limits sets edited=true; enabled does not)
    DELETE /api/jobs/{slug}             -> 204 (and its runs) | 409 builtin | 404
    POST   /api/jobs/{slug}/reset       -> Job (builtin back to the release version, edited=false) | 409
    POST   /api/jobs/{slug}/run         -> 202 {run_id}  (also when disabled or paused)
    GET    /api/jobs/{slug}/runs?limit= -> {items: [JobRun]} newest first, a running one included
    GET    /api/jobs/runs/{run_id}      -> JobRun | 404
    GET    /api/status                  -> … + overall: {status, headline, finished, run_id} | null
                                           + jobs: [{slug, name, kind, enabled, status|null,
                                                     finished|null, run_id|null}]
    GET    /api/info                    -> … + jobs: [{name ("job:<slug>", "prune", …), kind,
                                           paused, next_run, runs, running, last_status, last_error}]
    SSE /api/stream?types=job           job.started {run_id, slug, trigger}
                                        job.finished {run_id, slug, status, error}
    Logs page: a run's LLM/tool calls have purpose "job", link_kind "job", link_id slug,
    turn = trace_turn. Settings section "jobs" (title "Jobs"): max_concurrent_runs, timeout,
    max_steps.

## Builtins (seeded from `jobs/builtins.py`; unedited ones follow later releases)

    storage   worker  every 4 h     disk space and inodes, biggest directories
    journal   worker  every 1 h     journal errors of the last hour, kernel warnings
    resources worker  every 30 min  CPU, memory, load, GPU, temperatures
    docker    worker  every 30 min  container state, restarts, health
    systemd   worker  every 1 h     failed units, system state
    updates   worker  cron 0 6 * * * pending updates, reboot required
    summary   main    every 1 h     all latest topic results + previous summary; bash + delegate

## Notes

- Cron runs in the container's time zone (UTC unless TZ is set).
- Pause: the whole schedule (jobs + housekeeping) stops; scheduled runs in flight are cancelled
  and stored as error. "Run now" and chat-started runs still run.
- Not done: `docs/grafana-dashboard.json` still shows only the LLM/approval panels.
