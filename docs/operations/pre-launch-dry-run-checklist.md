# Pre-launch dry-run checklist

This is the rehearsal gate before launch day. It assumes the status page
hosting account may still be unprovisioned; in that case, run every step
locally and leave only DNS / TLS / provider-account boxes unchecked.

Pair this checklist with [`launch-day-playbook.md`](launch-day-playbook.md),
[`status-page.md`](status-page.md), and
[`incident-response.md`](incident-response.md). Use
[`first-customer-runbook.md`](first-customer-runbook.md) as the
customer-specific plan. If a future checkout omits it, use
[`concierge-onboarding.md`](concierge-onboarding.md) as the fallback.

---

## 0. Dry-run record

Fill this in before starting. Paste the completed block into the launch
thread when the rehearsal ends.

| Field | Value |
|---|---|
| Dry-run date | `<YYYY-MM-DD>` |
| Operator | `<name>` |
| Gateway target | `<gateway-url>` |
| Status-page target | `https://status.2sum.ai` |
| Customer / trial slug | `<slug or n/a>` |
| Trace DB path | `<path>` |
| Result | `pass / pass-with-notes / fail` |

---

## 1. Preconditions

- [ ] The working tree is clean or every unrelated edit is accounted for.
- [ ] The owner has picked the launch window and rollback window.
- [ ] `docs/operations/status-page-config.yaml` has been reviewed.
- [ ] `docs/operations/support-channels.md` has a named primary support owner.
- [ ] `docs/operations/incident-response.md` severity table is accepted.
- [ ] `docs/operations/sla-template.md` is either accepted or explicitly
      marked "not offered for this launch."
- [ ] Provider keys exist for every upstream the gateway will route to.
- [ ] A capped synthetic gateway key exists, or the owner has approved
      deferring synthetic POST probes until hosting is provisioned.
- [ ] The customer-specific plan is
      [`first-customer-runbook.md`](first-customer-runbook.md), or the
      fallback is [`concierge-onboarding.md`](concierge-onboarding.md).

Stop here if any required owner approval is missing. A dry run that
discovers missing decisions is useful; pretending they are decisions is not.

---

## 2. Local artifact sanity

Run these from the repo root.

```bash
git status --short
python3 - <<'PY'
from pathlib import Path
for path in [
    "docs/operations/launch-day-playbook.md",
    "docs/operations/pre-launch-dry-run-checklist.md",
    "docs/operations/support-channels.md",
    "docs/operations/status-page-config.yaml",
]:
    p = Path(path)
    print(f"{path}: {'ok' if p.exists() else 'missing'}")
PY
```

- [ ] All four operational artifacts exist.
- [ ] `git status --short` contains no unexpected non-doc edits.
- [ ] No secret value appears in `status-page-config.yaml`.
- [ ] The status-page config still uses `${SYNTHETIC_GATEWAY_KEY}` and
      `${KUMA_PUSH_URL}` placeholders in the committed version.

---

## 3. Docs render and link pass

Preferred check:

```bash
uv run --with mkdocs-material mkdocs build --strict --site-dir /tmp/metis-docs-site
```

Fallback if the MkDocs dependency is not available:

```bash
python3 - <<'PY'
from pathlib import Path
import re
missing = []
for path in Path("docs").rglob("*.md"):
    text = path.read_text()
    for target in re.findall(r"\[[^\]]+\]\(([^)#][^)]+)\)", text):
        if "://" in target or target.startswith("mailto:"):
            continue
        clean = target.split("#", 1)[0]
        if not clean:
            continue
        resolved = (path.parent / clean).resolve()
        if not resolved.exists():
            missing.append((str(path), clean))
if missing:
    for src, target in missing:
        print(f"{src}: missing {target}")
    raise SystemExit(1)
print("relative markdown links ok")
PY
```

- [ ] MkDocs strict build passes, or the fallback relative-link pass is clean.
- [ ] New operations pages are present in `mkdocs.yml` under Operations.
- [ ] Any warning is either fixed or written into the dry-run record.

---

## 4. Gateway smoke

If running the local trial path:

```bash
infra/gateway/scripts/quickstart.sh
source .metis-trial/state.env
curl -fsS "$METIS_TRIAL_GATEWAY_URL/healthz"
uv run metis trial \
    --gateway-url "$METIS_TRIAL_GATEWAY_URL" \
    --gateway-key "$METIS_TRIAL_GATEWAY_KEY"
```

If running against a deployed gateway:

