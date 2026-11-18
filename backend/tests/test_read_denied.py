"""read_denied: only a failed read is worth a hint about the read-permission capability."""

from __future__ import annotations

import pytest

from ai_server_monitor.agent.shell import read_denied


@pytest.mark.parametrize(
    "output",
    [
        "cat: /var/log/auth.log: Permission denied",
        "du: cannot read directory '/root': Permission denied",
        "find: '/etc/ssl/private': Permission denied",
        "ok line\ncat: /etc/shadow: Permission denied\nanother",
    ],
)
def test_failed_reads_are_detected(output: str) -> None:
    assert read_denied(output)


@pytest.mark.parametrize(
    "output",
    [
        "",
        "all fine",
        "touch: cannot touch '/etc/x': Permission denied",
        "mkdir: cannot create directory '/opt/x': Permission denied",
        "rm: cannot remove '/etc/hosts': Permission denied",
        "permission denied while trying to connect to the Docker daemon socket at "
        "unix:///var/run/docker.sock",
        "kill: (123) - Operation not permitted",
        "-bash: kill: (123) - Permission denied",
    ],
)
def test_writes_sockets_and_signals_are_not(output: str) -> None:
    assert not read_denied(output)
