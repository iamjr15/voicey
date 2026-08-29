# Release engineering

Voicey release preparation and stable publication are automated behind explicit
human approval. The workflow builds without publishing authority, then gives a
separate protected job only a short-lived OIDC identity. It never stores a PyPI
token. The public repository is `iamjr15/voicey`; the one-time GitHub/PyPI
trust binding remains a human-authorized operation. Voicey 1.0.0 used the
documented bootstrap path because that remote was not yet integrated; OIDC is
the required steady-state path once the binding exists.

## Version policy

Stable releases use `MAJOR.MINOR.PATCH` and follow Semantic Versioning:

- patch: compatible fixes with no public-contract removal;
- minor: compatible additions and announced deprecations;
- major: an intentional incompatible public-contract change.

The public contract includes serialized `Agent` configuration, the top-level
Python API, `tool`, `results`, and `testing` APIs, webhook payloads, CLI
commands/flags, and adapter protocols. Python build metadata uses PEP 440, so a
SemVer canary such as `1.2.0-rc.1` is normally exposed by installers as
`1.2.0rc1`. The release line remains `1.2.0`.

A public API may be deprecated only with:

1. a `VoiceyDeprecationWarning` at runtime;
2. an entry in `CHANGELOG.md`;
3. an exact replacement and migration URL; and
4. a removal version at least two minor releases after the first warning.

`Deprecation` validates the horizon when declared, while
`validate_deprecations()` prevents an overdue declaration from passing release
preparation.

## Public-contract snapshots

The executable sources of truth produce:

- `docs/api/snapshots/config-schema.json` from serialized `Agent`;
- `docs/api/snapshots/webhook-schema.json` from the actual validated webhook
  envelope; and
- `docs/api/snapshots/cli-surface.json` from the installed Typer command tree.

Check them without changing the tree:

```bash
uv run python scripts/update_public_snapshots.py --check
```

For an intentional contract change, update code, spec, tests, docs, and
`CHANGELOG.md`, then explicitly regenerate:

```bash
uv run python scripts/update_public_snapshots.py
git diff -- docs/api/snapshots
```

CI rejects stale snapshots. On pull requests, a snapshot diff also requires
`CHANGELOG.md` and at least one explanatory `docs/*.md` page.

## Runtime compatibility

Every runtime window is backed by installed-version evidence. Startup inspects
the installed distribution. Missing dependencies still fail at their normal
import or doctor boundary; an installed version outside the certified window
emits `RuntimeCompatibilityWarning` with the compatibility-table link and
continues.

The scheduled `Runtime compatibility edges` workflow installs each declared
edge on Python 3.11 and 3.14, confirms the reported distribution version, and
executes every first-party recipe on that runtime. Expand a window only in the
same commit that adds its lower/upper edge rows and records empirical evidence
in [the compatibility table](compatibility.md).

## Canary before stable

Build the release-shaped wheel and validate the current prerelease locally:

```bash
uv build --out-dir dist
VOICEY_WHEEL="$(find dist -maxdepth 1 -name '*.whl' -type f)"
uv run python tests/verification/run_p4_release_gate.py \
  --wheel "$VOICEY_WHEEL" \
  --channel canary \
  --report .voicey/verification/p4-release-report.json
```

The gate reads wheel metadata, creates a fresh environment, installs only that
wheel with both runtime extras, compiles every packaged scenario, and
instantiates each recipe's native Pipecat node and LiveKit `Agent`. It also
checks SemVer/deprecation policy, compatibility policy, and all public
snapshots.

The manually dispatched `Release to PyPI` workflow accepts only an exact
`refs/tags/v<project.version>` ref and a matching channel. Canary runs upload
the wheel, sdist, checksums, and evidence as a private Actions artifact. A
stable run must name this workflow's prior canary run id. Stable preparation
downloads the exact named artifact and requires:

- a green P4.5 canary result;
- the same `MAJOR.MINOR.PATCH` release line;
- a valid recorded canary artifact digest; and
- the full installed-wheel recipe gate to pass again.

Stable validation command, using the downloaded report:

```bash
uv run python tests/verification/run_p4_release_gate.py \
  --wheel dist/voicey-1.2.0-py3-none-any.whl \
  --channel stable \
  --canary-report canary-evidence/p4-release-report.json \
  --report .voicey/verification/p4-stable-report.json
```

