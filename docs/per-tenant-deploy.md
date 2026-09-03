# One machine per tenant, behind Cloudflare Access

Serving several people from one Carbon Paper install is unsafe: a stage can run
Python on the host, so every tenant would reach every other tenant's rows. This
directory gives each tenant their own Fly machine, volume and hostname instead,
and puts Cloudflare Access in front of each one.

The tenant machine has **no public IP**. `cloudflared` dials out from inside it,
so the only route in is Cloudflare's edge, and Access decides who gets that far.

| Piece | Where |
|---|---|
| Fly config, one per tenant | `deploy/fly.tenant.toml.template` → `deploy/tenants/<tenant>.toml` |
| Provisioning | `scripts/provision_tenant.sh` |
| Fail-closed check in the app | `app/web/access_gate.py` |

## Prerequisites

A domain whose nameservers point at Cloudflare, and Cloudflare Zero Trust
enabled on that account (free to 50 users). `flyctl` and `cloudflared` on PATH.

## Per tenant

**1. Create the Access application** at *Zero Trust → Access → Applications*:

- Type: Self-hosted. Subdomain `<tenant>`, domain your apex.
- Policy: Allow, `Emails` → that one address.
- Login method: One-time PIN needs no identity provider — Cloudflare mails a
  code, and only to an address a policy already allows.
- Copy the **Application Audience (AUD) tag** from the app's Overview.

**2. Run the provisioner:**

```
TENANT=alice DOMAIN=example.com \
ACCESS_TEAM=yourteam ACCESS_AUD=<aud tag> \
ANTHROPIC_CREDENTIAL=sk-ant-... scripts/provision_tenant.sh
```

It creates the tunnel and DNS record, creates the Fly app and volume, writes
`deploy/tenants/alice.toml`, stages the secrets and deploys. It fails loudly if
the app ends up holding a public IP, because that would bypass Access.

For a subscription token rather than a metered key, set
`ANTHROPIC_ENV_NAME=CLAUDE_CODE_OAUTH_TOKEN`. Never set both — the API key wins
and the subscription silently goes unused.

## What this does not protect

- **The Anthropic credential is shared and readable by the tenant.** A Python
  stage runs with the machine's environment, so a tenant can take the key and
  spend on it. This is a deliberate choice that holds only while every tenant is
  someone you already trust. Giving each tenant their own key is the fix.
- **Nothing meters spend.** Chat has no cost field at all.
- **A tenant machine runs 24/7** (~$11.81/mo). A tunnel dials out, so a stopped
  machine has no tunnel and no request can wake it.

## The gate in the app

`app/web/access_gate.py` verifies the `Cf-Access-Jwt-Assertion` header against
Cloudflare's public keys and refuses everything else. It installs itself only
when `CARBON_PAPER_ACCESS_TEAM` and `CARBON_PAPER_ACCESS_AUD` are set, which the
tenant template does and a local run does not.

It is not the perimeter — having no public IP is. It is a tripwire: if a machine
is ever exposed by mistake, requests fail closed instead of serving an
unauthenticated Carbon Paper to the internet.
