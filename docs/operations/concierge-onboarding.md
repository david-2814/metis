# Concierge onboarding — first paid customer

> Trial → conversion path for the first paid Metis customer. Maps the
> 7-day flow the buyer experiences, names what they do, what we deliver,
> and what success looks like at each stage. Automated where it can be —
> scripted assists where it can't.

Pairs with [`quickstart.md`](quickstart.md) (the < 1-hour helm-install
recipe) and [`../customer-trial-recipe.md`](../customer-trial-recipe.md)
(the buyer-runs-their-own-workload recipe). This doc layers a
conversation cadence on top of those two — the trial flow is the same,
but the touch points are scripted so the conversion conversation lands
with evidence already in hand.

**Audience:** the human running point on the first paid customer
(initially: the project owner). Not the buyer; this is the internal
playbook.

## The 7-day shape

| Day | What the buyer does | What we provide | Success looks like |
|---|---|---|---|
| 0 (intake) | Books a call; agrees to a 7-day trial | Opening email + provisioned trial key tagged `customer_tier=trial` | Buyer has the key + the install link before day 1 |
| 1 (install) | Runs `infra/gateway/scripts/quickstart.sh` or points an existing tool at the gateway | Live install support window (~30 min) | Gateway healthcheck green; first `gw_…` call lands in trace DB |
| 2 (catch-up) | Routes real traffic through the gateway | (none — they drive) | First non-zero `metis trial-status` readout |
| 3 (first signal) | We send the day-3 check-in | First savings number quoted from `metis trial-status` | `readiness_band ≥ warm`; ≥ 20 calls in window |
| 4-6 (incubate) | They keep using it; we stay quiet | (none, by design) | Per-user / per-team rollup populated; cost-per-quality stabilizes |
| 7 (close) | We send the day-7 close + report | `metis customer-report` HTML attached; suggested conversion contract | Buyer commits to paid (`customer_tier=paid`) or names a specific blocker |

Each stage below has a "what to send" section. Copy the templates,
substitute the named placeholders, send.

---

## Day 0 — intake

**What the buyer does.** They've already booked a 30-minute call.
Before the call, they've named (a) the model(s) they currently use,
(b) rough monthly LLM spend, and (c) one workflow they want to evaluate
against. The call confirms (a)-(c) and sets the trial start time.

**What we do.** Issue the trial key tagged `customer_tier=trial` and
send the opening email.

```bash
metis gateway issue-key \
    --name "{{customer_slug}}-trial" \
    --workspace /workspace \
    --customer-tier trial \
    --daily-cap-usd 50 \
    --user "{{primary_user_slug}}" \
    --team "{{customer_slug}}"
```

The `--daily-cap-usd 50` ceiling is a guardrail, not a billing signal —
it prevents a runaway-loop bug on the buyer's side from generating a
six-figure surprise during a free trial. Adjust per agreement.

**What to send (opening email):**

```
Subject: Metis trial — your gateway key + day-1 install link

Hi {{first_name}},

Per our call, here's the 7-day setup. Your trial key:

    gateway URL : {{gateway_url}}
    bearer token: {{gw_token}}   (only shown once — save it now)
    daily cap   : $50/day (you can ask us to lift it)
    trial ends  : {{trial_end_iso}}

Day 1 install recipe (~30 min):
    {{repo_url}}/docs/operations/quickstart.md

I'll send a day-3 check-in with the first cost number. If anything
breaks on install, reply to this thread — I have a live window
{{day_1_window}} {{tz}} for that purpose.

Two things make the trial more useful:
  1. Pick one real workflow you care about. Run it through the
     gateway. We can attribute cost back to it.
  2. If you have a way to grade the output (a rubric, a test suite,
     thumbs-up/down), wire it. Otherwise the cost number is solid
     but the quality number stays empty.

- {{your_name}}
```

**Success looks like.** Buyer replies "got it" before day 1. If
they don't, send a one-line nudge on day 1 morning. If still silence
on day 2, the trial has stalled before it started — pick up the phone.

---

## Day 1 — install

**What the buyer does.** Runs the helm-quickstart against their own
cluster (or `docker compose run` for the docker path), points their
existing tool at the gateway, makes the first call.

**What we provide.** A 30-minute "install office hours" window in case
the helm chart, kind cluster, or `ANTHROPIC_BASE_URL` flip surprises
them. Most installs don't need this; book it anyway.

**What to verify (from our side).** After they've reported the key
works, snapshot the gateway DB once and run:

```bash
metis trial-status /workspace --db-path .metis-trial/snapshot/metis.db
```

You should see `llm calls: 1+` and a `readiness_band: warm`. If
`llm calls: 0`, the gateway is up but the buyer hasn't sent traffic
through it — confirm they flipped `ANTHROPIC_BASE_URL` (Claude Code)
or `OPENAI_BASE_URL` (Cursor) per
[`../gateway-client-quickstart.md`](../gateway-client-quickstart.md).

