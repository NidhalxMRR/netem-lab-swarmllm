#!/usr/bin/env python3
"""netem-lab — a rootless, isolated WAN emulator.

WHY THIS EXISTS
    SwarmLLM ranks peers with a cost model built from latency and speed terms
    that has never been tested against real delay and loss (maintainer's own
    words: "the biggest untested surface in the project"). arXiv 2602.16760
    measured a 10x throughput swing between two providers in the SAME city --
    1.2 tok/s at 200-400ms RTT vs 11-17 tok/s at 80-100ms -- purely from
    network routing. A cost model that cannot see that difference will pick
    the wrong peer every time.

    This harness shapes a link to known conditions so that claim is testable.

ISOLATION
    Everything runs inside an unprivileged user+network namespace. No root, no
    sudo. The host's real interfaces, routes and qdiscs are untouched and
    unreachable -- production services on this box cannot be affected.

TOPOLOGY
        [ client ns ]  veth-c <--shaped--> veth-s  [ server ns ]
           10.0.0.1                                   10.0.0.2

    The two veth ends MUST sit in different namespaces. With both ends in one
    namespace the kernel short-circuits delivery and the egress qdisc never
    runs -- shaping silently becomes a no-op. An earlier version of this
    harness had exactly that bug; it reported 0.169ms for a 100ms profile.
    Check 4 below exists to catch it.

    Shaping is applied to BOTH ends, half the RTT each, so the link is
    symmetric like a real WAN path rather than delayed in one direction only.

USAGE
    python3 netem-lab.py verify              self-test (run this first)
    python3 netem-lab.py list                show profiles
    python3 netem-lab.py sweep               measure every profile -> TSV
    python3 netem-lab.py profile <name>      measure one
    python3 netem-lab.py exec <name> -- cmd  run a command under that profile

    Re-execs itself inside the namespace, so no wrapper needed.
"""
import ctypes, json, os, subprocess, sys, time

IP, TC = "/usr/sbin/ip", "/usr/sbin/tc"
CLONE_NEWNET, CLONE_NEWUSER = 0x40000000, 0x10000000
CLIENT_IP, SERVER_IP = "10.0.0.1", "10.0.0.2"

# Profiles named after measured real-world conditions, not round numbers, so a
# result maps onto something. rtt is ROUND TRIP; the harness halves it per side.
PROFILES = {
    "lan":         dict(rtt=1,   jitter=0,   loss=0.0, rate=None,
                        note="same rack"),
    "metro":       dict(rtt=20,  jitter=2,   loss=0.0, rate=None,
                        note="regional, same metro (paper projects 15-19 tok/s)"),
    "runpod-tx":   dict(rtt=80,  jitter=5,   loss=0.0, rate=None,
                        note="measured RunPod US-TX: 11-17 tok/s"),
    "wan-typical": dict(rtt=100, jitter=10,  loss=0.1, rate=None,
                        note="ordinary internet path"),
    "vast-proxy":  dict(rtt=300, jitter=40,  loss=0.5, rate=None,
                        note="measured VAST.ai proxy routing: 1.2 tok/s"),
    "wan-bad":     dict(rtt=800, jitter=100, loss=2.0, rate=None,
                        note="satellite / congested mobile"),
    "thin-uplink": dict(rtt=100, jitter=10,  loss=0.1, rate="10mbit",
                        note="the 513KB-verify-round killer"),
    "lossy-quic":  dict(rtt=60,  jitter=15,  loss=3.0, rate=None,
                        note="quinn 1024-gap repro candidate"),
}


def _run(*args, ns_pid=None, check=False):
    cmd = list(args)
    if ns_pid:
        cmd = ["nsenter", "-t", str(ns_pid), "-n"] + cmd
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(cmd)}\n  {r.stderr.strip()}")
    return r


def _in_namespace():
    """True once we hold CAP_NET_ADMIN in our own netns."""
    return os.environ.get("NETEM_LAB_INNER") == "1"


def _reexec_in_namespace():
    """Re-exec self inside a fresh unprivileged user+net namespace."""
    os.environ["NETEM_LAB_INNER"] = "1"
    os.execvp("unshare", ["unshare", "-Urn", sys.executable] + sys.argv)