```bash
curl -fsS "$GATEWAY_URL/healthz"
curl -fsS "$GATEWAY_URL/v1/messages" \
    -H "x-api-key: $SYNTHETIC_GATEWAY_KEY" \
    -H "anthropic-version: 2023-06-01" \
    -H "content-type: application/json" \
    -d '{"model":"anthropic:claude-haiku-4-5","max_tokens":1,"messages":[{"role":"user","content":"ping"}]}'
```

- [ ] `/healthz` returns 200 from the expected network vantage point.
- [ ] One Anthropic-shape synthetic POST returns a message response.
- [ ] If OpenAI-shape traffic is in scope, one `/v1/chat/completions`
      synthetic POST returns a choices response.
- [ ] The trace DB receives `route.decided`, `llm.call_completed`, and
      `turn.completed` events.
- [ ] `gateway_key_id`, `user_id`, and `team_id` stamp as expected for
      the synthetic key.

---

## 5. Analytics and concierge smoke

Snapshot the DB if the gateway runs in Kubernetes, then run:

```bash
uv run metis trial-status /workspace \
    --db-path .metis-trial/snapshot/metis.db

uv run metis customer-report \
    --workspace /workspace \
    --customer-label "Dry Run" \
    --customer-tier trial \
    --db-path .metis-trial/snapshot/metis.db \
    --out /tmp/metis-dry-run-report.html
```

- [ ] `metis trial-status` reports non-zero calls after synthetic traffic.
- [ ] `readiness_band` is plausible for the amount of traffic.
- [ ] `metis customer-report` writes an offline HTML file.
- [ ] The report contains no secret token, prompt body, or customer name
      that should be redacted.

---

## 6. Status page dry run

Use [`status-page-config.yaml`](status-page-config.yaml) as the source of
truth.

- [ ] Owner has confirmed Tier A or Tier B for launch day.
- [ ] If Tier B, `helm template` renders the status-page resources:

```bash
helm template test ./infra/gateway/helm/ \
    --set provider.anthropicApiKey=sk-test \
    --set statusPage.enabled=true \
    --set statusPage.ingress.enabled=true \
    --set statusPage.ingress.host=status.2sum.ai \
    >/tmp/metis-status-page-render.yaml
```

- [ ] If Tier A, provider account owner is named.
- [ ] Monitor rows have been pasted into the provider, or a ticket links
      to the exact YAML artifact for later paste.
- [ ] Incident templates have been pasted, or saved as a pinned operator
      message if the provider lacks template support.
- [ ] The status-page "what to redact" rules are pinned in the incident
      channel.
- [ ] DNS and TLS are either complete or explicitly owner-blocked.

---

## 7. Support channel dry run

Use [`support-channels.md`](support-channels.md) as the channel contract.

- [ ] Buyer-facing support email or alias exists.
- [ ] Private operator channel exists.
- [ ] Incident channel exists.
- [ ] Status-page admin owner can log in.
- [ ] On-call / primary support owner can receive a page or urgent SMS.
- [ ] Escalation path from support owner to incident commander is tested.
- [ ] A fake SEV2 customer report is triaged into the incident channel.
- [ ] A fake non-incident question is kept out of the status page.

Suggested fake report:

```text
Customer says Anthropic-shape requests are timing out, but OpenAI-shape
requests still succeed. Please classify severity, post the first internal
update, and decide whether the public status page changes.
```

Expected outcome: SEV2 if confirmed, `partial-outage`, 30-minute update
cadence, affected component `Gateway (Anthropic shape)`.

---

## 8. Incident drill

Time-box this to 30 minutes. Do not debug a real system; rehearse the
motions.

- [ ] Start a timer.
- [ ] Operator acknowledges within the SEV target from
      [`incident-response.md`](incident-response.md#severity-levels).
- [ ] Incident commander posts the first internal update.
- [ ] Status-page operator drafts the public initial update.
- [ ] Gateway operator names the first mitigation: rollback, failover,
      restart, or maintenance mode.
- [ ] Support owner drafts the buyer-facing response.
- [ ] Scribe records timestamps in UTC.
- [ ] Drill ends with one action item, owner, due date, and tracking link.

---

## 9. Go / no-go decision

Use this exact shape in the launch thread.

```text
Pre-launch dry run: PASS / PASS WITH NOTES / FAIL
Date:
Operator:
Gateway:
Status page:
Support owner:
Blocking items:
Non-blocking notes:
Decision:
```

Go requires:

- [ ] Gateway smoke passed.
- [ ] Status-page path selected, even if hosting is owner-blocked.
- [ ] Support channels selected and reachable.
- [ ] Incident drill completed.
- [ ] No secret material was written to repo docs.
- [ ] Owner accepted every remaining manual step.

If any go item fails, launch moves. That is the point of the rehearsal.
