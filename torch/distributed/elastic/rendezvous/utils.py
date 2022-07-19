# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import fcntl
import ipaddress
import random
import re
import socket
import struct
import time
import weakref
from datetime import timedelta
from threading import Event, Thread
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

__all__ = ['parse_rendezvous_endpoint']


# From https://stackoverflow.com/a/27494105.
def nic_ip_address(nic_name: str) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    return socket.inet_ntoa(fcntl.ioctl(
        s.fileno(),
        0x8915,  # SIOCGIFADDR
        struct.pack('256s', nic_name[:15].encode("UTF-8"))
    )[20:24])


# Adapted from https://stackoverflow.com/a/27494105.
def nic_info() -> List[Tuple[str, str]]:
    """Return a list of tuples containing each NIC's hostname and its IPv4."""
    nics: List[Tuple[str, str]] = []
    try:
        if_nameindex = socket.if_nameindex()
    except OSError:
        return nics

    for (_, nic_name) in if_nameindex:
        try:
            ip_addr = nic_ip_address(nic_name)
        except OSError:
            continue

        hostname = socket.gethostbyaddr(ip_addr)[0]
        nics.append((hostname, ip_addr))
    return nics


def _parse_rendezvous_config(config_str: str) -> Dict[str, str]:
    """Extract key-value pairs from a rendezvous configuration string.

    Args:
        config_str:
            A string in format <key1>=<value1>,...,<keyN>=<valueN>.
    """
    config: Dict[str, str] = {}

    config_str = config_str.strip()
    if not config_str:
        return config

    key_values = config_str.split(",")
    for kv in key_values:
        key, *values = kv.split("=", 1)

        key = key.strip()
        if not key:
            raise ValueError(
                "The rendezvous configuration string must be in format "
                "<key1>=<value1>,...,<keyN>=<valueN>."
            )

        value: Optional[str]
        if values:
            value = values[0].strip()
        else:
            value = None
        if not value:
            raise ValueError(
                f"The rendezvous configuration option '{key}' must have a value specified."
            )

        config[key] = value
    return config


def _try_parse_port(port_str: str) -> Optional[int]:
    """Try to extract the port number from ``port_str``."""
    if port_str and re.match(r"^[0-9]{1,5}$", port_str):
        return int(port_str)
    return None


def parse_rendezvous_endpoint(endpoint: Optional[str], default_port: int) -> Tuple[str, int]:
    """Extract the hostname and the port number from a rendezvous endpoint.

    Args:
        endpoint:
            A string in format <hostname>[:<port>].
        default_port:
            The port number to use if the endpoint does not include one.

    Returns:
        A tuple of hostname and port number.
    """
    if endpoint is not None:
        endpoint = endpoint.strip()

    if not endpoint:
        return ("localhost", default_port)

    # An endpoint that starts and ends with brackets represents an IPv6 address.
    if endpoint[0] == "[" and endpoint[-1] == "]":
        host, *rest = endpoint, *[]
    else:
        host, *rest = endpoint.rsplit(":", 1)

    # Sanitize the IPv6 address.
    if len(host) > 1 and host[0] == "[" and host[-1] == "]":
        host = host[1:-1]

    if len(rest) == 1:
        port = _try_parse_port(rest[0])
        if port is None or port >= 2 ** 16:
            raise ValueError(
                f"The port number of the rendezvous endpoint '{endpoint}' must be an integer "
                "between 0 and 65536."
            )
    else:
        port = default_port

    if not re.match(r"^[\w\.:-]+$", host):
        raise ValueError(
            f"The hostname of the rendezvous endpoint '{endpoint}' must be a dot-separated list of "
            "labels, an IPv4 address, or an IPv6 address."
        )

    return host, port