The stable job then pauses at GitHub's protected `pypi` environment. After an
authorized reviewer approves it, the job downloads the current run's exact
bundle, verifies its checksum manifest and one-wheel/one-sdist shape, and uses
PyPI Trusted Publishing. Build code has no OIDC permission; the publishing job
does not check out or build source. Duplicate uploads fail closed and PEP 740
attestations are enabled.

## One-time Trusted Publisher setup

For the existing `iamjr15/voicey` GitHub repository:

1. Create a GitHub environment named `pypi`, restrict it to release tags, and
   require a reviewer before deployment.
2. In the existing PyPI `voicey` project, add a GitHub Trusted Publisher with
   the exact repository owner, repository name, workflow `release.yml`, and
   environment `pypi`.
3. Do not create `PYPI_API_TOKEN` or any other upload secret in GitHub.
4. Push an exact prerelease tag such as `v1.1.0rc1`, dispatch the canary
   channel, and retain its run id.
5. Push `v1.1.0`, dispatch the stable channel with that canary run id, review
   the private evidence, and approve the protected environment.

The repository workflow is implemented and tested. Its protected environment
and PyPI identity binding must both exist before an OIDC publication can run.

## Approved bootstrap upload

The public project was bootstrapped before a GitHub repository existed. The
manually approved stable upload therefore used the project-scoped `voicey`
token from the operator credential manager. This was a bootstrap path, not the
steady-state CI/CD design.

Build from the exact reviewed commit into a fresh directory, validate both
artifacts, and rerun the installed-wheel canary gate:

```bash
RELEASE_DIST="$(mktemp -d)"
uv build --out-dir "$RELEASE_DIST"
uvx --from twine twine check "$RELEASE_DIST"/*
VOICEY_WHEEL="$(find "$RELEASE_DIST" -maxdepth 1 -name '*.whl' -type f)"
uv run python tests/verification/run_p4_release_gate.py \
  --wheel "$VOICEY_WHEEL" \
  --channel stable \
  --canary-report .voicey/verification/p4-1.0.0-canary-report.json \
  --report .voicey/verification/p4-1.0.0-stable-report.json
```

Load the project-scoped token from the credential manager into the publisher
process only, upload the exact checked wheel and sdist, and clear it from the
environment immediately. Never paste it into a command argument or repository
file:

```bash
UV_PUBLISH_TOKEN="$(security find-generic-password \
  -s pypi.org.voicey.project-upload -a iamjr15 -w)" \
  uv publish --check-url https://pypi.org/simple/ "$RELEASE_DIST"/*
unset UV_PUBLISH_TOKEN
```

Revoke every bootstrap account-wide token once the project-scoped token exists.
After Trusted Publishing succeeds, revoke the project-scoped token too.

Verify index bytes from a clean environment with no checkout import path:

```bash
VERIFY_ROOT="$(mktemp -d)"
uv venv --python 3.14 "$VERIFY_ROOT/.venv"
uv pip install --python "$VERIFY_ROOT/.venv/bin/python" \
  --no-cache --default-index https://pypi.org/simple \
  'voicey[pipecat,livekit]==1.0.0'
(cd "$VERIFY_ROOT" && "$VERIFY_ROOT/.venv/bin/voicey" --version)
```

Record the PyPI URL, artifact SHA-256, clean-install output, and exact source
commit in `docs/COMPLETION-REPORT.md`. Never store the account password or
upload token in the repository, shell history, release report, or generated
artifact.

## Published 1.0.0 evidence

The bootstrap publication completed on 2026-08-29 from exact commit `e414bee`,
which remains the target of annotated tag `v1.0.0`. The same-line `1.0.0rc1`
canary, stable installed-wheel gate, security gate, and Twine validation were
green before upload. Public artifacts are:

- wheel SHA-256:
  `6df19c44cb5ffe6f97feca11d7824d75c398118d844aaa31c19045c5bd3733fe`;
- sdist SHA-256:
  `af9929a49bf8a7bc2c828c522707a295027d98c381fcaca40bd2c1c4ab7001e8`.

Direct public redownloads matched the gated artifacts byte-for-byte. A clean
Python 3.14 environment installed 148 packages with both runtime extras and
loaded Voicey only from temporary site-packages; a separate cache-disabled
unversioned install selected 1.0.0. The superseded `0.0.0.dev0` release is
yanked, not deleted.

The project-scoped bootstrap token remains only until a Trusted Publishing run
succeeds. The temporary account-wide claim token still requires manual removal
because PyPI rejected the supplied account password during the revocation
attempt; see `docs/GAPS.md`. No credential value is stored in this repository.
