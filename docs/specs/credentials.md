# Credentials Specification

**Status:** Shipped v1
**Last updated:** 2026-05-20

> Defines how Metis resolves LLM-provider API keys at runtime. Today the CLI
> and gateway each call `os.environ.get("ANTHROPIC_API_KEY")` etc. directly
> from their bootstrap path ([`cli/runtime.py:112-118`](../../packages/metis/src/metis/cli/runtime.py),
> [`gateway/runtime.py:81-85`](../../packages/metis/src/metis/gateway/runtime.py));
> adding a fourth provider means editing both call sites. This spec replaces
> the direct lookups with a `CredentialResolver` that walks a documented
> priority chain. Env vars keep working — they sit on the chain — but a new
> structured file at `~/.metis/credentials.yaml` becomes the discoverable
> default UX. An OS-keychain tier is specified as an opt-in hook for the
> future without committing to a runtime dependency in v1.

---

## 1. Purpose

Three problems with the current env-var-only approach:

1. **Discoverability.** A new user installs Metis, runs `metis chat`, and sees
   "set ANTHROPIC_API_KEY". They don't know which providers are required vs
   optional, how to verify their key works, or where multiple keys would live.
2. **Per-session pain.** API keys live in shell sessions, `.envrc`, `.env`, or
   `~/.zshrc`. Each shell reload is a small friction tax; adding a fifth
   provider doubles the surface.
3. **Extensibility.** Adding a new LLM provider today touches `cli/runtime.py`
   and `gateway/runtime.py` and the README. The resolver lets the runtime
   discover new providers through a single registry table.

The fix is not a config file (which alone solves discoverability) or a CLI
wizard (which alone solves per-session pain) but a **resolution chain** that
admits both, plus a small `metis auth` CLI surface for setup and diagnostics.

## 2. Scope

### 2.1 In scope

- A `CredentialResolver` Protocol + default implementation
- A documented resolution chain
- A structured file format (`~/.metis/credentials.yaml`)
- A `metis auth` CLI subcommand group (`add`, `list`, `remove`, `test`, `doctor`)
- Backwards compatibility with today's env-var path
- Security posture (file mode, key truncation in logs/UI)
- A hook contract for future OS-keychain integration

### 2.2 Out of scope (deferred)

- OS-keychain implementation (Option B from the design proposal). Spec'd
  as a Protocol extension point; impl deferred until v1.0+.
- Plugin system for adding new providers via Python entry points. Adding
  a provider today means adding one row to a registry table; a plugin
  system is over-engineering for ≤5 providers.
- Per-gateway-key credential routing (the gateway today uses a single
  operator-side credential set for all upstream provider calls; per-buyer
  upstream credentials are a separate Pro-tier feature).
- Secrets rotation policy / expiry tracking. v1 stores keys as plain
  strings; rotation is "delete and re-add."

## 3. Resolution chain

The resolver walks this order, returning the first match:

| Order | Source                              | Use case                                    |
| ----- | ----------------------------------- | ------------------------------------------- |
| 1     | `--api-key provider=<value>`        | Per-invocation override (rare; CI / scripts)|
| 2     | `${PROVIDER}_API_KEY` env var       | Today's path; 12-factor; CI; Docker         |
| 3     | `~/.metis/credentials.yaml`         | New default UX; one-time setup              |
| 4     | `~/.metis/.env`                     | Legacy dotenv support; existing users       |
| 5     | OS keychain (opt-in)                | Future tier; deferred                       |

If all sources miss for a required provider, the resolver raises
`CredentialNotFoundError` with a message that tells the user how to fix it:

```
no credentials configured for anthropic. Add via:
  metis auth add anthropic
or set ANTHROPIC_API_KEY in your environment / .env file
```

### 3.1 Ordering rationale

- **CLI flag first** — explicit per-invocation overrides should win.
- **Env vars second** — 12-factor convention; CI / Docker / production setups
  rely on env vars and should not be silently overridden by a leftover file.
- **Credentials file third** — the recommended default for interactive users.
- **Legacy `.env` fourth** — preserves the existing dotenv workflow without
  promoting it as the default.