**Success looks like.** First non-zero `metis trial-status` readout
within 24h of intake. If day 2 morning shows `llm calls: 0`, send a
"are you blocked?" message — do NOT wait until day 3.

---

## Day 2 — quiet

**What the buyer does.** Routes traffic through the gateway. We
stay quiet. Watching the metrics is fine; emailing them isn't.

**What to verify.** Re-snapshot the trace DB and run
`metis trial-status` once. You're checking two things:

- `llm calls` is growing — confirms the gateway is the path of all
  traffic, not just smoke calls.
- `quality_count` is growing if they wired an evaluator. If 0 but
  `llm calls` is healthy, flag this internally — the day-7 close
  will need to lean on cost-only framing.

---

## Day 3 — first measurable savings

**What the buyer does.** They've been using the gateway for ~48h.
They have a real cost number; if they wired an evaluator, they have a
real quality number too.

**What we provide.** The day-3 check-in email — short, quotes one
number, asks one question.

Run this on the snapshot DB first so you're quoting real numbers:

```bash
metis trial-status /workspace \
    --db-path .metis-trial/snapshot/metis.db \
    --since "{{trial_start_iso}}"
```

**What to send (day-3 check-in):**

```
Subject: Metis trial — day 3 quick check

Hi {{first_name}},

Quick numbers from your trace DB through {{now_iso}}:

  spend so far : ${{spend}}
  savings vs {{baseline_short}} : {{savings_pct}}%
  llm calls    : {{llm_calls}}
  {{quality_line if available; else: "(no quality verdicts yet — let me
   know if you want help wiring an evaluator)"}}

If anything looks off, I'm happy to dig in. Otherwise: you've got
4 days left. Two ways people typically use the rest of the window:

  (a) keep the same workflow running; see if the savings shape holds.
  (b) try a second workflow — same key, different workload tag — so
      day 7 has two data points.

Either is fine. Let me know which (or neither — silence is fine too).

- {{your_name}}
```

**Success looks like.** Either a reply (any reply — confirms they're
engaged) or sustained traffic growth. A silent buyer with growing
traffic is fine. A silent buyer with flat-line traffic is a warning.

---

## Days 4-6 — incubate

**What the buyer does.** Whatever they want. The trial runs itself.

**What we provide.** Nothing, by design. Resist the urge to send
"how's it going" emails — the asymmetry of buyer time vs vendor time
makes mid-trial nudges feel pushy.

**What to verify (internally).** Mid-week, run:

```bash
metis customer-report \
    --workspace /workspace \
    --customer-label "{{customer_name}}" \
    --customer-tier trial \
    --since "{{trial_start_iso}}" \
    --db-path .metis-trial/snapshot/metis.db \
    --out /tmp/{{customer_slug}}-day5.html
```

Don't send this report. It's a dry run for day 7. You want to know:

- Does the by-user table show a 2-3× outlier? (Always does. Note who.)
- Does the by-team rollup populate? (If not, they're routing through
  one key — day 7 framing should emphasize cost-per-quality, not
  per-team attribution.)
- Does the daily-spend trend slope up, down, or flat? Trend matters
  for the day-7 commitment ask.

---

## Day 7 — conversion conversation

**What the buyer does.** Reads the day-7 close email + the attached
report. Either commits to paid or names a specific blocker.

**What we provide.** The day-7 close email + the
`metis customer-report` HTML attached.

Generate the report off the most recent snapshot:

```bash
metis customer-report \
    --workspace /workspace \
    --customer-label "{{customer_name}}" \
    --customer-tier trial \
    --since "{{trial_start_iso}}" \
    --until "{{trial_end_iso}}" \
    --db-path .metis-trial/snapshot/metis.db \
    --out /tmp/{{customer_slug}}-day7.html
```

**What to send (day-7 close):**

```
Subject: Metis trial — wrap-up + report

Hi {{first_name}},

Trial wraps today. Attached is the full report (HTML — open in any
browser, no JS, no fetches).

Headline:

  spend       : ${{spend}} across {{llm_calls}} calls
  savings     : {{savings_pct}}% vs {{baseline_short}}
  cost / quality : ${{cost_per_quality}} ({{quality_count}} verdicts)

The per-{{user|team}} rollup at the bottom is usually the more
interesting half — {{specific_observation_from_report}}.

Two ways to keep going:

  (a) Convert to paid at $X/seat/month for {{N}} seats. We re-issue
      your gateway key under customer_tier=paid; everything else
      stays put (same trace DB, same dashboard, same retention).
  (b) Extend the trial 7 more days. Useful if {{observation}}; not
      useful if {{the_thing_we_already_proved}}.

If you want to convert, reply "let's go" and I'll send the contract.
If you want to extend, reply "extend" and I'll lift the daily cap +
re-tag the key. If neither: name the blocker. The case study
template at docs/sales/case-study-template.md is what we'd fill in
together if you convert.

Either way — thanks for taking the trial.

- {{your_name}}
```

