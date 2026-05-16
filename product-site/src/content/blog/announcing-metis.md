---
title: "Announcing Metis"
description: "An open-core LLM gateway and agent runtime for teams that need AI-agent spend to be visible, governable, and cheaper."
publishDate: "2026-05-16"
author: "2sum AI"
---

Metis is launching as an open-core LLM gateway and agent runtime for AI
development traffic. It sits in front of the tools teams already use -
Claude Code, Cursor, OpenAI-shaped SDKs, Anthropic-shaped SDKs - and
turns each request into a traceable, attributable, and optimizable event.

The first promise is deliberately practical: flip a base URL, keep your
provider keys, and see where the bill goes by user, team, key, inbound
shape, model, and task class. The deeper promise is that Metis learns
from outcomes. It can route against task-fingerprint history, evaluate
quality, and use planner-worker delegation when a bigger model should
plan while smaller workers do the parallel work.

## What ships today

The gateway accepts OpenAI and Anthropic provider shapes, supports sync
and SSE streaming, stamps every call with key/user/team identity, and
records the routing chain plus cost into a local SQLite trace store. The
same core powers the agent surfaces: `metis chat`, `metis tui`, and
`metis serve`.

The launch build also includes the buyer-facing operational pieces that
make a trial real: per-key and per-team analytics, Prometheus metrics,
SOC2/GDPR readiness artifacts, audit log export, 90-day trace retention,
redaction, GDPR export/forget, helm and Docker deployment recipes,
concierge reporting CLIs, and a status-page deployment recipe.

Wave 15 closed the two GA-readiness blockers found in the Wave 14 audit:
a single SSL hiccup no longer marks a whole provider unavailable, and
SDK-canonical bare model names are normalized before routing so cost
reporting no longer over-counts when prefixes are stripped.

The Phase 3 claim itself remains ready for owner review, not auto-promoted.
That distinction matters: the product is launch-ready for buyer trials,
while the formal phase label is still a recorded owner decision.

## The validated savings story

Metis has three cost levers: context engineering, skills, and routing.
They are not equally mature, and they should not be sold as one vague
"AI saves money" blob.

The most reproduced routing-surface lever is **delegation**. On the
`multi-step-with-delegation` benchmark, a sonnet planner with haiku
workers beat a sonnet-only no-delegation baseline by **8.3% to 26.1%
better cost-per-quality**, across three independent measurements with a
**19.9% midpoint** in the A3-rev7 completion run.

Prompt caching is the clearest context-engineering proof point. In Run
3, cache fired on **49 of 49 LLM calls** and the same-workload cost fell
**22.8%** versus the cold-cache comparison.

Model selection is more nuanced. A3-rev3 remains the canonical
end-to-end proof that the mechanism can work: slot 4 routed sonnet to
the one hard `regex-with-edge-cases` turn and haiku elsewhere, landing at
**$0.0477 per quality unit** between haiku-only **$0.0383** and
sonnet-only **$0.1176**. But A3-rev7 completion did not reproduce a
broader inversion: Pass C picked haiku on every routed turn across five
partial-credit workloads. The takeaway is honest and useful: the
mechanics are built, but generalized model-selection gains need task
domains where the haiku-vs-sonnet quality gap is larger than the
remaining variance.

## Pricing shape

The commercial model is now ratified at the shape level:

- **Metis Community:** open-core gateway and single-user/self-hosted
  agent surfaces at $0.
- **Metis Pro:** per active user per month, for the team features:
  identity, caps, per-user/team analytics, hosted operations, audit
  export, LLM-judge quality analytics, and the agent upgrade path.
- **Metis Enterprise:** Pro plus a capped percent-of-savings add-on for
  buyers who want outcome-linked contracting.

Metis does not resell provider tokens. Buyers bring their Anthropic,
OpenAI, or OpenRouter keys; the provider bill stays with the provider.
The Wave 15 billing module is Stripe-backed and opt-in, with Pro
subscriptions and Enterprise savings usage records implemented behind
the gateway billing surface.

## Why this is different

Most gateway products intercept HTTP and normalize it to an OpenAI-shaped
surface. Metis uses a canonical message format that preserves Anthropic
content blocks, thinking blocks, tool use, and cache controls as
first-class internal data. That makes provider swaps, replay,
re-pricing, and quality evaluation a core property instead of a
best-effort log parser.

The other difference is the loop. Metis records what happened, judges
whether it worked, and feeds that outcome back into routing and
delegation. The product thesis is not "one more proxy." It is a runtime
that gets cheaper as it learns which work actually needs which model.

## Try it

Start with the operations quickstart for the under-one-hour buyer trial,
then point existing tools at the gateway with the client quickstart in
the repo:

- `docs/operations/quickstart.md`
- `docs/gateway-client-quickstart.md`
- `docs/sales/one-pager.md`

The launch posture is simple: Community gets the gateway into your
traffic, Pro turns it into a team control plane, and Enterprise ties the
bill to verified savings when procurement is ready for that shape.
