# Case study template

> Template to be filled in by the first GA customer. The goal is honest
> framing with specific numbers — not marketing puff. Anything that
> can't be replicated by another buyer reading our trial recipe doesn't
> belong here.

Pairs with [`docs/customer-trial-recipe.md`](../customer-trial-recipe.md) —
the methodology section below points buyers at the same recipe so they
can reproduce.

---

## How to fill this in

- **Numbers come from the trace DB**, not memory. Quote the exact query
  used (see `customer-trial-recipe.md §4` and `§5`).
- **State the baseline.** "Saves 42%" against opus-pinned is different
  from sonnet-pinned or against your existing routed setup. Always say
  which.
- **State the time window.** "Saves 42% over 7 days" beats "saves 42%."
- **State what didn't work too.** A case study that's only good news
  isn't credible. Workload shapes where Metis didn't move the needle
  belong in §6.
- **Get sign-off.** The customer reads and approves every number before
  publication.

Remove this section before publishing.

---

# {{Customer name}} — {{one-line outcome}}

> {{Two-sentence summary. Specific. State the workload, the time
> window, and the headline number. Example: "{{Customer}} routed
> {{N}} dev sessions per week through the Metis gateway for {{D}}
> weeks. Cost-per-quality dropped from $X (pinned-baseline) to $Y
> (slot 4 routing) on their {{workload class}} workload."}}

**Customer:** {{Company name, if public; or "a {{N}}-dev {{vertical}}
shop" if anonymous.}}
**Deployment:** {{Docker compose / kind / production k8s / SaaS}}
**Trial window:** {{YYYY-MM-DD}} to {{YYYY-MM-DD}}
**Headline:** {{One sentence; the cost-per-quality column.}}

---

## 1. What they were running before

{{One paragraph. What stack? What providers? What was the per-month
spend, roughly? What was the per-dev attribution story (or lack of)?}}

Example shape:
- N developers across M teams.
- Mixed Claude Code + Cursor + scripted SDK clients.
- $X/month Anthropic, $Y/month OpenAI.
- Cost attribution per provider key only; no per-dev or per-project
  visibility.

---

## 2. What they wanted

{{Bullet list. Two or three items max. Honest about which were "must
have" vs "nice to have."}}

Example:
- Per-team cost attribution without re-provisioning provider keys (must).
- Routing that picks the cheaper model on simple turns and the bigger
  model on complex ones, without each dev having to think about it
  (must).
- Audit log for SOC2 (nice to have; their SOC2 effort independent of
  this trial).

---

## 3. What they did

{{Pointer to the trial recipe section they followed. Three to five
bullets. Include any deviations from the recipe and why.}}

Example:
- Followed [`customer-trial-recipe.md`](../customer-trial-recipe.md)
  Path B with 8 prompts chosen from their highest-frequency dev
  scenarios.
- Wrote a 3-question pass/fail rubric per prompt.
- Ran each prompt through `--model haiku`, `--model sonnet`, and slot-4
  routing.
- Two-week window: week 1 pinned-baseline as control, week 2 Metis.

---

## 4. What the numbers say

{{The cost-per-quality table. Reproduced from the same query shapes in
the trial recipe.}}

| Strategy | Quality sum (N prompts) | Real spend | $ / quality unit |
|---|---:|---:|---:|
| {{Baseline strategy, e.g. sonnet pinned}} | {{Q_B}} | {{$X_B}} | **{{$X_B/Q_B}}** |
| {{Pinned-cheaper, e.g. haiku pinned}} | {{Q_H}} | {{$X_H}} | **{{$X_H/Q_H}}** |
| Slot 4 routing | {{Q_M}} | {{$X_M}} | **{{$X_M/Q_M}}** |

{{One sentence on what this means. Example: "Slot 4 produced
quality {{Q_M}} for spend {{$X_M}}, landing {{NN%}} below the pinned-
sonnet cost while staying within {{epsilon}} of pinned-sonnet
quality."}}

Per-team cost rollup over the trial window:

| Team | Spend | Calls | Cost / call |
|---|---:|---:|---:|
| {{team_a}} | {{$...}} | {{...}} | {{$...}} |
| {{team_b}} | {{$...}} | {{...}} | {{$...}} |
| ... | ... | ... | ... |

Source query: `/analytics/by_team?window={{N}}d`. See
[`docs/specs/analytics-api.md §4.9`](../specs/analytics-api.md).

---

## 5. What surprised them

{{One or two specifics. The "we didn't expect X" line is what makes a
case study sound real. If nothing surprised them, leave this section
out rather than fake it.}}

Example:
- "Slot 4 picked sonnet on {{N}} of {{M}} {{workload class}} turns and
  haiku everywhere else. We'd expected a more uniform distribution."
- "Per-team rollup showed {{team X}} was 3× the spend of {{team Y}};
  digging in, it was driven by {{specific cause}} — not what we'd
  guessed."

---

## 6. Where it didn't help

{{Critical for credibility. Real case studies aren't all-upside. If
every workload class saw savings, write that — but if any didn't, write
that too.}}

Example shapes (delete the ones that don't apply):
- Single-model workloads: every turn needed sonnet anyway. Slot 4 fired
  zero times; routing wedge was 0%. Caching layer saved {{NN%}} on
  long sessions.
- Very short sessions: cache writes didn't pay off; net effect on
  short-CLI traffic was {{NN%}}.
- No rubric on {{workload class}}: slot 4 couldn't learn from outcomes;
  pattern store accumulated cost + latency but not success. They left
  the routing wedge off for these and used Metis only for per-team
  attribution.

---

## 7. Where they went from here

{{One paragraph. What's their plan post-trial? Did they roll out to
more teams? Did they integrate with their SIEM? Did they cancel the
trial and stay on pinned-baseline?}}

---

## 8. Numbers that should reproduce

{{The reproduction recipe. The whole point of an honest case study is
the next buyer can follow the same recipe and get the same shape of
result on their workload. If your numbers don't reproduce, the case
study doesn't help us — list which steps they took.}}

```bash
# Workload definition (paste shape if shareable; describe if not)
cat benchmarks/workloads/{{their_workload}}/workload.yaml

# The two queries that produced the cost-per-quality column
curl 'http://gateway:8421/analytics/cost?window={{N}}d' | jq
curl 'http://gateway:8421/analytics/quality?group_by=model&window={{N}}d' | jq

# The benchmark harness invocation, if Path B
uv run python scripts/benchmark.py \
    --workload {{their_workload}} \
    --judge hybrid --judge-escalation-threshold 0.7 \
    --output {{trial_dir}}/{{their_workload}}.json
```

---

## 9. Customer quote

> "{{One or two sentences from the buyer. Specific. 'It works' is not
> useful; 'we found per-team cost attribution exposed an outlier we
> didn't know about' is.}}"
>
> — {{Name, title, customer}}

---

## 10. Caveats

- {{Time window}}. Results from a longer trial may differ.
- {{Workload shape}}. Their workload looked like {{X}}; a workload that
  looks like {{Y}} may behave differently.
- Model availability and pricing as of {{date}}; re-priced numbers in
  the trace DB use the same `PriceTable` for apples-to-apples
  comparison.

---

## How to publish this

1. Customer reads the draft and approves every number.
2. PR adds the case study at `docs/sales/case-study-{{customer-slug}}.md`.
3. Update [`README.md`](../../README.md) "Operations" section to link
   the case study (after first GA customer).
4. Add a one-line entry to `docs/sales/case-studies.md` (a future
   index file — create it when the second case study lands).

If the customer wants to stay anonymous, replace the customer name
field with a shape description ("a 40-dev fintech in EMEA") and keep
the numbers.