- **Keychain last** — when implemented, it's a fallback for users who haven't
  explicitly added a key elsewhere. Operators who want keychain-as-default
  can set `prefer_keychain: true` in `credentials.yaml` to flip ordering.

## 4. File format

`~/.metis/credentials.yaml` (mode `0o600`):

```yaml
# Schema version. Resolver refuses to load a file whose version it doesn't
# understand. Forward-only migration; no v0 fallback needed in v1.
schema_version: 1

providers:
  anthropic:
    api_key: sk-ant-...
  openai:
    api_key: sk-...
  openrouter:
    api_key: sk-or-...

# Which provider to prefer when the routing engine has multiple candidates
# and a slot doesn't pin one explicitly. Optional; defaults to the first
# provider listed.
default_provider: anthropic

# Reserved for future opt-in keychain integration. Setting this to true
# moves keychain ahead of `~/.metis/.env` in the resolution chain (still
# behind env vars and CLI flag).
prefer_keychain: false
```

### 4.1 Multi-key per provider (v1.1)

v1.0 ships single-key-per-provider. v1.1 adds optional named keys:

```yaml
providers:
  openrouter:
    api_key: sk-or-...           # default key for this provider
    keys:                         # additional named keys
      personal:
        api_key: sk-or-personal-...
      work:
        api_key: sk-or-work-...
default_provider_key: openrouter.work
```

The resolver returns the `default_provider_key` when set, falling back to the
provider's top-level `api_key` otherwise. Named keys are accessed by
`<provider>.<key_name>` everywhere a `provider` argument is accepted (CLI
flag, routing rule, etc.).

### 4.2 File operations

- **Read:** standard YAML load + schema_version check.
- **Write:** atomic via write-temp-then-rename, preserving the file mode.
  Pattern mirrors [`MemoryStore._write`](../../packages/metis/src/metis/core/memory/store.py).
- **Permissions enforcement:** the resolver REFUSES to read the file if
  the OS mode is not `0o600` (rejects world-readable files); raises
  `CredentialsFileInsecure` with the path so the user can `chmod 600` it.

## 5. CLI surface

All subcommands live under `metis auth`. The CLI uses the same resolver
internally — `metis auth add` writes to the credentials file; `metis auth
test` walks the resolution chain.

### 5.1 `metis auth add <provider>`

Interactive: prompts for the API key, optionally validates by pinging a
free endpoint on the provider, then writes to `~/.metis/credentials.yaml`.

```
$ metis auth add anthropic
API key for anthropic: sk-ant-***************
Validating... ✓ (responded in 142ms)
Added to ~/.metis/credentials.yaml
```

Flags:
- `--no-validate` — skip the ping (offline / paranoid mode)
- `--key-name <name>` — store as a named key (v1.1 feature; v1.0 ignores)

### 5.2 `metis auth list`

Shows configured providers and resolution source. Never prints the full key.

```
$ metis auth list
PROVIDER        SOURCE                              KEY
anthropic       ~/.metis/credentials.yaml          sk-ant-1234...wxyz
openai          ANTHROPIC_API_KEY (env)            sk-1234...wxyz
openrouter      (not configured)
```

The display shows first 8 + last 4 characters of the key (`sk-ant-1234...wxyz`).
This is enough for the user to recognize their own key without leaking it to
screen-share viewers.

### 5.3 `metis auth remove <provider>`

Removes the provider's entry from the credentials file. Idempotent. Doesn't
touch env vars or the legacy `.env`.

### 5.4 `metis auth test [provider]`

Pings each configured provider's free endpoint to verify the key works.

```
$ metis auth test
anthropic    ✓ (87ms)
openai       ✓ (104ms)
openrouter   ✗ AUTH error — key may be revoked
```

Validation endpoints:
- **Anthropic:** 1-token completion on haiku, max_tokens=1 (cost ≈ $0.000001)
- **OpenAI:** `GET /v1/models` (free)
- **OpenRouter:** `GET /api/v1/auth/key` (free)
- **Future providers:** declared per-adapter (see §6.2)

### 5.5 `metis auth doctor`

Full diagnostic: which providers configured, last successful call timestamp
from the trace, recent AUTH errors. Buyer-trial debugging surface.