class ShapedLink:
    """A veth pair split across two netns, shaped symmetrically with netem."""

    def __init__(self, rtt, jitter=0, loss=0.0, rate=None):
        self.rtt, self.jitter, self.loss, self.rate = rtt, jitter, loss, rate
        self.pid = None
        self._wfd = None

    def __enter__(self):
        rfd, self._wfd = os.pipe()
        pid = os.fork()
        if pid == 0:                       # child: park in its own netns
            os.close(self._wfd)
            libc = ctypes.CDLL("libc.so.6", use_errno=True)
            if libc.unshare(CLONE_NEWNET) != 0:
                os._exit(9)
            os.read(rfd, 1)
            os._exit(0)
        os.close(rfd)
        self.pid = pid
        time.sleep(0.4)

        _run(IP, "link", "set", "lo", "up")
        # A run that dies mid-teardown leaves veth-c behind, and every later
        # profile then fails with "RTNETLINK answers: File exists" -- which is
        # how the first divergence sweep lost its last two profiles. The pair is
        # ours by construction (private netns), so clearing a stale one is safe
        # and makes the harness restartable.
        _run(IP, "link", "del", "veth-c", check=False)
        _run(IP, "link", "add", "veth-c", "type", "veth",
             "peer", "name", "veth-s", check=True)
        _run(IP, "link", "set", "veth-s", "netns", str(pid), check=True)
        _run(IP, "addr", "add", f"{CLIENT_IP}/24", "dev", "veth-c", check=True)
        _run(IP, "link", "set", "veth-c", "up", check=True)
        _run(IP, "addr", "add", f"{SERVER_IP}/24", "dev", "veth-s",
             ns_pid=pid, check=True)
        _run(IP, "link", "set", "veth-s", "up", ns_pid=pid, check=True)
        _run(IP, "link", "set", "lo", "up", ns_pid=pid)

        if self.rtt or self.loss or self.rate:
            self._shape()
        return self

    def _shape(self):
        owd, ojit = self.rtt / 2.0, self.jitter / 2.0
        args = ["delay", f"{owd}ms"]
        if ojit:
            args += [f"{ojit}ms", "distribution", "normal"]
        if self.loss:
            args += ["loss", f"{self.loss}%"]
        if self.rate:
            args += ["rate", self.rate]
        _run(TC, "qdisc", "add", "dev", "veth-c", "root", "netem",
             *args, check=True)
        _run(TC, "qdisc", "add", "dev", "veth-s", "root", "netem",
             *args, ns_pid=self.pid, check=True)

    def ping(self, count=30, interval=None, warmup=True):
        """Measure what the link ACTUALLY does. Never trust the set value.

        A cold link pays for ARP resolution, and that exchange itself crosses
        the shaped path -- at 800ms RTT it inflated the first burst to ~997ms
        against an 800ms target. The warmup burst is discarded so the reported
        figure is steady-state.

        Interval and deadline are derived from the RTT. A fixed interval sends
        faster than a slow link can drain, so ping's deadline expires with
        packets still in flight and they are counted as LOST -- a 0%-loss
        800ms profile read 34.8% before this. Loss numbers must come from the
        link, never from the harness outrunning it.
        """
        if interval is None:
            interval = max(0.05, (self.rtt / 1000.0) * 1.2)
        flight = (self.rtt / 1000.0) + 1.0
        deadline = max(10, int(count * interval + flight * 3))

        if warmup:
            subprocess.run(
                ["ping", "-c", "3", "-i", str(interval),
                 "-W", str(max(2, int(flight * 2))), "-q", SERVER_IP],
                capture_output=True, text=True)
        r = subprocess.run(
            ["ping", "-c", str(count), "-i", str(interval),
             "-W", str(max(2, int(flight * 2))),
             "-w", str(deadline), "-q", SERVER_IP],
            capture_output=True, text=True)
        out = {"sent": 0, "recv": 0, "rtt_avg": None, "mdev": None}
        for line in r.stdout.splitlines():
            if "packets transmitted" in line:
                p = line.split()
                out["sent"], out["recv"] = int(p[0]), int(p[3])
            if line.startswith(("rtt", "round-trip")):
                v = line.split("=")[1].split("/")
                out["rtt_avg"], out["mdev"] = float(v[1]), float(v[3].split()[0])
        return out

    def __exit__(self, *exc):
        if self._wfd is not None:
            try:
                os.write(self._wfd, b"x")
                os.close(self._wfd)
            except OSError:
                pass
        if self.pid:
            try:
                os.waitpid(self.pid, 0)
            except ChildProcessError:
                pass
        return False


def cmd_list():
    print(f"{'PROFILE':<13}{'RTT':>6}{'JIT':>5}{'LOSS':>6}{'RATE':>9}  NOTE")
    for n, p in PROFILES.items():
        print(f"{n:<13}{p['rtt']:>6}{p['jitter']:>5}{p['loss']:>6}"
              f"{str(p['rate'] or '-'):>9}  {p['note']}")


def _measure(name, p):
    with ShapedLink(p["rtt"], p["jitter"], p["loss"], p["rate"]) as link:
        m = link.ping()
    lost = m["sent"] - m["recv"]
    loss_pct = (lost / m["sent"] * 100) if m["sent"] else 100.0
    return dict(profile=name, set_rtt=p["rtt"], meas_rtt=m["rtt_avg"],
                mdev=m["mdev"], set_loss=p["loss"], meas_loss=round(loss_pct, 1),
                rate=p["rate"] or "-")


