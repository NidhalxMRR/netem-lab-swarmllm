# netem test plan — SwarmLLM peer routing under shaped links

**Claimed on issue #21 (2026-09-07).** enapt asked contributors to announce a
work item before starting so we don't collide; he is in the scheduler and
process pool this week. This plan deliberately touches neither: it observes
routing behaviour through shaped links and reports, it does not patch.

## The claim under test

`src/inference/scheduler/mod.rs`, doc comment on `DELEGATE_MAX_LATENCY_MS`
(raised 200 -> 1000 ms on 2026-08-31):

> **Raising the ceiling cannot make a near peer lose to a far one.**
> `candidates` arrives sorted pool-first, then reachability, then latency, so
> the first survivor is still the nearest qualifying peer; a wider bound only
> adds fallbacks where there were none.

And the author's own caveat, two paragraphs later:

> **This is still a threshold on a proxy.** The honest version compares
> predicted delegated time against local processor time and needs no constant
> at all.

The invariant is falsifiable. The caveat says where to aim: a proxy validated
on quiet links has only been half tested.

## Why a proxy can fail without the sort being wrong

The selection loop rejects on `c.latency_ms > DELEGATE_MAX_LATENCY_MS`, and
`latency_ms` is an application-level health-check reading (scheduler/mod.rs
~line 212 calls this out explicitly: it is `peer_registry.latency_ms`, not a
transport RTT). The sort is therefore correct *by construction* over whatever
those readings say.

That is exactly the gap worth probing. A health check is a small, infrequent
message. A delegated forward is a large, sustained transfer. The two do not
degrade the same way:

| link property        | effect on a small health ping | effect on a bulk forward |
|----------------------|-------------------------------|--------------------------|
| steady delay         | proportional                  | proportional             |
| jitter               | averages out over samples     | tail latency dominates   |
| loss                 | often invisible (retry)       | retransmit + congestion collapse |
| thin uplink (rate)   | ~invisible (few bytes)        | dominant (serialisation) |

So a peer can report a *good* `latency_ms` while being a *bad* place to send
work — and the invariant would still hold, because the sort faithfully ranks a
number that no longer predicts anything. **The near peer never loses the sort;
it loses the race.**

That is the finding worth having, and it is a measurement, not an opinion.

## Method

Harness: `netem-lab.py` (this repo). Rootless — veth pair across two
unprivileged network namespaces, so nothing touches host networking. It
self-tests before reporting: six checks including a mutation check (shaped must
differ from unshaped by ~10x) and a leak check (no qdisc left on the host).
Three harness bugs were caught by that self-test before any number was
believed; see the commit message for what they were.

Profiles (measured vs set, from the current sweep):

| profile      | set RTT | measured | jitter | loss | rate   |
|--------------|---------|----------|--------|------|--------|
| lan          | 1 ms    | 1.20     | 0.45   | 0%   | -      |
| metro        | 20 ms   | 20.34    | 1.54   | 0%   | -      |
| runpod-tx    | 80 ms   | 79.64    | 3.55   | 0%   | -      |
| wan-typical  | 100 ms  | 101.35   | 6.71   | 0.1% | -      |
| vast-proxy   | 300 ms  | 292.20   | 26.10  | 0.5% | -      |
| wan-bad      | 800 ms  | 800.57   | 70.26  | 2%   | -      |
| thin-uplink  | 100 ms  | 98.34    | 8.04   | 0.1% | 10mbit |
| lossy-quic   | 60 ms   | 60.62    | 7.19   | 3%   | -      |

The `runpod-tx` and `vast-proxy` figures are not arbitrary: arXiv 2602.16760
measured the same GPU model in the same city at 80-100 ms (RunPod) and
200-400 ms (VAST.ai) and saw **11-17 tok/s vs 1.2 tok/s** — a 10x throughput
swing from provider network architecture alone, invisible to any cost model
reasoning about geography or hardware. That is the case this plan is built to
detect.

## Measurements

For each profile:

1. **`latency_ms` as the health check reads it** — what the scheduler will rank on.
2. **Effective bulk throughput** on the same link, at the transfer size a real
   delegated forward moves.
3. **Divergence** = (2) ranked against (1). The invariant is about ordering, so
   the test is whether the ordering produced by (1) matches the ordering
   produced by (2).

A pass is: same order. A finding is: any profile where a peer ranked better by
`latency_ms` transfers worse — with the size of the gap.

## Oracle, and how this test can fail

The oracle lives outside the code under test: measured wall-clock transfer
time, not the scheduler's own estimate. If the harness reported the same number
for shaped and unshaped links the mutation check would fail and the run is
void — that check exists because the first version of this harness did exactly
that (both veth ends in one namespace, kernel short-circuits delivery, a 100 ms
profile measured 0.169 ms).

## Scope

- **No patches to `src/inference/scheduler/` or `src/inference/process_pool.rs`**
  while enapt is working there. Findings get a repro, not a PR.
- Read-only against the SwarmLLM tree; all shaping happens in a private netns.

## Status

- [x] Harness built and self-proving
- [x] Profiles calibrated against measured RTT
- [x] v0.3.161-alpha built on a 16 GB zero-swap box
- [ ] Bulk-vs-ping divergence measured per profile
- [ ] Reported to #21