```
$ metis auth doctor
Credential resolver:
  ~/.metis/credentials.yaml         ✓ readable (mode 0o600)
  ~/.metis/.env                     (not present)
  Keychain support                  (opt-in; not active)

Providers:
  anthropic        ✓ configured (~/.metis/credentials.yaml)
                   last successful call: 2026-05-20T14:32:11Z
                   recent AUTH errors:    0 (last 24h)
  openai           ✓ configured (env: OPENAI_API_KEY)
                   last successful call: 2026-05-19T09:11:43Z
                   recent AUTH errors:    1 (last 24h) — see trace event 01HZ...
  openrouter       ✗ not configured
                   Add via: metis auth add openrouter

Default provider: anthropic
```

## 6. Implementation

### 6.1 Protocol

```python
# packages/metis/src/metis/core/credentials/protocol.py

@runtime_checkable
class CredentialResolver(Protocol):
    """Returns API keys for LLM providers. Walks the resolution chain per §3."""

    def get(self, provider: str) -> str | None:
        """Return the API key for `provider`, or None if not configured.

        `provider` is the canonical name ("anthropic", "openai", "openrouter").
        Never raises on missing — callers decide whether absence is fatal.
        """
        ...

    def list_configured(self) -> list[ConfiguredCredential]:
        """Return one entry per configured provider, with source provenance
        but never the full key. Used by `metis auth list` and `doctor`."""
        ...
```

### 6.2 Provider registry

Adding a new LLM provider means one row in
`packages/metis/src/metis/core/credentials/providers.py`:

```python
KNOWN_PROVIDERS: dict[str, ProviderSpec] = {
    "anthropic": ProviderSpec(
        env_var="ANTHROPIC_API_KEY",
        validate_endpoint=("POST", "https://api.anthropic.com/v1/messages",
                           {"model": "claude-haiku-4-5", "max_tokens": 1, ...}),
    ),
    "openai": ProviderSpec(
        env_var="OPENAI_API_KEY",
        validate_endpoint=("GET", "https://api.openai.com/v1/models", None),
    ),
    "openrouter": ProviderSpec(
        env_var="OPENROUTER_API_KEY",
        validate_endpoint=("GET", "https://openrouter.ai/api/v1/auth/key", None),
    ),
    # Adding "groq" / "mistral" / "deepseek" is one new entry here.
}
```

The resolver consults this table to map provider → env var name and to
implement `metis auth test`.

### 6.3 Migration

- **Runtime hookup:** [`cli/runtime.py:112-118`](../../packages/metis/src/metis/cli/runtime.py)
  and [`gateway/runtime.py:81-85`](../../packages/metis/src/metis/gateway/runtime.py)
  drop their `os.environ.get` calls and instead instantiate a
  `DefaultCredentialResolver` and pass it to `ModelRegistry.register(...)`.
- **Existing users:** if `~/.metis/credentials.yaml` doesn't exist, the
  resolver falls through to env vars — today's experience is unchanged.
- **First-run hint:** when no credentials are configured anywhere, the
  CLI errors with "run `metis auth add <provider>`" instead of "set
  ANTHROPIC_API_KEY etc."

## 7. Security posture

1. **File mode 0o600.** Enforced on every read; the resolver REFUSES to
   load a credentials file with insecure permissions and tells the user
   how to fix it. Mirrors `~/.ssh/id_*` and `~/.aws/credentials`.
2. **Never log the full key.** The resolver returns keys to callers; logs,
   trace events, and CLI output only show truncated forms (`sk-ant-1234...wxyz`).
3. **No key in error messages.** A failed `metis auth test` shows the
   provider name and HTTP status, never the key.
4. **Atomic writes.** Write-temp-then-rename; on partial-write failure
   the existing file stays intact.
5. **Resolver returns `str`, not a wrapper.** Callers (provider adapters)
   are responsible for not echoing the key. Wrapping in a "secret" type
   adds friction without preventing the dominant leak path (logs from
   exception traces in the adapter).
6. **Keychain integration (future) is the upgrade path** for users who
   want the file-on-disk plaintext gone.

## 8. OS keychain hook (Option B placeholder)

The `CredentialResolver` Protocol is implementation-agnostic. A future
`KeychainCredentialResolver` would:

