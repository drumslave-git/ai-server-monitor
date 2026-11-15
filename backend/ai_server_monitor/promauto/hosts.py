"""Which Prometheus instances are not the monitored server.

The monitor watches one Linux host itself. A Prometheus often scrapes more: another
machine's exporter (an IP address or a Windows exporter) must not be mistaken for the
monitored server, so the digest marks those results "other host".
"""

from __future__ import annotations

import ipaddress
import re

_PORT = re.compile(r":\d+$")


def instance_host(instance: str) -> str:
    """'192.168.88.30:9182' -> '192.168.88.30'; '[::1]:9100' -> '::1'; 'node:9100' -> 'node'."""
    text = _PORT.sub("", instance.strip())
    return text.strip("[]")


def is_other_host(instance: str = "", job: str = "") -> bool:
    """Heuristic: a Windows exporter, or a scrape address that is a LAN/public IP.

    Compose service names ('node-exporter:9100'), loopback and docker-internal
    addresses (172.16/12) count as the monitored server.
    """
    if "windows" in job.lower():
        return True
    host = instance_host(instance)
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.is_loopback:
        return False
    return not (ip.version == 4 and ip in ipaddress.ip_network("172.16.0.0/12"))
