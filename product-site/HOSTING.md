# Hosting runbook

This document walks through the one-time setup and ongoing deploy flow for
`2sum.ai`. The infrastructure lives in [`infra/`](./infra/) (AWS CDK in
TypeScript). The continuous-deploy workflow lives at
[`.github/workflows/deploy-product-site.yml`](../.github/workflows/deploy-product-site.yml).

## What you're getting

```
                 https://2sum.ai
                      │
            ┌─────────▼──────────┐
            │  Route 53 alias    │  (A + AAAA → CloudFront)
            └─────────┬──────────┘
                      │
            ┌─────────▼──────────┐
            │  CloudFront        │  TLS via ACM (us-east-1)
            │  + viewer fn       │  www → apex 301
            │                    │  directory-URL rewrites
            └─────────┬──────────┘
                      │  OAC (SigV4)
            ┌─────────▼──────────┐
            │  S3 (private)      │  versioned, SSE-S3
            │  metis-site-2sum-ai│
            └────────────────────┘

      GitHub Actions ──── OIDC ───► metis-site-deploy IAM role
                                     │
                       s3:PutObject  │  cloudfront:CreateInvalidation
```

Estimated steady-state cost at low traffic (< 1 GB egress / month): **\$0.50
– \$2 / month**. Route 53 hosted zone is \$0.50/mo flat; S3 storage of ~2 MB
is negligible; CloudFront has a 1 TB/month always-free tier.

## Prerequisites

You need, on your local machine:

- **AWS CLI v2** (`brew install awscli`).
- **Node.js 20+** (you already have it for the site build).
- **An IAM admin user or SSO profile** for the AWS account that owns
  `2sum.ai`. The first `cdk deploy` needs broad permissions; subsequent
  GitHub-driven deploys use the narrowly scoped `metis-site-deploy` role.

Sanity check before you begin:

```bash
aws sts get-caller-identity                      # confirms you're authenticated
aws route53domains list-domains --region us-east-1 | grep 2sum.ai
```

The second command confirms the domain is registered with Route 53 (a
*domain* registration is separate from a Route 53 *hosted zone* — we're
about to create the latter).

## One-time bootstrap (you run these once)

### 1. Fill in the GitHub repo identifiers

Open [`infra/cdk.json`](./infra/cdk.json) and replace `REPLACE_ME` in:

```json
"githubOwner": "REPLACE_ME",
"githubRepo": "REPLACE_ME",
```

The OIDC trust policy uses these to scope which GitHub repo+branch can
assume the deploy role. If you get this wrong, deploys will fail with
`AccessDenied: Not authorized to perform sts:AssumeRoleWithWebIdentity`.

### 2. Install CDK dependencies

```bash
cd product-site/infra
npm install
```

### 3. Bootstrap CDK in your account (once per account+region)

This creates the CDK staging bucket and execution role:

```bash
npx cdk bootstrap aws://<ACCOUNT_ID>/us-east-1
```

Find your account ID with `aws sts get-caller-identity --query Account --output text`.

### 4. Deploy the stack

```bash
npx cdk deploy
```

Confirm the IAM changes when prompted. Deploy takes ~5–10 minutes (most of
it is CloudFront distribution provisioning). At the end, CDK prints the
stack outputs — **save these**, you'll need them in steps 5 and 6:

```
Outputs:
MetisSiteStack.BucketName             = metis-site-2sum-ai
MetisSiteStack.DistributionId         = E1ABCDEF234567
MetisSiteStack.DistributionDomain     = d1xxxxxxxxxxxx.cloudfront.net
MetisSiteStack.DeployRoleArn          = arn:aws:iam::123456789012:role/metis-site-deploy
MetisSiteStack.HostedZoneNameServers  = ns-123.awsdns-12.com,ns-456.awsdns-34.net,...
```

### 5. Delegate DNS to the new hosted zone

The hosted zone has four nameservers (the `HostedZoneNameServers` output).
You need to tell the *registrar* (Route 53 Domains) to point `2sum.ai` at
those nameservers.

In the AWS console:

1. Open **Route 53 → Registered domains → 2sum.ai**.
2. Click **Add or edit name servers**.
3. Replace the four entries with the values from `HostedZoneNameServers`
   (one nameserver per line, comma-separated in the CDK output).
4. Save.

CLI equivalent:

```bash
aws route53domains update-domain-nameservers \
  --region us-east-1 \
  --domain-name 2sum.ai \
  --nameservers \
      Name=ns-123.awsdns-12.com \
      Name=ns-456.awsdns-34.net \
      Name=ns-789.awsdns-56.org \
      Name=ns-1011.awsdns-78.co.uk
```

