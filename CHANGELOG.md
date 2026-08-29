# Changelog

All notable changes are recorded here. Voicey follows Semantic Versioning for
release compatibility; Python package indexes may normalize prerelease spelling
to PEP 440.

## Unreleased

## 1.0.0 - 2026-08-29

### Added

- Production dual-runtime Pipecat and LiveKit engine, carrier adapters, native
  recipes, unified testing, protected results delivery, deploy targets,
  observability, chaos/soak tooling, and safe project upgrades.
- Enforced two-minor deprecation horizon with actionable runtime warnings.
- Non-fatal installed-runtime compatibility diagnostics and scheduled
  Python/range-edge recipe certification.
- Deterministic config, webhook, and CLI public-contract snapshots with
  changelog/docs enforcement in pull requests.
- Installed-wheel canary verification across all four first-party recipes and
  both native runtimes, plus a stable-promotion gate that requires matching
  green canary evidence.
- First public Python canary, `voicey==0.0.0.dev0`, published from reviewed
  commit `25a75f7` and clean-install verified from PyPI on Python 3.14.
- Stable PyPI CI/CD with exact release tags, same-line canary promotion,
  protected-environment approval, isolated OIDC authority, artifact checksum
  verification, and PEP 740 attestations.

### Changed

- `voicey doctor` now reports an installed but uncertified Pipecat or LiveKit
  version as a loud warning instead of failing an otherwise usable project.
- Railway created-only rollback now scopes service-domain deletion with the
  exact ledgered application service, matching the installed 5.30.3 CLI and
  preserving resumable reverse-order cleanup.

### Security

- Constrain the Pipecat extra to NLTK 3.10.2 or newer, resolving
  PYSEC-2026-3726 before the first public Python canary.
- Build every Voicey-owned container virtual environment without pip and
  remove system pip from final stages, eliminating pip's unused vendored
  build-tool dependency set from runtime images.
- Keep PyPI credentials out of command arguments and shell history; bootstrap
  uploads use a project-scoped token, while normal CI/CD uses tokenless PyPI
  Trusted Publishing with short-lived GitHub OIDC identity.
- Raise the Telnyx/companion cryptography floor to 50 and pin the audited
  development installer to pip 26.2+, resolving PYSEC-2026-3552 and
  PYSEC-2026-3721 without suppressions.
- Release publishing authority is isolated from all checkout/build/test steps,
  and every third-party action in the release workflow is commit-pinned.
