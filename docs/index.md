# Metis documentation

Metis is a local-first AI dev agent — provider-agnostic, self-improving,
and cost-aware. Canonical message format, layered routing, bounded
portable memory, transparent cost attribution per turn / user / team.

This site is built from the [`docs/`](https://github.com/david-2814/metis/tree/main/docs)
tree in the repo. The four sections in the top nav line up with how the
project itself is organized.

## Where to start

- **New to Metis?** Start with the [Project overview](project-overview.md)
  for the vision, principles, and architecture, then walk
  [First savings number](operations/quickstart.md) for the
  &lt; 1-hour buyer-trial path (kind + helm + `metis trial` end-to-end).
- **Pointing a client at it?** The
  [Gateway client quickstart](gateway-client-quickstart.md) walks Claude
  Code / Cursor / raw SDK clients through flipping
  `ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` at a running Metis gateway.
- **Want the savings number?** [Savings demo](savings-demo.md) is the
  cost-vs-quality story end-to-end with the actual benchmark numbers.
- **Deploying to production?** [Gateway deployment](gateway-deployment.md)
  is the operator reference (env vars, volumes, key rotation, TLS,
  cost attribution), and the [Operations](operations/quickstart.md)
  section has incident response, SLAs, status pages, and SOC2 readiness.

## The four nav sections

### Getting Started

Onboarding and first-run paths. Aimed at someone who has cloned the repo
or installed the gateway and wants to see something work.

### Specs

The component contracts. The design is specified before code lands; if
you want to know *why* something behaves the way it does, this is where
to look. The
[canonical message format](specs/canonical-message-format.md) is the
load-bearing data contract — everything else depends on it. The
[event bus & trace catalog](specs/event-bus-and-trace-catalog.md) is the
observability spine. The [routing engine](specs/routing-engine.md)
covers model selection, rules, delegation, and the learned-pattern
slot. The full [change log](specs/CHANGES.md) tracks every cross-spec
edit with date, type, and verification status.

### Operations

The operational playbooks an SRE will read before signing. Quickstart,
deployment, upgrade, incident response, SLA template, status page
recipe, SOC2 readiness audit, trace-store performance reference.

### Strategy

The *why* and the competitive landscape. [STRATEGY.md](STRATEGY.md)
holds the cost-optimization thesis, buyer ≠ user framing, three cost
levers, and open strategic questions. [Known issues](KNOWN_ISSUES.md)
tracks spec/impl gaps from prior reviews — the watchlist of "looks fine
but is subtly wrong." [Market research](market-research/synthesis.md)
is the synthesis + four per-stream reports (coding agents, local-first
platforms, routing layers, skills & memory).

## Source links

Every page on this site has an **Edit this page** and **View source**
action in the top-right. Both point at GitHub —
[`david-2814/metis`](https://github.com/david-2814/metis), `main` branch — at
the exact file backing the page. Spec edits go through a PR; the change
log is in [specs/CHANGES.md](specs/CHANGES.md).

## Running the site locally

```bash
# One-shot preview with mkdocs-material installed on demand:
uv run --with mkdocs-material mkdocs serve

# Or via Docker (mirrors the gateway shape; serves on 127.0.0.1:8423):
docker compose --profile docs up docs
```

Both bind loopback-only by default. The site is pure-static once built
(`mkdocs build` writes to `site/`), so any static host works for
production deployment.
