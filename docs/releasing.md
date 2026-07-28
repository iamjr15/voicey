# Release engineering

Voicekit release preparation is automated, but publication is intentionally
human-only. The workflow never uploads to PyPI, creates a repository, registers
a name, or mutates a public release.

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

1. a `VoicekitDeprecationWarning` at runtime;
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
VOICEKIT_WHEEL="$(find dist -maxdepth 1 -name '*.whl' -type f)"
uv run python tests/verification/run_p4_release_gate.py \
  --wheel "$VOICEKIT_WHEEL" \
  --channel canary \
  --report .voicekit/verification/p4-release-report.json
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
  --wheel dist/voicekit-1.2.0-py3-none-any.whl \
  --channel stable \
  --canary-report canary-evidence/p4-release-report.json \
  --report .voicekit/verification/p4-stable-report.json
```

Publishing remains blocked until the human chooses the final name, executes
`RENAME.md`, reviews the artifact evidence, and explicitly performs the public
upload.
