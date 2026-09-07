#!/usr/bin/env python3
"""divergence — does a health-check ping predict where work should go?

SwarmLLM ranks delegation candidates on `latency_ms`, an application-level
health-check reading. The scheduler's own doc comment concedes this is "still a
threshold on a proxy". This measures whether the proxy holds.

For each shaped profile we measure BOTH:
  ping_rtt      -- what the health check sees (small, infrequent message)
  bulk_seconds  -- what a delegated forward actually costs (sustained transfer)

then rank the profiles by each and compare the orderings. A proxy that predicts
is one where the two orderings agree.

The oracle is wall-clock transfer time measured outside the code under test.
Run: unshare -Urn python3 divergence.py
"""
import importlib.util
import json
import os
import socket
import statistics
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("nl", os.path.join(HERE, "netem-lab.py"))
nl = importlib.util.module_from_spec(spec)
os.environ["NETEM_LAB_INNER"] = "1"
spec.loader.exec_module(nl)

# A delegated forward moves an activation tensor. arXiv 2602.16760 measures
# ~8 KB/token for a 12B hidden state in fp16; SwarmLLM's verify round is larger.
# Both sizes are measured so the result is not an artifact of one choice.
PAYLOADS = {"activation_8kb": 8 * 1024, "verify_round_513kb": 513 * 1024}
PORT = 9899


def _serve(stop, ready):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((nl.SERVER_IP, PORT))
    srv.listen(16)
    srv.settimeout(0.5)
    ready.set()
    while not stop.is_set():
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        threading.Thread(target=_echo, args=(conn,), daemon=True).start()
    srv.close()


def _echo(conn):
    """Read a length-prefixed request, echo the same number of bytes back.

    A delegated forward is a round trip: activations out, result back. An
    echo has the same shape, so the timing includes both directions the way
    a real delegation does.
    """
    try:
        conn.settimeout(30)
        hdr = conn.recv(8)
        if len(hdr) < 8:
            return
        n = int.from_bytes(hdr, "big")
        got = 0
        while got < n:
            b = conn.recv(min(65536, n - got))
            if not b:
                return
            got += len(b)
        payload = b"x" * n
        conn.sendall(payload)
    except (OSError, socket.timeout):
        pass
    finally:
        conn.close()


def bulk_round_trip(nbytes, reps=3):
    """Time a full request/response of nbytes each way, from the client netns."""
    times = []
    for _ in range(reps):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(60)
        t0 = time.perf_counter()
        try:
            s.connect((nl.SERVER_IP, PORT))
            s.sendall(nbytes.to_bytes(8, "big") + b"x" * nbytes)
            got = 0
            while got < nbytes:
                b = s.recv(min(65536, nbytes - got))
                if not b:
                    break
                got += len(b)
            if got < nbytes:
                times.append(float("nan"))
            else:
                times.append(time.perf_counter() - t0)
        except (OSError, socket.timeout):
            times.append(float("nan"))
        finally:
            s.close()
    good = [t for t in times if t == t]
    return statistics.median(good) if good else float("nan")


def main():
    results = []
    for name, kw in nl.PROFILES.items():
        kw = {k: v for k, v in kw.items() if k != "note"}
        with nl.ShapedLink(**kw) as link:
            # the health check's view
            m = link.ping(count=20)
            ping_rtt = m["rtt_avg"]

            # the delegated forward's view -- server runs in the server netns
            stop, ready = threading.Event(), threading.Event()
            pid = link.pid
            # run the echo server inside the server namespace
            srv_proc = subprocess.Popen(
                ["nsenter", "-t", str(pid), "-n", sys.executable, "-c",
                 f"import sys; sys.path.insert(0,{HERE!r}); "
                 f"import importlib.util,os; os.environ['NETEM_LAB_INNER']='1'; "
                 f"s=importlib.util.spec_from_file_location('d',{__file__!r}); "
                 f"d=importlib.util.module_from_spec(s); s.loader.exec_module(d); "
                 f"import threading; st=threading.Event(); rd=threading.Event(); "
                 f"d._serve(st,rd)"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1.2)

            row = {"profile": name, "set_rtt": kw["rtt"], "ping_rtt": ping_rtt}
            for pname, sz in PAYLOADS.items():
                row[pname] = bulk_round_trip(sz)
            srv_proc.terminate()
            srv_proc.wait(timeout=5)
            results.append(row)
            print(f"  {name:14s} ping={ping_rtt}ms  "
                  + "  ".join(f"{k}={row[k]:.3f}s" for k in PAYLOADS
                              if row[k] == row[k]), flush=True)

    print("\n=== ordering comparison ===")
    by_ping = [r["profile"] for r in sorted(results, key=lambda r: r["ping_rtt"] or 1e9)]
    for pname in PAYLOADS:
        usable = [r for r in results if r[pname] == r[pname]]
        by_bulk = [r["profile"] for r in sorted(usable, key=lambda r: r[pname])]
        agree = by_ping[:len(by_bulk)] == by_bulk
        print(f"\n{pname}:")
        print(f"  by ping: {' < '.join(by_ping)}")
        print(f"  by bulk: {' < '.join(by_bulk)}")
        print(f"  ORDERINGS {'AGREE' if agree else 'DIVERGE'}")

    out = os.path.join(HERE, "divergence.json")
    with open(out, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nsaved: {out}")


if __name__ == "__main__":
    if os.environ.get("_DIV_INNER") != "1" and os.geteuid() != 0:
        os.environ["_DIV_INNER"] = "1"
        os.execvp("unshare", ["unshare", "-Urn", sys.executable] + sys.argv)
    main()
