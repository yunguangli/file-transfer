"""Local identity/address helpers (stdlib only).

lanlink advertises the *peer id* and *name*; the UI additionally shows this
machine's LAN address so two people can verify they picked the right device.
"""

from __future__ import annotations

import socket


def hostname() -> str:
    """Short device name, shown under the editable nickname."""
    return socket.gethostname().split(".")[0] or "this device"


def local_ip() -> str:
    """Best-effort LAN address of this machine.

    A connected UDP socket knows which source address the kernel would pick
    for the LAN without sending a single packet. Falls back to loopback when
    the machine has no route at all.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("239.255.42.99", 47555))  # multicast addr: never contacted
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def is_loopback(ip: str) -> bool:
    return ip.startswith("127.") or ip in ("::1", "localhost")
