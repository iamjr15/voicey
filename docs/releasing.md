# Release engineering

Voicey release preparation is automated, but publication requires explicit
human approval. The workflow never uploads to PyPI, creates a repository,
registers a name, or mutates a public release. An approved operator may perform
the upload with the guarded procedure below.

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

The manually dispatched `Prepare release artifacts` workflow accepts an exact
ref and channel. Canary runs upload the wheel, sdist, and signed-by-CI evidence
as a private Actions artifact. A stable run must name the prior canary workflow
run id. Stable preparation downloads that report and requires:

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

Stable publishing remains blocked until the human reviews the evidence for the
same release line and explicitly approves that public upload. The separately
approved first canary follows the procedure below.

## Human-approved PyPI upload

The name is finalized as Voicey and `RENAME.md` is complete. The first public
Python artifact is the existing `0.0.0.dev0` canary: it reserves the name
without claiming a stable release while paid, physical, and wall-clock gates
remain open.

Build from the exact reviewed commit into a fresh directory, validate both
artifacts, and rerun the installed-wheel canary gate:

```bash
RELEASE_DIST="$(mktemp -d)"
uv build --out-dir "$RELEASE_DIST"
uvx --from twine twine check "$RELEASE_DIST"/*
VOICEY_WHEEL="$(find "$RELEASE_DIST" -maxdepth 1 -name '*.whl' -type f)"
uv run python tests/verification/run_p4_release_gate.py \
  --wheel "$VOICEY_WHEEL" \
  --channel canary \
  --report .voicey/verification/p4-pypi-canary-report.json
```

An unclaimed project cannot have a project-scoped token yet. Create one
account-wide token for the first upload, enter it through a hidden prompt, and
upload the exact checked wheel and sdist without putting the token in command
arguments or shell history:

```bash
UV_PUBLISH_TOKEN="$(python -c 'import getpass; print(getpass.getpass("PyPI token: "))')" \
  uv publish --check-url https://pypi.org/simple/ "$RELEASE_DIST"/*
```

As soon as PyPI creates `voicey`, create a token scoped to that project, store
it in the operator's credential manager, and revoke the account-wide token.

Verify index bytes from a clean environment with no checkout import path:

```bash
VERIFY_ROOT="$(mktemp -d)"
uv venv --python 3.14 "$VERIFY_ROOT/.venv"
uv pip install --python "$VERIFY_ROOT/.venv/bin/python" \
  'voicey==0.0.0.dev0'
(cd "$VERIFY_ROOT" && "$VERIFY_ROOT/.venv/bin/voicey" --version)
```

Record the PyPI URL, artifact SHA-256, clean-install output, and exact source
commit in `docs/COMPLETION-REPORT.md`. Never store the account password or
upload token in the repository, shell history, release report, or generated
artifact.