1. Implement the same Protocol
2. Use the `keyring` library (cross-platform: macOS Keychain / Windows
   Credential Manager / Linux Secret Service)
3. Be opt-in via `prefer_keychain: true` in `credentials.yaml` OR a new
   `--keychain` flag

The v1.0 file resolver MUST be designed so that the keychain resolver
can compose on top of it without breaking compatibility — meaning:
- The resolver's `get(provider)` signature doesn't change
- The provider name vocabulary stays canonical (no keychain-specific names)
- `metis auth list` learns to display keychain-sourced credentials with
  source = `keychain` instead of file path

v1.0 acceptance criterion: the resolver code has a clean injection point
for an additional source between steps 4 and 5 of the chain, and the
`ConfiguredCredential.source` enum accepts a `KEYCHAIN` value even though
v1.0 never emits it.

## 9. Open questions

1. **First-run wizard.** Should `metis chat` on a fresh install detect zero
   configured providers and offer to run `metis auth add` interactively? Or
   should the error message be sufficient? Default lean: error message
   only; wizards have a reputation for being annoying. **Resolved v1
   (2026-05-20):** error message only. `setup_runtime` raises `SetupError`
   with the canonical "Run `metis auth add anthropic` (or set
   ANTHROPIC_API_KEY in env / .env)." string. Revisit if the buyer-trial
   funnel shows the message is missed.
2. **Validation endpoint costs.** Anthropic's `messages` is a paid endpoint;
   1-token validation costs ~$0.000001 per `metis auth test` call. OpenAI's
   `/v1/models` is free. Worth noting in the user-facing message? "Validating
   Anthropic key costs ~$0.000001" feels pedantic but transparent. **Resolved
   v1:** no pre-call disclosure. `--no-validate` is the explicit opt-out.
3. **Per-team credentials in metis-pro.** The Pro tier may eventually want
   per-team or per-customer upstream credentials (so each customer's
   gateway requests use their own Anthropic key, not the operator's). Out
   of scope for OSS v1.0 — but the Protocol should not preclude it. The
   metis-pro overlay could implement a `TeamScopedCredentialResolver`
   that wraps the OSS resolver. **v1 surface:** `CredentialResolver`
   Protocol is structural; Pro overlay can decorate it without touching
   OSS code.
4. **Schema_version migrations.** v1.0 ships at schema_version 1. When v1.1
   adds multi-key support (named keys), the schema bumps to 2 with a
   forward-only migration (v1 files load cleanly under v2 code; v2 files
   refuse to load under v1 code). Confirm policy. **v1 enforcement:**
   `CredentialsFileSchemaUnknown` rejects any `schema_version` outside
   `SUPPORTED_SCHEMA_VERSIONS = (1,)`.

### v1 implementation deviations from earlier drafts

- `ProviderSpec` carries `auth_header_name`, `auth_header_value_template`,
  and `extra_headers` in addition to the spec §6.2 sketch's `env_var` +
  `validate_endpoint` fields. Anthropic's `x-api-key` + `anthropic-version`
  header pair doesn't fit the implicit "everyone uses `Authorization:
  Bearer`" assumption; the spec §6.2 sketch elided the difference.
- The CLI surface omits `--api-key provider=<value>` (spec §3 step 1). The
  resolver's `cli_overrides` constructor argument supports it for programmatic
  use; a top-level CLI flag is deferred until the rare-use bar is met. The
  resolution chain still honors it via `DefaultCredentialResolver(cli_overrides=…)`,
  so the spec §3 ordering is intact end-to-end.

## 10. References

- [`docs/specs/multi-user.md`](multi-user.md) — per-team identity layer; the
  Pro extension for per-team credentials would compose on top of this spec.
- [`docs/specs/pricing.md`](pricing.md) — the "free OSS gateway" tier
  depends on the resolver finding operator-side credentials; this spec
  defines that lookup path.
- [`packages/metis/src/metis/cli/runtime.py`](../../packages/metis/src/metis/cli/runtime.py)
  — current env-var lookup site to be replaced.
- [`packages/metis/src/metis/gateway/runtime.py`](../../packages/metis/src/metis/gateway/runtime.py)
  — second current env-var lookup site.