def cmd_sweep():
    print(f"# netem-lab sweep {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}")
    print("# rootless netns; host networking untouched")
    print("PROFILE\tSET_RTT\tMEAS_RTT\tMDEV\tSET_LOSS%\tMEAS_LOSS%\tRATE")
    rows = []
    for name, p in PROFILES.items():
        r = _measure(name, p)
        rows.append(r)
        print(f"{r['profile']}\t{r['set_rtt']}\t{r['meas_rtt']}\t{r['mdev']}"
              f"\t{r['set_loss']}\t{r['meas_loss']}\t{r['rate']}")
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sweep.json")
    with open(out, "w") as f:
        json.dump(rows, f, indent=2)
    print(f"\n# saved: {out}")


def cmd_profile(name):
    if name not in PROFILES:
        print(f"unknown profile: {name}"); cmd_list(); sys.exit(1)
    r = _measure(name, PROFILES[name])
    print("PROFILE\tSET_RTT\tMEAS_RTT\tMDEV\tSET_LOSS%\tMEAS_LOSS%\tRATE")
    print(f"{r['profile']}\t{r['set_rtt']}\t{r['meas_rtt']}\t{r['mdev']}"
          f"\t{r['set_loss']}\t{r['meas_loss']}\t{r['rate']}")


def cmd_exec(name, argv):
    if name not in PROFILES:
        print(f"unknown profile: {name}"); sys.exit(1)
    p = PROFILES[name]
    with ShapedLink(p["rtt"], p["jitter"], p["loss"], p["rate"]) as link:
        print(f"# shaped: {name} (~{p['rtt']}ms RTT, {p['loss']}% loss)  "
              f"server={SERVER_IP}  server_ns_pid={link.pid}", file=sys.stderr)
        sys.exit(subprocess.run(argv).returncode)


def cmd_verify():
    """Prove the harness CAN fail before trusting any number it produces.

    A harness that reports plausible numbers whether or not shaping works is
    worse than no harness. Check 4 is the mutation test: an earlier version
    with both veth ends in one namespace passed checks 1 and 5 while doing
    nothing at all.
    """
    fails = []

    print("=== netem-lab self-test ===")

    with ShapedLink(0) as link:
        base = link.ping(count=10)["rtt_avg"]
    ok = base is not None and base < 5
    print(f"1. unshaped baseline < 5ms .................. "
          f"{'PASS' if ok else 'FAIL'} ({base} ms)")
    if not ok: fails.append(1)

    with ShapedLink(100) as link:
        m100 = link.ping(count=12)["rtt_avg"]
    ok = m100 is not None and 95 <= m100 <= 115
    print(f"2. 100ms target measures 95-115ms ........... "
          f"{'PASS' if ok else 'FAIL'} ({m100} ms)")
    if not ok: fails.append(2)

    with ShapedLink(800) as link:
        m800 = link.ping(count=8, interval=0.25)["rtt_avg"]
    ok = m800 is not None and 780 <= m800 <= 880
    print(f"3. 800ms target measures 780-880ms .......... "
          f"{'PASS' if ok else 'FAIL'} ({m800} ms)")
    if not ok: fails.append(3)

    ok = base and m100 and m100 > base * 10
    print(f"4. MUTATION: shaped must differ from unshaped "
          f"{'PASS' if ok else 'FAIL'} "
          f"({base} -> {m100} ms)")
    if not ok: fails.append(4)

    with ShapedLink(50, loss=10.0) as link:
        lm = link.ping(count=120, interval=0.03)
    lost_pct = ((lm["sent"] - lm["recv"]) / lm["sent"] * 100) if lm["sent"] else 0
    # Loss is applied to BOTH directions, so a round trip survives only if both
    # legs survive: 1 - 0.9^2 = 19%. A one-sided reading of 10% would mean the
    # return path is unshaped -- i.e. the topology bug is back.
    ok = 12 <= lost_pct <= 27
    print(f"5. 10% each way -> ~19% round-trip loss .... "
          f"{'PASS' if ok else 'FAIL'} ({lost_pct:.1f}%)")
    if not ok: fails.append(5)

    host = subprocess.run([TC, "qdisc", "show"], capture_output=True, text=True)
    leaked = "netem" in host.stdout
    print(f"6. host qdiscs clean (no leak) .............. "
          f"{'FAIL' if leaked else 'PASS'}")
    if leaked: fails.append(6)

    print()
    if fails:
        print(f"SELF-TEST FAILED (checks {fails}) — do not trust measurements")
        return 1
    print("ALL CHECKS PASSED — harness is trustworthy")
    return 0


def main():
    if not _in_namespace():
        _reexec_in_namespace()
    args = sys.argv[1:]
    if not args:
        print(__doc__); return 1
    c = args[0]
    if c == "list":    cmd_list(); return 0
    if c == "verify":  return cmd_verify()
    if c == "sweep":   cmd_sweep(); return 0
    if c == "profile": cmd_profile(args[1]); return 0
    if c == "exec":
        sep = args.index("--")
        cmd_exec(args[1], args[sep + 1:]); return 0
    print(f"unknown command: {c}"); return 1


if __name__ == "__main__":
    sys.exit(main())