**Success looks like.** A reply within 48h that names one of the
three paths (convert / extend / blocker). If the buyer goes silent
past 72h, the trial converted to "not now" — log it as such in the
account notes and move on. Aggressive follow-up doesn't move
indifferent buyers.

---

## Conversion mechanics

When the buyer commits, the actual conversion is two commands:

```bash
# Re-tag the existing key as paid (preserves the gateway_key_id +
# all historical spend attribution).
metis gateway revoke-key {{old_key_id}} --db-path ~/.metis/metis.db

metis gateway issue-key \
    --name "{{customer_slug}}-paid" \
    --workspace /workspace \
    --customer-tier paid \
    --user "{{primary_user_slug}}" \
    --team "{{customer_slug}}"
```

Send the new bearer token over the same channel as the trial token
(once — the gateway prints it once). Update the daily cap per the
signed agreement.

The `customer_tier=paid` tag is the signal future runs of
`metis customer-report` pivot on: paid-tier reports drop the
"trial ending {{date}}" framing.

---

## Conversion artifacts inventory

What we hand the buyer over the 7 days:

1. **Day 0 opening email** — token + install link + 7-day shape.
2. **Day 3 check-in** — one cost number, one open-ended question.
3. **Day 5 internal dry run** of `metis customer-report` — never
   sent; informs the day-7 framing.
4. **Day 7 close email** — full HTML report attached, three paths.
5. **`metis customer-report` HTML** — generated from the trace DB,
   no external assets, browser-print-to-PDF for archival.
6. **Sales-collateral deep-link** — pointer to
   [`../sales/one-pager.md`](../sales/one-pager.md) (post-trial
   sanity check) and
   [`../sales/case-study-template.md`](../sales/case-study-template.md)
   (post-conversion case study draft).

---

## Day-by-day quick reference

```bash
# Day 0 — provision
metis gateway issue-key --name "{{slug}}-trial" --workspace /workspace \
    --customer-tier trial --daily-cap-usd 50 \
    --user "{{user}}" --team "{{slug}}"

# Day 1 — verify install (after the buyer reports the key works)
metis trial-status /workspace --db-path .metis-trial/snapshot/metis.db

# Day 3 — first measurable signal
metis trial-status /workspace \
    --db-path .metis-trial/snapshot/metis.db \
    --since "{{trial_start_iso}}"

# Day 5 — dry-run report (don't send)
metis customer-report --workspace /workspace --customer-tier trial \
    --since "{{trial_start_iso}}" \
    --db-path .metis-trial/snapshot/metis.db \
    --out /tmp/{{slug}}-day5.html

# Day 7 — close
metis customer-report --workspace /workspace --customer-tier trial \
    --customer-label "{{customer_name}}" \
    --since "{{trial_start_iso}}" --until "{{trial_end_iso}}" \
    --db-path .metis-trial/snapshot/metis.db \
    --out /tmp/{{slug}}-day7.html

# Conversion (after the buyer commits)
metis gateway revoke-key {{old_key_id}}
metis gateway issue-key --name "{{slug}}-paid" --workspace /workspace \
    --customer-tier paid --user "{{user}}" --team "{{slug}}"
```

---

## When this doesn't work

The 7-day flow assumes the buyer has (a) a real workflow to run through
the gateway and (b) enough traffic to make the numbers credible.
Some shapes won't fit:

- **Buyer has no workload to run.** Day 1 stalls at `llm calls: 0`.
  Pivot to the [`quickstart.md`](quickstart.md) pre-baked workload
  + `metis trial` — at least there's *a* number to discuss on day 3.
  Don't pretend this is the same as a real-workload trial.
- **Buyer routes only catch-up traffic.** They've already paid for
  the month elsewhere; they're testing on a low-volume backup
  workflow. The savings number will be real but small. Frame the
  trial as "operational evidence" (per-user / per-team rollup,
  audit log) rather than "savings evidence."
- **Buyer has no quality signal.** No rubric / test suite / feedback
  loop → quality_count stays at 0. The day-7 report can quote cost
  and savings_pct only. Don't fabricate a quality number; the report
  already prints "no quality verdicts in window" verbatim.

For each shape: rewrite the day-3 and day-7 emails to match what the
trial can actually show. Don't promise a number the trace DB can't
back up.

---

## What this doc isn't

Not a billing surface. Not a pricing recommendation. The
`customer_tier` field is a *support-context* tag, not an entitlement
flag — the gateway does not gate behavior on tier. Billing /
metering lives in Wave 15a-6 territory if it lands. Pricing lives
in [`../specs/pricing.md`](../specs/pricing.md) (awaiting owner
ratification).