DNS propagation typically takes 15–60 minutes but can take up to 48 hours.
Check progress with `dig +short NS 2sum.ai` — once it shows the new
nameservers, the rest will follow.

### 6. Add GitHub repo secrets

In your GitHub repo settings (**Settings → Secrets and variables →
Actions → New repository secret**), add three:

| Secret name | Value | Source |
|---|---|---|
| `AWS_DEPLOY_ROLE_ARN` | `arn:aws:iam::...:role/metis-site-deploy` | CDK output `DeployRoleArn` |
| `AWS_SITE_BUCKET` | `metis-site-2sum-ai` | CDK output `BucketName` |
| `AWS_DISTRIBUTION_ID` | `E1ABCDEF234567` | CDK output `DistributionId` |

### 7. First deploy

Push to `main` (or run the workflow manually from the Actions tab). The
workflow builds the site, syncs to S3 with cache-control headers, and
invalidates CloudFront. ~2–4 minutes end to end.

Before DNS propagates, you can preview at the
`DistributionDomain` URL (`d1xxx.cloudfront.net`) — TLS won't match the
final domain but the content will be live.

## Ongoing deploys

Just push to `main`. The workflow only runs when files under
`product-site/` change (see the `paths:` filter in the workflow), so
unrelated changes don't trigger marketing-site rebuilds.

To deploy manually without pushing:

- **From GitHub:** Actions tab → "Deploy product site" → Run workflow.
- **From your laptop:** with AWS credentials in your shell,

  ```bash
  cd product-site
  npm run build
  aws s3 sync dist/ s3://metis-site-2sum-ai/ --delete
  aws cloudfront create-invalidation \
      --distribution-id E1ABCDEF234567 \
      --paths "/*"
  ```

## Rolling back

S3 versioning is on. If a bad deploy lands, you have two options:

**Roll forward (recommended):** revert the offending commit, push to
`main`, let the normal deploy flow take over.

**Restore from S3 versions:** for each affected object, find the previous
version in the S3 console and restore it. Then invalidate `/*` in
CloudFront. Use sparingly — there's no automation for this.

## Updating the infrastructure

Any change to `infra/` (e.g. tweaking the CloudFront Function, adding a
second behavior, changing cache TTLs) follows the same flow:

```bash
cd product-site/infra
npx cdk diff       # see what's about to change
npx cdk deploy     # apply
```

`cdk diff` is your friend — read every change before saying yes.

## Tearing it down

```bash
cd product-site/infra
npx cdk destroy
```

Two things won't auto-delete and will need manual cleanup if you really
want them gone:

- The **S3 bucket** (kept on `RemovalPolicy.RETAIN` so a `cdk destroy`
  can't nuke production content). Empty it first, then delete.
- The **Route 53 hosted zone** (CDK will try to delete it, but it'll fail
  if there are still records you added manually outside CDK). Delete
  stale records first.

The registered domain itself is unaffected — that lives in Route 53
Domains, not in this stack.

## Troubleshooting

**`Error: The certificate must be in the us-east-1 region.`**
You changed the stack region. CloudFront's certificate has to live in
us-east-1; the rest of the stack should follow it there (it's pinned in
[`bin/metis-site.ts`](./infra/bin/metis-site.ts)). Don't change the
region without splitting the stack.

**`AccessDenied: Not authorized to perform sts:AssumeRoleWithWebIdentity`**
The OIDC trust policy doesn't match your GitHub repo. Re-check
`githubOwner`/`githubRepo`/`githubBranch` in `cdk.json`, run
`cdk deploy` again, and the role's trust policy will update.

**Site loads at `d1xxx.cloudfront.net` but `2sum.ai` shows DNS error.**
DNS hasn't propagated yet, or the registrar nameservers weren't updated.
Re-run step 5 and `dig +short NS 2sum.ai`.

**Browser shows old content after deploy.**
CloudFront cache. The workflow invalidates `/*` after every sync, but
your browser cache can still hold the HTML for up to 0 seconds (HTML is
sent with `max-age=0, must-revalidate`). Hard-refresh
(<kbd>Cmd-Shift-R</kbd>) to bypass. If that doesn't fix it, check the
Actions log to confirm the invalidation actually ran.

**`www.2sum.ai` doesn't redirect to `2sum.ai`.**
The CloudFront Function expects `host` to be exactly `www.2sum.ai`. Open
[`infra/cloudfront/viewer-request.js`](./infra/cloudfront/viewer-request.js)
and confirm the placeholder substitution worked — after synth, the file
in `cdk.out/` should have the real domain baked in. If you renamed the
domain, redeploy the stack.
