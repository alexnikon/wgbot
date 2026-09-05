#!/usr/bin/env python3
"""Test metrics filtering in disposable Linux namespaces without host firewall changes."""

import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path


def main() -> None:
    if sys.platform != "linux" or os.geteuid() != 0:
        raise SystemExit("Run as root on Linux with iproute2, iptables and curl installed")
    prefix = "wgbot-test-" + uuid.uuid4().hex[:8]
    spaces = {name: f"{prefix}-{name}" for name in ("router", "vpn", "wan", "app")}
    created = []
    processes = []

    def run(*args, **kwargs):
        return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs)

    def ns(name, *args, **kwargs):
        return run("ip", "netns", "exec", spaces[name], *args, **kwargs)

    def firewall(*args):
        return ns("router", "iptables", "-w", *args)

    def request(name, address, allowed, source=None):
        args = ["curl", "--noproxy", "*", "-fsS", "--connect-timeout", "1", "--max-time", "2"]
        if source:
            args += ["--interface", source]
        result = subprocess.run(
            ["ip", "netns", "exec", spaces[name], *args, f"http://{address}:9200/"],
            capture_output=True,
            text=True,
        )
        assert (result.returncode == 0) == allowed, (name, address, result.stderr)

    try:
        with tempfile.TemporaryDirectory(prefix=prefix) as directory:
            Path(directory, "index.html").write_text("ok\n")
            for name in spaces:
                run("ip", "netns", "add", spaces[name])
                created.append(name)
                ns(name, "ip", "link", "set", "lo", "up")
            for name, interface, host_ip, peer_ip in (
                ("vpn", "wg12", None, "10.8.2.2/24"),
                ("wan", "wan0", "198.18.0.1/24", "198.18.0.2/24"),
                ("app", "brtest", "172.31.254.1/24", "172.31.254.2/24"),
            ):
                ns(
                    "router",
                    "ip",
                    "link",
                    "add",
                    interface,
                    "type",
                    "veth",
                    "peer",
                    "name",
                    "peer0",
                )
                ns("router", "ip", "link", "set", "peer0", "netns", spaces[name])
                ns(name, "ip", "link", "set", "peer0", "name", "eth0")
                ns(name, "ip", "addr", "add", peer_ip, "dev", "eth0")
                ns(name, "ip", "link", "set", "eth0", "up")
                ns("router", "ip", "link", "set", interface, "up")
                if host_ip:
                    ns("router", "ip", "addr", "add", host_ip, "dev", interface)
            ns("router", "sysctl", "-qw", "net.ipv4.ip_forward=1")
            for interface in ("all", "wan0"):
                ns("router", "sysctl", "-qw", f"net.ipv4.conf.{interface}.rp_filter=0")
            ns("app", "ip", "route", "add", "default", "via", "172.31.254.1")
            ns("wan", "ip", "addr", "add", "10.8.2.66/32", "dev", "eth0")
            firewall("-N", "DOCKER-USER")
            firewall("-A", "FORWARD", "-j", "DOCKER-USER")
            rules = Path(__file__).with_name("wgbot-metrics.rules.v4").read_text()
            ns("router", "iptables-restore", "--test", "--noflush", input=rules)
            ns("router", "iptables-restore", "--noflush", input=rules)

            # A wildcard listener works before the VPN address is created.
            ns(
                "router",
                sys.executable,
                "-c",
                """
import socket
with socket.socket() as sock:
    try:
        sock.bind(('10.8.2.1', 9200))
    except OSError:
        pass
    else:
        raise SystemExit('VPN address unexpectedly exists')
""",
            )
            for name, port in (("router", "9200"), ("app", "19200")):
                processes.append(
                    subprocess.Popen(
                        [
                            "ip",
                            "netns",
                            "exec",
                            spaces[name],
                            sys.executable,
                            "-m",
                            "http.server",
                            port,
                            "--bind",
                            "0.0.0.0",
                            "--directory",
                            directory,
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                )
            time.sleep(0.3)
            request("router", "127.0.0.1", True)
            print("PASS: wildcard listener starts without VPN address", flush=True)
            ns("router", "ip", "addr", "add", "10.8.2.1/24", "dev", "wg12")
            request("vpn", "10.8.2.1", True)
            request("wan", "198.18.0.1", False)
            request("wan", "198.18.0.1", False, "10.8.2.66")
            print("PASS: INPUT permits VPN/loopback and denies WAN/spoofed source", flush=True)

            # Translate to a different backend port to verify original-port matching.
            firewall(
                "-t",
                "nat",
                "-A",
                "PREROUTING",
                "-p",
                "tcp",
                "--dport",
                "9200",
                "-j",
                "DNAT",
                "--to-destination",
                "172.31.254.2:19200",
            )
            firewall("-Z", "WGBOT_METRICS")
            request("vpn", "10.8.2.1", True)
            request("wan", "198.18.0.1", False)
            request("wan", "198.18.0.1", False, "10.8.2.66")
            counters = firewall("-L", "WGBOT_METRICS", "-n", "-v", "-x").stdout
            dropped = next(int(line.split()[0]) for line in counters.splitlines() if "DROP" in line)
            assert dropped >= 2, counters
            print("PASS: DNAT permits VPN and drops WAN/spoofed source", flush=True)
    finally:
        for process in processes:
            process.terminate()
            process.wait(timeout=5)
        for name in reversed(created):
            run("ip", "netns", "delete", spaces[name])


if __name__ == "__main__":
    main()
