# Raw notes — payment retry meeting (2026-04-22)

attendees: jordan (eng), priya (pm), sam (oncall)

## what happened

- friday 4/19, ~14:30 UTC. checkout failures spiked from baseline ~0.4% to 12% in 8 min
- root cause: stripe rate-limited us when our retry loop fired with no jitter
  during a transient network blip — every node retried at exactly the same
  intervals, multiplying load on stripe by ~6x
- recovered in 22 min after we deployed a hotfix that capped concurrent
  retries at 50 (was unbounded). full restore took ~38 min including stripe's
  cooldown window

## root cause notes

- `payments/retry.py::with_retry` uses `tenacity.retry(stop=stop_after_attempt(5))`
  with no `wait_exponential`
- so all retries land at t+0, t+1, t+2, t+4, t+8 from the *initial* failure,
  identical across pods. the fleet retried in lockstep
- stripe's per-merchant ceiling is ~200 req/s. 4500 pods x 5 attempts in 8s =
  ~2800 req/s sustained for the burst window

## what we want for the writeup

- audience: payments team + on-call rotation, not exec
- needs to include: timeline, root cause, fix shipped, what we're doing about
  the underlying class of bug
- *don't* include: customer impact $$ (legal hasn't cleared the number),
  who-said-what blame (priya is firm on this)
- format: standard postmortem doc — summary, timeline, root cause, action
  items
- length: 1 page-ish. if it's longer than a page no one reads it

## action items priya logged

1. land jitter in `with_retry` (sam owns, by 4/29)
2. add a slo dashboard tile for "p95 retry concurrency" (jordan owns, by 5/6)
3. write a runbook entry for "stripe 429 burst" (sam owns, by 5/6)
4. write this postmortem doc (jordan owns — that's this task)
