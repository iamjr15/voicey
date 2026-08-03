# Changelog

All notable changes are recorded here. Voicey follows Semantic Versioning for
release compatibility; Python package indexes may normalize prerelease spelling
to PEP 440.

## Unreleased

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

### Changed

- `voicey doctor` now reports an installed but uncertified Pipecat or LiveKit
  version as a loud warning instead of failing an otherwise usable project.

### Security

- Release workflows produce private CI artifacts only. Public package
  publishing remains an explicit human-only action after rename and review.