def _matches_machine_hostname(host: str) -> bool:
    """Indicate whether ``host`` matches the hostname of this machine.

    This function compares ``host`` to the hostname as well as to the IP
    addresses of this machine. Note that it may return a false negative if this
    machine has CNAME records beyond its FQDN or IP addresses assigned to
    secondary NICs.
    """
    if host == "localhost":
        return True

    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        addr = None

    if addr and addr.is_loopback:
        return True

    try:
        host_addr_list = socket.getaddrinfo(
            host, None, proto=socket.IPPROTO_TCP, flags=socket.AI_CANONNAME
        )
    except (ValueError, socket.gaierror) as _:
        host_addr_list = []

    host_ip_list = [
        host_addr_info[4][0]
        for host_addr_info in host_addr_list
    ]

    this_host = socket.gethostname()
    if host == this_host:
        return True

    host_fqdn = socket.getfqdn(host)
    addr_list = socket.getaddrinfo(
        this_host, None, proto=socket.IPPROTO_TCP, flags=socket.AI_CANONNAME
    )
    for addr_info in addr_list:
        # If we have an FQDN in the addr_info, compare it to `host`.
        if addr_info[3] and (addr_info[3] == host or addr_info[3] == host_fqdn):
            return True

        # Otherwise if `host` represents an IP address, compare it to our IP
        # address.
        if addr and addr_info[4][0] == str(addr):
            return True

        # If the IP address matches one of the provided host's IP addresses
        if addr_info[4][0] in host_ip_list:
            return True

    for (nic_host, nic_addr) in nic_info():
        if (
                nic_host == host or nic_host == host_fqdn
                or addr and nic_addr == str(addr)
        ):
            return True

    return False


def _delay(seconds: Union[float, Tuple[float, float]]) -> None:
    """Suspend the current thread for ``seconds``.

    Args:
        seconds:
            Either the delay, in seconds, or a tuple of a lower and an upper
            bound within which a random delay will be picked.
    """
    if isinstance(seconds, tuple):
        seconds = random.uniform(*seconds)
    # Ignore delay requests that are less than 10 milliseconds.
    if seconds >= 0.01:
        time.sleep(seconds)


class _PeriodicTimer:
    """Represent a timer that periodically runs a specified function.

    Args:
        interval:
            The interval, in seconds, between each run.
        function:
            The function to run.
    """

    # The state of the timer is hold in a separate context object to avoid a
    # reference cycle between the timer and the background thread.
    class _Context:
        interval: float
        function: Callable[..., None]
        args: Tuple[Any, ...]
        kwargs: Dict[str, Any]
        stop_event: Event

    _name: Optional[str]
    _thread: Optional[Thread]
    _finalizer: Optional[weakref.finalize]

    # The context that is shared between the timer and the background thread.
    _ctx: _Context

    def __init__(
        self,
        interval: timedelta,
        function: Callable[..., None],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        self._name = None

        self._ctx = self._Context()
        self._ctx.interval = interval.total_seconds()
        self._ctx.function = function  # type: ignore[assignment]
        self._ctx.args = args or ()
        self._ctx.kwargs = kwargs or {}
        self._ctx.stop_event = Event()

        self._thread = None
        self._finalizer = None

    @property
    def name(self) -> Optional[str]:
        """Get the name of the timer."""
        return self._name

    def set_name(self, name: str) -> None:
        """Set the name of the timer.

        The specified name will be assigned to the background thread and serves
        for debugging and troubleshooting purposes.
        """
        if self._thread:
            raise RuntimeError("The timer has already started.")

        self._name = name

    def start(self) -> None:
        """Start the timer."""
        if self._thread:
            raise RuntimeError("The timer has already started.")

        self._thread = Thread(
            target=self._run, name=self._name or "PeriodicTimer", args=(self._ctx,), daemon=True
        )

        # We avoid using a regular finalizer (a.k.a. __del__) for stopping the
        # timer as joining a daemon thread during the interpreter shutdown can
        # cause deadlocks. The weakref.finalize is a superior alternative that
        # provides a consistent behavior regardless of the GC implementation.
        self._finalizer = weakref.finalize(
            self, self._stop_thread, self._thread, self._ctx.stop_event
        )

        # We do not attempt to stop our background thread during the interpreter
        # shutdown. At that point we do not even know whether it still exists.
        self._finalizer.atexit = False

        self._thread.start()

    def cancel(self) -> None:
        """Stop the timer at the next opportunity."""
        if self._finalizer:
            self._finalizer()

    @staticmethod
    def _run(ctx) -> None:
        while not ctx.stop_event.wait(ctx.interval):
            ctx.function(*ctx.args, **ctx.kwargs)

    @staticmethod
    def _stop_thread(thread, stop_event):
        stop_event.set()

        thread.join()
