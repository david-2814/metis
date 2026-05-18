# Contributing to Metis

Thanks for considering a contribution. This document covers the working norms, the license posture, and the practical steps for landing changes.

## License

This repo is licensed under [Apache-2.0](LICENSE). By submitting a pull request, you agree that your contribution is made under the same Apache-2.0 terms — Section 5 of the license formalizes this:

> Unless You explicitly state otherwise, any Contribution intentionally submitted for inclusion in the Work by You to the Licensor shall be under the terms and conditions of this License, without any additional terms or conditions.

We do **not** require a separate Contributor License Agreement (CLA). The Apache-2.0 inbound=outbound rule is sufficient for v1; if a future enterprise buyer requires a stricter contribution chain, we'll revisit.

## What's in this repo vs. the paid overlay

The `metis` repo (this one) is the OSS substrate: gateway, canonical IR, adapters, routing, pattern store, memory, tools, skills, heuristic evaluator, per-key analytics, the CLI/TUI/serve agent surfaces, and all specs / operations / sales docs. It's standalone-usable — clone, `uv sync`, `metis trial`, see real savings.

A separate private repo (`metis-pro`) holds the paid-tier overlay: billing (Stripe), signup, accounts, hosted dashboard UI, curated LLM-judge rubric library, and enterprise SAML/OIDC/SCIM glue. It plugs into this repo via extension Protocols defined in [`packages/metis-core/src/metis_core/extensions.py`](packages/metis-core/src/metis_core/extensions.py) and [`apps/gateway/src/metis_gateway/extensions.py`](apps/gateway/src/metis_gateway/extensions.py).

The architectural boundary is documented in [docs/operations/repo-split-plan.md](docs/operations/repo-split-plan.md). Contributions to *this* repo touch only the OSS substrate.

## Before you start

Please read in this order:

1. [`AGENTS.md`](AGENTS.md) — current implementation state, conventions, gotchas. Same file is loaded by AI agents (Claude Code, Cursor, etc.); humans benefit from reading it too.
2. [`docs/STRATEGY.md`](docs/STRATEGY.md) — the *why*: cost-optimization thesis, buyer ≠ user framing, open strategic questions.
3. [`docs/specs/`](docs/specs/) — the relevant component spec for whatever you're touching.

The project is **specs-first**: if your change touches a contract (event types, public API, wire format, Protocol), draft the spec change first or flag the spec impact in your PR.

## Working norms

Inherited from [AGENTS.md](AGENTS.md) "Working norms" — the highlights:

- **Solo, part-time owner.** Scope decisions favor what one person can land and maintain. Bundling unrelated changes makes review harder; please open separate PRs for separate concerns.
- **Cross-spec discipline.** Spec changes that touch a contract need an entry in [`docs/specs/CHANGES.md`](docs/specs/CHANGES.md) — date, change, type, references to verify, status.
- **Bounded memory is a feature.** `MEMORY.md` / `USER.md` caps are intentional. PRs that uncap them get pushed back.
- **OSS standalone-usable invariant.** Anything that breaks the "self-hoster runs `metis trial` and sees savings" path on the OSS-only deployment is a blocker. The Pro overlay is an *addition*, not a precondition.

## How to land a change

```bash
git clone https://github.com/2sumAI/metis.git
cd metis
uv sync                                       # resolves the workspace
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env    # for live-API smoke tests

uv run pytest                                 # full test suite (~40s)
uv run ruff check packages apps scripts
uv run ruff format packages apps scripts
```

Before opening a PR:

- All tests pass (`uv run pytest`).
- Ruff is clean on both `check` and `format`.
- Mypy clean if you touched typed surfaces (`uv run mypy packages apps`).
- New behavior has tests; new HTTP endpoints / event types have spec entries.
- PR description names the spec(s) you read and any cross-spec implications.

For migration-style work (large refactors, repo-shape changes), discuss the approach in an issue first. The owner is part-time; surprise large diffs are hard to review.

## Reporting bugs

Issues with a clear repro recipe + the expected vs actual behavior land best. Tests reproducing the bug (even failing) are the gold standard.

## Reporting security issues

Please **don't** file security-sensitive issues publicly. Email the owner directly (the email on `git log -1 --format='%ae'`). Issues affecting `metis-pro` operational surfaces (auth, billing, signup, signature verification) are higher priority than OSS-only issues.

## Code of Conduct

We follow the [Contributor Covenant 2.1](https://www.contributor-covenant.org/version/2/1/code_of_conduct/). Be kind, be specific, assume good intent.
