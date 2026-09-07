#!/usr/bin/env python3
"""Probe: can we build a two-netns shaped veth pair with no root?

Both veth ends MUST live in different network namespaces. If they share one,
Linux delivers frames internally and the egress qdisc never runs -- which is
exactly the silent no-op the first harness hit.

Strategy: parent unshares user+net (becoming root in that userns), forks a
child that unshares ANOTHER netns and parks. Parent moves veth-s into the
child's netns by PID, shapes both sides, measures.
"""
import os, subprocess, sys, time

IP, TC = "/usr/sbin/ip", "/usr/sbin/tc"

def run(*a, ns_pid=None, check=False):
    cmd = list(a)
    if ns_pid:
        cmd = ["nsenter", "-t", str(ns_pid), "-n"] + cmd
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode:
        print(f"FAIL: {' '.join(cmd)}\n  {r.stderr.strip()}", file=sys.stderr)
        sys.exit(3)
    return r

def ping_rtt(target="10.0.0.2", count=8, interval=0.15):
    r = subprocess.run(["ping", "-c", str(count), "-i", str(interval), "-q", target],
                       capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if line.startswith(("rtt", "round-trip")):
            return float(line.split("=")[1].split("/")[1])
    return None

def main():
    delay_ms = float(sys.argv[1]) if len(sys.argv) > 1 else 50.0

    # child parks in its own netns; parent moves an interface into it
    r_fd, w_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(w_fd)
        import ctypes
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        if libc.unshare(0x40000000) != 0:          # CLONE_NEWNET
            os._exit(9)
        os.read(r_fd, 1)                           # park until parent is done
        os._exit(0)

    os.close(r_fd)
    time.sleep(0.4)

    run(IP, "link", "set", "lo", "up")
    run(IP, "link", "add", "veth-c", "type", "veth", "peer", "name", "veth-s", check=True)
    mv = run(IP, "link", "set", "veth-s", "netns", str(pid))
    if mv.returncode:
        print(f"MOVE FAILED: {mv.stderr.strip()}"); os.write(w_fd, b"x"); sys.exit(3)

    run(IP, "addr", "add", "10.0.0.1/24", "dev", "veth-c", check=True)
    run(IP, "link", "set", "veth-c", "up", check=True)
    run(IP, "addr", "add", "10.0.0.2/24", "dev", "veth-s", ns_pid=pid, check=True)
    run(IP, "link", "set", "veth-s", "up", ns_pid=pid, check=True)
    run(IP, "link", "set", "lo", "up", ns_pid=pid)

    base = ping_rtt()
    print(f"unshaped RTT      : {base} ms")

    run(TC, "qdisc", "add", "dev", "veth-c", "root", "netem",
        "delay", f"{delay_ms}ms", check=True)
    run(TC, "qdisc", "add", "dev", "veth-s", "root", "netem",
        "delay", f"{delay_ms}ms", ns_pid=pid, check=True)

    shaped = ping_rtt()
    target = delay_ms * 2
    print(f"shaped RTT        : {shaped} ms   (target ~{target} ms)")

    os.write(w_fd, b"x")
    os.waitpid(pid, 0)

    if base is None or shaped is None:
        print("VERDICT: no connectivity"); sys.exit(1)
    if shaped > base * 10 and abs(shaped - target) < target * 0.2:
        print("VERDICT: SHAPING WORKS, ROOTLESS, TWO NAMESPACES")
        sys.exit(0)
    print("VERDICT: shaping did not take effect")
    sys.exit(1)

if __name__ == "__main__":
    main()
