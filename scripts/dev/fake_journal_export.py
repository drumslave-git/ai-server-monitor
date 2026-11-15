"""Print a journal in `systemd-journal-remote` export format (a fixture for chat_tools_live.sh).

    python fake_journal_export.py | systemd-journal-remote -o system.journal -
"""

from __future__ import annotations

import time

MACHINE = "0123456789abcdef0123456789abcdef"
BOOT = "fedcba9876543210fedcba9876543210"


def entry(ts_us: int, unit: str, ident: str, prio: int, message: str) -> str:
    fields = [
        ("__REALTIME_TIMESTAMP", ts_us),
        ("__MONOTONIC_TIMESTAMP", ts_us % 10**12),
        ("_BOOT_ID", BOOT),
        ("_MACHINE_ID", MACHINE),
        ("_HOSTNAME", "box"),
        ("_SYSTEMD_UNIT", unit),
        ("SYSLOG_IDENTIFIER", ident),
        ("PRIORITY", prio),
        ("MESSAGE", message),
    ]
    return "".join(f"{k}={v}\n" for k, v in fields) + "\n"


def main() -> None:
    now = int(time.time())
    out = []
    for day in (3, 2, 1):
        ts = (now - day * 86400) * 1_000_000
        out.append(
            entry(
                ts,
                "logrotate.service",
                "logrotate",
                3,
                "error: /etc/logrotate.d/nginx:7 unexpected end of file, expected endscript",
            )
        )
        out.append(
            entry(
                ts + 1000,
                "logrotate.service",
                "systemd",
                3,
                "logrotate.service: Main process exited, code=exited, status=1/FAILURE",
            )
        )
        out.append(
            entry(
                ts + 2000,
                "logrotate.service",
                "systemd",
                3,
                "logrotate.service: Failed with result 'exit-code'.",
            )
        )
    out.append(
        entry(
            now * 1_000_000,
            "cron.service",
            "CRON",
            6,
            "pam_unix(cron:session): session opened for user root",
        )
    )
    print("".join(out), end="")


if __name__ == "__main__":
    main()
