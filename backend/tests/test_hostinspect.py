"""Host inspection: command validation, path guard, output shaping, disk attribution,
container summaries and metric history. Pure logic: runs anywhere. The sandbox itself is
exercised in test_hostinspect_sandbox.py."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from ai_server_monitor.db import Database
from ai_server_monitor.hostinspect import attribution, disk, history
from ai_server_monitor.hostinspect.commands import (
    PathGuard,
    Refused,
    denied_reason,
    monitor_data_dirs,
    parse_pipeline,
    split_stages,
)
from ai_server_monitor.hostinspect.containers import summarize_inspect, summarize_list
from ai_server_monitor.hostinspect.output import HeadTail, human, scrub_output
from ai_server_monitor.hostinspect.sandbox import RunResult

# --------------------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    "line",
    [
        "ls; rm -rf /",
        "ls && ls",
        "ls || ls",
        "ls &",
        "ls > /tmp/x",
        "ls >> /tmp/x",
        "cat < /etc/hostname",
        "ls $(id)",
        "ls `id`",
        "echo $HOME",
        'ls "$(id)"',
        "ls\nid",
        "ls | | wc",
        "| ls",
        "ls |",
        "FOO=bar ls",
        "/bin/ls",
        "curl http://example.com",
        "bash -c id",
        "python3 -c 1",
        "sh -c id",
        "sudo ls",
        "docker ps",
        "rm -rf /tmp/x",
        "find / -delete",
        "find / -name x -exec cat {} +",
        "find / -execdir id ;",
        "find / -fprint /tmp/x",
        "journalctl --vacuum-size=1M",
        "journalctl --vacuum-time=1d",
        "journalctl --rotate",
        "journalctl -f",
        "journalctl --follow",
        "journalctl -fu ssh",
        "systemctl restart ssh",
        "systemctl stop docker",
        "systemctl daemon-reload",
        "sort -o /tmp/x /etc/hosts",
        "tail -f /var/log/syslog",
        "tail -F /var/log/syslog",
        "dmesg -c",
        "dmesg --clear",
        "top -i",
        "ip addr add 10.0.0.1/24 dev eth0",
        "ip link set eth0 down",
        "mount /dev/sda1 /mnt",
        "hostname evil",
        "date -s 2000-01-01",
        "getent shadow",
        "nvidia-smi -pm 1",
        "file -C",
        "cat /etc/shadow",
        "cat /etc/gshadow",
        "cat /home/u/.ssh/id_rsa",
        "head -n 5 /root/.ssh/authorized_keys",
        "cat /srv/app/.env",
        "cat /etc/ssl/private/server.key",
        "cat /etc/secrets.txt",
        "cat /srv/my-password-list",
        "cat /proc/1/environ",
        "cat /etc/../etc/shadow",
        "grep root /etc/shadow",
        "grep -f /etc/shadow /etc/passwd",
        "grep --file=/etc/shadow /etc/passwd",
        "wc -l /home/u/.mozilla/firefox/x/cookies.sqlite",
        "ls 'unterminated",
        "ls " + "a" * 1100,
        "ls | ls | ls | ls | ls | ls",
    ],
)
def test_forbidden_commands_are_refused(line: str) -> None:
    with pytest.raises(Refused):
        parse_pipeline(line)


@pytest.mark.parametrize(
    ("line", "stages"),
    [
        ("ls -la /etc", [["ls", "-la", "/etc"]]),
        (
            "du -xh --max-depth=1 /mnt/disks/disk14TB | sort -h | tail -n 10",
            [
                ["du", "-xh", "--max-depth=1", "/mnt/disks/disk14TB"],
                ["sort", "-h"],
                ["tail", "-n", "10"],
            ],
        ),
        ("grep 'a|b' /var/log/syslog", [["grep", "a|b", "/var/log/syslog"]]),
        (
            'grep "failed password" /var/log/auth.log',
            [["grep", "failed password", "/var/log/auth.log"]],
        ),
        ("ss -tlnp | grep ':8080'", [["ss", "-tlnp"], ["grep", ":8080"]]),
        (
            "find /var/log -name '*.log' -size +10M",
            [["find", "/var/log", "-name", "*.log", "-size", "+10M"]],
        ),
        ("find /etc -name '*secret*'", [["find", "/etc", "-name", "*secret*"]]),
        ("cat /etc/passwd", [["cat", "/etc/passwd"]]),
        ("top | head -n 15", [["top", "-bn1"], ["head", "-n", "15"]]),
        ("top -bn1 | head -n 15", [["top", "-bn1"], ["head", "-n", "15"]]),
        ("ip -br addr show", [["ip", "-br", "addr", "show"]]),
        ("ip route", [["ip", "route"]]),
        ("systemctl list-units --failed", [["systemctl", "--no-pager", "list-units", "--failed"]]),
        ("mount", [["mount"]]),
        ("uname -a", [["uname", "-a"]]),
        ("tail -n 50 /var/log/syslog", [["tail", "-n", "50", "/var/log/syslog"]]),
        ("tail -n5 /var/log/syslog", [["tail", "-n5", "/var/log/syslog"]]),
    ],
)
def test_allowed_commands_parse(line: str, stages: list[list[str]]) -> None:
    assert parse_pipeline(line).stages == stages


def test_journalctl_gets_pager_off_and_a_default_limit() -> None:
    assert parse_pipeline("journalctl -u logrotate").stages == [
        ["journalctl", "-n", "200", "--no-pager", "-u", "logrotate"]
    ]
    # an explicit limit or time window is respected
    assert parse_pipeline("journalctl -u x -n 20").stages == [
        ["journalctl", "--no-pager", "-u", "x", "-n", "20"]
    ]
    assert parse_pipeline("journalctl --since '2 hours ago' -p err").stages[0][:2] == [
        "journalctl",
        "--no-pager",
    ]


def test_systemctl_status_and_bare_systemctl() -> None:
    assert parse_pipeline("systemctl status logrotate.service").stages == [
        ["systemctl", "--no-pager", "status", "logrotate.service"]
    ]
    assert parse_pipeline("systemctl").stages == [["systemctl", "list-units", "--no-pager"]]


def test_quoted_operators_are_just_text() -> None:
    assert split_stages("grep ';' file | wc -l") == [["grep", ";", "file"], ["wc", "-l"]]
    assert split_stages("grep 'a$' file") == [["grep", "a$", "file"]]
    assert split_stages("grep 'x > y' file") == [["grep", "x > y", "file"]]


def test_grep_pattern_is_not_treated_as_a_path() -> None:
    # `password` as a search word is fine; as a file name it is not
    assert parse_pipeline("grep -i password /etc/hosts").stages == [
        ["grep", "-i", "password", "/etc/hosts"]
    ]
    assert parse_pipeline("grep -e password /etc/hosts").stages[0][-1] == "/etc/hosts"
    with pytest.raises(Refused):
        parse_pipeline("grep root /etc/shadow")


def test_recursive_grep_excludes_secret_files() -> None:
    argv = parse_pipeline("grep -rn error /etc").stages[0]
    assert "--exclude=.env" in argv
    assert "--exclude=*.pem" in argv
    assert "--exclude-dir=.ssh" in argv
    assert argv[-3:] == ["-rn", "error", "/etc"]
    plain = parse_pipeline("grep error /etc/hosts").stages[0]
    assert not any(a.startswith("--exclude") for a in plain)


def test_refusal_messages_guide_the_model() -> None:
    with pytest.raises(Refused, match="Run ONE command"):
        parse_pipeline("ls; id")
    with pytest.raises(Refused, match="container_inspect"):
        parse_pipeline("docker ps")
    with pytest.raises(Refused, match="Allowed commands"):
        parse_pipeline("curl x")
    with pytest.raises(Refused, match="deny list"):
        parse_pipeline("cat /etc/shadow")


# --------------------------------------------------------------------------- path guard


@pytest.mark.parametrize(
    "path",
    [
        "/etc/shadow",
        "/etc/gshadow-",
        "/home/a/.ssh/config",
        "/home/a/.ssh",
        "/root/.gnupg/pubring.kbx",
        "/home/a/.ssh/id_ed25519.pub",
        "/etc/ssh/ssh_host_rsa_key",
        "/etc/ssl/private/x",
        "/etc/letsencrypt/live/x/fullchain.pem",
        "/srv/app/.env",
        "/srv/app/.env.production",
        "/srv/app/prod.env",
        "/srv/app/api_secret.json",
        "/srv/app/Passwords.txt",
        "/srv/app/credentials.json",
        "/srv/cert.pem",
        "/srv/tls.key",
        "/proc/123/environ",
        "/home/a/.mozilla/firefox/p/logins.json",
        "/home/a/.config/google-chrome/Default/Login Data",
        "/etc/wireguard/wg0.conf",
        "/data/monitor.db",
    ],
)
def test_deny_list(path: str) -> None:
    assert denied_reason(path) is not None


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "/etc/logrotate.conf",
        "/etc/logrotate.d/rsyslog",
        "/var/log/syslog",
        "/mnt/disks/disk14TB/media/film.mkv",
        "/proc/1/status",
        "/etc/ssh/sshd_config",
        "/etc/hosts",
    ],
)
def test_normal_paths_are_allowed(path: str) -> None:
    assert denied_reason(path) is None


def test_monitor_data_dir_from_mountinfo() -> None:
    mountinfo = (
        "1 0 8:1 / / rw - ext4 /dev/sda1 rw\n"
        "2 1 8:1 /opt/asm/data /data rw,relatime - ext4 /dev/sda1 rw\n"
        "3 1 8:1 /home/u/media /media rw - ext4 /dev/sda1 rw\n"
    )
    dirs = monitor_data_dirs(mountinfo)
    assert dirs == ("/opt/asm/data",)
    guard = PathGuard(extra_dirs=dirs)
    assert guard.reason("/opt/asm/data/settings.json")
    assert guard.reason("/opt/asm/data")
    assert guard.reason("/opt/asm/database") is None
    assert monitor_data_dirs("1 0 8:1 / /data rw - ext4 /dev/sda1 rw\n") == ()


@pytest.mark.skipif(os.name != "posix", reason="symlinks and globbing under a root")
def test_guard_follows_symlinks_and_expands_globs(tmp_path: Path) -> None:
    root = tmp_path / "host"
    (root / "etc" / "logrotate.d").mkdir(parents=True)
    (root / "etc" / "logrotate.d" / "apt").write_text("a")
    (root / "etc" / "logrotate.d" / "app.env").write_text("SECRET=1")
    (root / "etc" / "logrotate.d" / "rsyslog").write_text("b")
    (root / "etc" / "shadow").write_text("root:x")
    (root / "etc" / "innocent").symlink_to("shadow")
    guard = PathGuard(root)
    assert guard.reason("/etc/innocent") is not None  # symlink to shadow
    assert guard.reason("/etc/logrotate.d/apt") is None
    matches, dropped = guard.expand("/etc/logrotate.d/*")
    assert matches == ["/etc/logrotate.d/apt", "/etc/logrotate.d/rsyslog"]
    assert dropped == 1
    plan = parse_pipeline("cat /etc/logrotate.d/*", guard)
    assert plan.stages == [["cat", "/etc/logrotate.d/apt", "/etc/logrotate.d/rsyslog"]]
    assert plan.notes and "skipped" in plan.notes[0]
    assert guard.expand("/etc/nothing/*") == (["/etc/nothing/*"], 0)


# --------------------------------------------------------------------------- output


def test_human_sizes() -> None:
    assert human(0) == "0 B"
    assert human(1536) == "1.5 KB"
    assert human(12 * 1024**4) == "12.0 TB"
    assert human(None) == "?"


def test_headtail_keeps_start_and_end_and_counts_cut_lines() -> None:
    ht = HeadTail(head=100, tail=100)
    for i in range(1000):
        ht.feed(f"line {i}\n".encode())
    text = ht.render()
    assert text.startswith("line 0\n")
    assert text.rstrip().endswith("line 999")
    assert "lines cut" in text
    cut = int(text.split("… ")[1].split(" lines")[0])
    assert 900 < cut < 1000
    assert len(text) < 400
    small = HeadTail(head=100, tail=100)
    small.feed(b"a\nb\n")
    assert small.render() == "a\nb\n"
    assert not small.truncated


def test_scrub_output() -> None:
    text = (
        "db_password = hunter2hunter2\n"
        "api_key: abcdef123456\n"
        'secret="s3cr3t-value"\n'
        "url = postgres://admin:pa55w0rd@db:5432/x\n"
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----\n"
        "Failed password for root from 1.2.3.4\n"
        "normal = value\n"
    )
    out = scrub_output(text)
    for leaked in ("hunter2", "abcdef123456", "s3cr3t", "pa55w0rd", "MIIEow"):
        assert leaked not in out
    assert "Failed password for root from 1.2.3.4" in out
    assert "normal = value" in out
    assert out.count("[redacted]") >= 4


# --------------------------------------------------------------------------- attribution

CONTAINERS: list[dict[str, Any]] = [
    {
        "Id": "a" * 64,
        "Names": ["/jellyfin"],
        "Image": "jellyfin/jellyfin",
        "Labels": {"com.docker.compose.service": "jellyfin"},
        "Mounts": [
            {"Type": "bind", "Source": "/mnt/disks/disk14TB/media",
             "Destination": "/data", "RW": False},
            {"Type": "volume", "Name": "jf_config",
             "Source": "/var/lib/docker/volumes/jf_config/_data",
             "Destination": "/config", "RW": True},
        ],
    },
    {
        "Id": "b" * 64,
        "Names": ["/radarr"],
        "Image": "lscr.io/linuxserver/radarr",
        "Labels": {},
        "Mounts": [
            {"Type": "bind", "Source": "/mnt/disks/disk14TB", "Destination": "/data", "RW": True},
        ],
    },
]  # fmt: skip


def _locations() -> attribution.Locations:
    loc = attribution.Locations()
    attribution.add_docker(loc, CONTAINERS, {"DockerRootDir": "/var/lib/docker"})
    attribution.add_arr(
        loc, [{"app": "radarr", "instance": "main", "path": "/data/movies"}], CONTAINERS
    )
    return loc


def test_attribution_docker_mounts_volumes_and_arr_roots() -> None:
    loc = _locations()
    media = loc.owners("/mnt/disks/disk14TB/media")
    assert any("bind mount of container 'jellyfin'" in o and "read-only" in o for o in media)
    assert any("inside bind mount of container 'radarr'" in o for o in media)
    movies = loc.owners("/mnt/disks/disk14TB/movies")
    assert any("radarr 'main' root folder /data/movies" in o for o in movies)
    vol = loc.owners("/var/lib/docker/volumes/jf_config/_data")
    assert any("Docker volume 'jf_config'" in o and "jellyfin" in o for o in vol)
    assert any("Docker data root" in o for o in loc.owners("/var/lib/docker/overlay2/x"))
    assert loc.owners("/var/cache/apt/archives")[0].startswith("apt package cache")
    assert loc.owners("/home/nobody-knows") == []


def test_attribution_arr_root_without_a_matching_container() -> None:
    loc = attribution.Locations()
    attribution.add_arr(loc, [{"app": "sonarr", "instance": "tv", "path": "/tv"}], CONTAINERS)
    assert "host mapping unknown" in loc.owners("/tv")[0]


def test_attribution_inside_lists_owners_below_a_path() -> None:
    below = _locations().inside("/mnt/disks/disk14TB")
    paths = [r["path"] for r in below]
    assert "/mnt/disks/disk14TB/media" in paths
    assert "/mnt/disks/disk14TB/movies" in paths
    assert "/var/lib/docker" not in paths


# --------------------------------------------------------------------------- disk scan

DU = {
    "/d/media": 9 * 1024**4,
    "/d/media/movies": 6 * 1024**4,
    "/d/media/tv": 3 * 1024**4,
    "/d/downloads": 2 * 1024**4,
    "/d/appdata": 5 * 1024**3,
}


class FakeRun:
    """A stand-in for Sandbox.run answering `ls` and `du` from a table."""

    def __init__(self, slow: set[str] | None = None, delay: float = 0.0):
        self.calls: list[list[str]] = []
        self.slow = slow or set()
        self.delay = delay

    def __call__(self, stages: list[list[str]], **kw: Any) -> RunResult:
        argv = stages[0]
        self.calls.append(argv)
        if argv[0] == "ls":
            names = sorted({p.split("/")[2] for p in DU if p.startswith(argv[-1] + "/")})
            return RunResult(0, "\n".join(names) + "\n")
        if argv[0] == "du":
            target = argv[-1]
            depth = int(next(a for a in argv if a.startswith("--max-depth=")).split("=")[1])
            if target in self.slow:
                time.sleep(min(self.delay, kw["timeout"]))
                return RunResult(None, "", stopped="timeout")
            rows = [
                (p, b)
                for p, b in DU.items()
                if (p == target or p.startswith(target + "/"))
                and p.count("/") - target.count("/") <= depth
            ]
            rows.sort(key=lambda r: -r[0].count("/"))
            return RunResult(0, "".join(f"{b}\t{p}\n" for p, b in rows), stderr="")
        raise AssertionError(argv)


def test_disk_scan_breakdown_with_owners() -> None:
    run = FakeRun()
    scanned = disk.scan(run, "/d", 1)
    assert scanned.complete
    assert [e.path for e in scanned.entries[:3]] == ["/d/media", "/d/downloads", "/d/appdata"]
    result = disk.summarize(scanned, "/d", 1, lambda p: ["owner of " + p], [])
    assert result["largest"][0] == {
        "path": "/d/media",
        "size": "9.0 TB",
        "percent": 81.8,
        "owner": ["owner of /d/media"],
    }
    assert result["total_scanned"] == "11.0 TB"
    assert "approximate" not in result
    # one `du` per child, each with --max-depth=0
    assert sum(1 for c in run.calls if c[0] == "du") == 3
    assert all("--max-depth=0" in c for c in run.calls if c[0] == "du")


def test_disk_scan_depth_two_lists_subdirectories() -> None:
    scanned = disk.scan(FakeRun(), "/d", 2)
    paths = [e.path for e in scanned.entries]
    assert paths[0] == "/d/media"
    assert "/d/media/movies" in paths


def test_disk_scan_stops_at_the_budget_and_says_so() -> None:
    run = FakeRun(slow={"/d/media"}, delay=5.0)
    started = time.monotonic()
    scanned = disk.scan(run, "/d", 1, budget_s=1.0)
    assert time.monotonic() - started < 4
    assert not scanned.complete
    assert scanned.unfinished == ["/d/media"]
    result = disk.summarize(scanned, "/d", 1, lambda p: [], [])
    assert "approximate" in result
    assert "scan stopped" in result["approximate"]
    assert "/d/media" in result["approximate"]
    assert result["total_scanned"].startswith("at least")
    assert [r["path"] for r in result["largest"]] == ["/d/downloads", "/d/appdata"]
    # the model cannot mistake it for the disk's usage: PARTIAL, what is missing, no percentages
    assert result["result"].startswith("PARTIAL")
    assert "NOT measured" in result["approximate"] and "/d/media" in result["approximate"]
    assert "NOT the disk's usage" in result["warning"]
    assert all("percent" not in r for r in result["largest"])


def test_disk_scan_missing_path_raises() -> None:
    def run(stages: list[list[str]], **kw: Any) -> RunResult:
        return RunResult(2, "", stderr="ls: cannot access '/nope': No such file or directory")

    with pytest.raises(FileNotFoundError, match="No such file"):
        disk.scan(run, "/nope", 1)


def test_disk_scan_reports_unreadable_entries() -> None:
    def run(stages: list[list[str]], **kw: Any) -> RunResult:
        if stages[0][0] == "ls":
            return RunResult(0, "a\n")
        return RunResult(
            1, "100\t/x/a\n", stderr="du: cannot read directory '/x/a/b': Permission denied\n"
        )

    result = disk.summarize(disk.scan(run, "/x", 1), "/x", 1, lambda p: [], [])
    assert "1 entries could not be read" in result["unreadable"]


def test_disk_cache_ttl() -> None:
    disk.clear_cache()
    disk.remember("/d", 1, {"path": "/d"}, now=1000.0)
    assert disk.cached("/d", 1, now=1000.0 + disk.CACHE_TTL_S - 1)["cached_age_s"] > 0
    assert disk.cached("/d", 1, now=1000.0 + disk.CACHE_TTL_S + 1) is None
    assert disk.cached("/d", 2, now=1000.0) is None
    disk.clear_cache()


def test_largest_files_sorted_and_flagged_when_partial() -> None:
    def run(stages: list[list[str]], **kw: Any) -> RunResult:
        assert stages[0][0] == "find"
        assert "-xdev" in stages[0]
        return RunResult(None, "300\t/d/a\n900\t/d/b\n600\t/d/c\n", stopped="timeout")

    out = disk.largest_files(run, "/d", 2)
    assert [f["path"] for f in out["files"]] == ["/d/b", "/d/c"]
    assert out["found"] == 3
    assert "approximate" in out


# --------------------------------------------------------------------------- containers


def test_container_inspect_hides_env_values_and_summarises() -> None:
    raw = {
        "Id": "c" * 64,
        "Name": "/radarr",
        "Created": "2026-01-01T00:00:00Z",
        "RestartCount": 3,
        "State": {
            "Status": "running", "Running": True, "StartedAt": "2026-11-01T00:00:00Z",
            "ExitCode": 0, "OOMKilled": False,
            "Health": {"Status": "unhealthy", "Log": [
                {"ExitCode": 1, "Output": "curl: (7) connection refused"}]},
        },
        "Config": {
            "Image": "lscr.io/linuxserver/radarr",
            "Env": ["RADARR__AUTH__APIKEY=topsecretvalue", "TZ=Europe/Berlin", "PUID=1000"],
            "Labels": {"com.docker.compose.project": "media",
                       "com.docker.compose.service": "radarr"},
            "Cmd": ["/init"],
        },
        "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}, "NetworkMode": "media_default"},
        "Mounts": [{"Type": "bind", "Source": "/srv/radarr", "Destination": "/config", "RW": True}],
        "NetworkSettings": {
            "Ports": {"7878/tcp": [{"HostIp": "0.0.0.0", "HostPort": "7878"}], "9/udp": None},
            "Networks": {"media_default": {}},
        },
    }  # fmt: skip
    out = summarize_inspect(raw)
    text = json.dumps(out)
    assert "topsecretvalue" not in text and "Europe/Berlin" not in text
    assert out["env_names"] == ["PUID", "RADARR__AUTH__APIKEY", "TZ"]
    assert out["ports"] == ["7878->7878/tcp"]
    assert out["mounts"] == ["bind: /srv/radarr -> /config (rw)"]
    assert out["compose_service"] == "radarr"
    assert out["health"] == "unhealthy"
    assert out["health_log"][0]["output"].startswith("curl")
    assert out["restart_count"] == 3
    assert "exit_code" not in out  # running


def test_container_list_summary_sorted_running_first() -> None:
    rows = summarize_list(
        [
            {"Names": ["/b"], "Image": "i", "State": "exited", "Status": "Exited (1)"},
            {"Names": ["/a"], "Image": "i", "State": "running", "Status": "Up",
             "Ports": [{"PublicPort": 80, "PrivatePort": 8080, "Type": "tcp"}], "Mounts": [{}]},
        ]
    )  # fmt: skip
    assert [r["name"] for r in rows] == ["a", "b"]
    assert rows[0]["ports"] == ["80->8080/tcp"] and rows[0]["mounts"] == 1


# --------------------------------------------------------------------------- history


def test_extract_with_where() -> None:
    data = {
        "filesystems": [
            {"mount": "/", "used": 10},
            {"mount": "/mnt/disks/disk14TB", "used": 12_000_000_000_000},
        ],
        "memory": {"percent": 61.5},
    }
    assert history.extract(data, "memory.percent", None) == 61.5
    where = history.parse_where("mount=/mnt/disks/disk14TB")
    assert history.extract(data, "filesystems.used", where) == 12_000_000_000_000
    assert history.extract(data, "filesystems.used", history.parse_where("mount=/nope")) is None
    assert history.extract(data, "memory.nothing", None) is None
    with pytest.raises(history.MetricError):
        history.extract(data, "filesystems.used", None)
    with pytest.raises(history.MetricError):
        history.parse_where("bad")


async def test_metric_history_trend_from_samples(db: Database) -> None:
    now = 1_800_000_000.0
    for i in range(0, 7 * 24):
        ts = now - (7 * 24 - i) * 3600
        used = 12_000_000_000_000 + i * 10_000_000_000
        data = {"filesystems": [{"mount": "/mnt/disks/disk14TB", "used": used},
                                {"mount": "/", "used": 1}]}  # fmt: skip
        await db.execute(
            "INSERT INTO collector_samples (ts, collector, ok, data) VALUES (?, 'system', 1, ?)",
            (ts, json.dumps(data)),
        )
    out = await history.metric_history(
        db, "system", "filesystems.used", "mount=/mnt/disks/disk14TB", 24 * 7, now=now
    )
    assert out["samples"] > 100
    assert out["first"].startswith("10.9 TB")
    assert out["last"].startswith("12.")
    assert out["trend"].startswith("Grew by")
    assert "per day" in out["trend"]
    assert len(out["series"]) <= history.MAX_POINTS + 1
    empty = await history.metric_history(db, "system", "memory.percent", None, 24, now=now)
    assert empty["samples"] == 0


QBIT_CONF = r"""[Preferences]
WebUI\Address=*
WebUI\CSRFProtection=true
WebUI\HostHeaderValidation=true
WebUI\Password_PBKDF2="@ByteArray(Zq3t1xT5Zm9YkQ==:Qm9ndXNIYXNoVmFsdWU9PQ==)"
WebUI\Port=8080
WebUI\ServerDomains=qb.example.test
WebUI\Username=admin
"""


def test_qbittorrent_conf_is_readable_but_its_password_hash_is_scrubbed() -> None:
    """The chat looks at the WebUI settings (host header validation, server domains) of the
    qBittorrent container through its bind mount; the password hash must not reach the model."""
    path = "/srv/qbit/config/qBittorrent/qBittorrent.conf"
    plan = parse_pipeline(f"grep -i webui {path}")
    assert path in plan.stages[0]  # not on the deny list
    shown = scrub_output(QBIT_CONF)
    assert "ByteArray" not in shown and "Zq3t1x" not in shown
    assert r"WebUI\HostHeaderValidation=true" in shown
    assert r"WebUI\ServerDomains=qb.example.test" in shown
    assert r"WebUI\Username=admin" in shown
    unquoted = scrub_output("WebUI\\Password_PBKDF2=@ByteArray(abc:def)\n")
    assert "abc" not in unquoted
