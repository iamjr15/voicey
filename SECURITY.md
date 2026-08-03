# Security policy

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Until a public security contact is configured, send a private report to the repository owner with:

- the affected version or commit;
- a minimal reproduction;
- impact and prerequisites;
- any suggested mitigation.

The maintainer will acknowledge the report within 3 business days, provide a triage update within 7 business days, and coordinate disclosure after a fix is available. No bounty is currently offered.

## Supported versions

Before 1.0, only the current `main` branch receives security fixes. Published support ranges will be added before the first release.

## Security boundaries

- Provider and webhook secrets are environment-only and must never enter manifests, logs, call records, images, or commits.
- Inbound carrier requests are signature-verified.
- Result webhooks use Standard Webhooks signatures and replay protection.
- Local protected data uses a `0700` directory and `0600` files.
- `.env*` is ignored except the documentation-only `.env.example`.

Please include `VY-SEC` in private report subjects so reports can be routed consistently.
