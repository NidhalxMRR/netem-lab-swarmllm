#!/usr/bin/env python3
"""Confirmation pass: does the 513KB ordering flip repeat?

The full sweep showed lossy-quic (60ms, 3% loss) ranking BETTER than
runpod-tx (80ms, 0% loss) by ping, but WORSE by 513KB bulk transfer. One
observation of a reordering is not a finding -- a scheduler claim needs the
flip to repeat, and needs the 8KB case to NOT flip on the same links, or the
result is just noise.

Run: unshare -Urn python3 confirm_flip.py
"""
import importlib.util
import os
import subprocess
import sys
import time

HERE = "/home/nidhal/ai4all/netem-lab"
os.environ["NETEM_LAB_INNER"] = "1"

_s = importlib.util.spec_from_file_location("nl", HERE + "/netem-lab.py")
nl = importlib.util.module_from_spec(_s)
_s.loader.exec_module(nl)

_d = importlib.util.spec_from_file_location("dv", HERE + "/divergence.py")
dv = importlib.util.module_from_spec(_d)
_d.loader.exec_module(dv)

PAIR = ("runpod-tx", "lossy-quic")
SIZES = {"8kb": 8 * 1024, "513kb": 513 * 1024}
TRIALS = 3


def measure(profile, nbytes):
    kw = {k: v for k, v in nl.PROFILES[profile].items() if k != "note"}
    with nl.ShapedLink(**kw) as link:
        srv = subprocess.Popen(
            ["nsenter", "-t", str(link.pid), "-n", sys.executable, "-c",
             f"import sys;sys.path.insert(0,{HERE!r});"
             f"import importlib.util,os;os.environ['NETEM_LAB_INNER']='1';"
             f"s=importlib.util.spec_from_file_location('d',{HERE + '/divergence.py'!r});"
             f"d=importlib.util.module_from_spec(s);s.loader.exec_module(d);"
             f"import threading;d._serve(threading.Event(),threading.Event())"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.2)
        ping = link.ping(count=15)["rtt_avg"]
        val = dv.bulk_round_trip(nbytes, reps=5)
        srv.terminate()
        srv.wait(timeout=5)
    return ping, val


def main():
    print(f"{'size':6s} {'trial':6s} {'runpod-tx(80ms,0%)':>20s} "
          f"{'lossy-quic(60ms,3%)':>21s}  flip?")
    verdict = {}
    for label, nbytes in SIZES.items():
        flips = 0
        for t in range(1, TRIALS + 1):
            p_far, v_far = measure("runpod-tx", nbytes)
            p_near, v_near = measure("lossy-quic", nbytes)
            # lossy-quic has the LOWER ping, so the scheduler ranks it first.
            # A flip = the lower-ping peer is actually slower.
            flip = v_near > v_far
            flips += flip
            print(f"{label:6s} {t:<6d} {v_far:>19.3f}s {v_near:>20.3f}s  "
                  f"{'FLIP' if flip else 'ok'}   "
                  f"(ping {p_far:.1f} vs {p_near:.1f})")
        verdict[label] = flips
    print()
    for label, f in verdict.items():
        print(f"{label}: lower-ping peer was slower in {f}/{TRIALS} trials")
    print()
    if verdict.get("513kb", 0) == TRIALS and verdict.get("8kb", 0) == 0:
        print("RESULT: reordering is payload-size dependent and repeatable.")
    elif verdict.get("513kb", 0) >= 2:
        print("RESULT: reordering repeats at 513KB but 8KB is not clean; "
              "report with the caveat.")
    else:
        print("RESULT: NOT reproducible -- do not report as a finding.")


if __name__ == "__main__":
    main()
