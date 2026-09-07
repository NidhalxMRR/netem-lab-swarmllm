# netem-lab

A rootless WAN emulator for testing latency-sensitive peer routing, built while
looking at [SwarmLLM](https://github.com/enapt/SwarmLLM)'s delegation scheduler.

It shapes delay, jitter, loss and rate across a veth pair spanning two
**unprivileged network namespaces** — no root, no sudo, and nothing that can
touch host networking.

## Why

A distributed inference scheduler has to decide where to send work. The cheap
signal is a health-check ping. The actual work is a sustained transfer. Those
two things do not degrade the same way, and a cost model validated on quiet
links has only been half tested.

This measures the gap.

## Result

Ping ordering and transfer ordering agree at 8 KB and disagree at 513 KB:

| profile | ping | 8 KB | 513 KB |
|---|---|---|---|
| lan | 1.2 ms | 0.003 s | 0.018 s |
| metro | 20.4 ms | 0.042 s | 0.240 s |
| **lossy-quic** (60 ms, 3% loss) | **60.3 ms** | 0.129 s | **2.738 s** |
| **runpod-tx** (80 ms, 0% loss) | **81.1 ms** | 0.166 s | **0.959 s** |
| wan-typical (100 ms, 0.1%) | 100.1 ms | 0.210 s | 1.193 s |
| thin-uplink (100 ms, 10 mbit) | 101.4 ms | 0.216 s | 1.587 s |
| vast-proxy (300 ms, 0.5%) | 295.9 ms | 0.630 s | 4.559 s |
| wan-bad (800 ms, 2%) | 822.5 ms | 1.722 s | 30.093 s |

`lossy-quic` has the **lower** ping and is **2.9x slower** on a 513 KB round
trip. Confirmed 3/3 trials at 513 KB, 0/3 at 8 KB — the reordering is
repeatable and payload-size dependent.

Loss is the reason. It is nearly invisible to a small retried health check and
expensive to a bulk transfer. A scheduler ranking on ping never mis-sorts; it
just sorts on a number that has stopped predicting anything.

## Trusting the numbers

`verify` runs six checks before any measurement is believed, including a
**mutation check** (a shaped link must differ from an unshaped one by ~10x) and
a **leak check** (no qdisc left on the host).

That self-test caught three bugs in this harness, each of which produced
confident and completely wrong numbers:

1. **Both veth ends in one namespace.** The kernel short-circuits delivery and
   the egress qdisc never runs — a 100 ms profile measured **0.169 ms**.
2. **ARP resolution crossing the shaped link**, inflating the first burst: an
   800 ms target read 997 ms cold, 800.4 ms warm.
3. **A fixed ping interval outrunning slow links.** Packets still in flight when
   the deadline expired were counted as lost — a 0%-loss 800 ms profile
   reported **34.8% loss**.

All three were mine, not netem's. A harness that cannot fail its own test is
not evidence.

## Use

```sh
python3 netem-lab.py verify     # six self-checks — run this first
python3 netem-lab.py sweep      # measured RTT/loss per profile
python3 divergence.py           # ping ordering vs transfer ordering
python3 confirm_flip.py         # repeat the reordering N times
```

Requires `iproute2` and unprivileged user namespaces
(`kernel.unprivileged_userns_clone=1`, the default on most distributions).

Everything re-execs itself into a private namespace; nothing persists after the
process exits.

## Files

- `netem-lab.py` — shaping harness, profiles, self-test
- `divergence.py` — ping vs bulk-transfer ordering across all profiles
- `confirm_flip.py` — repeatability check on the two profiles that reorder
- `probe_twons.py` — minimal proof that rootless two-namespace shaping works
- `TEST-PLAN.md` — what is being tested and why, written before the run
- `sweep.json`, `divergence.json` — raw measurements

## Licence

Apache-2.0.
