# Upgrading voicey

Voicey upgrades its engine lock entry without rewriting project source. Recipe
updates are always reviewed and merged by a human or coding agent.

## Before upgrading

The built-in path requires:

- a completed voicey project with regular `pyproject.toml` and `uv.lock`
  files;
- a direct `voicey` requirement in `project.dependencies`;
- `uv >=0.11,<1`;
- committed local work, so your normal version-control recovery remains
  available.

Projects generated before the recipe-baseline contract may not yet have
`voicey.recipe-lock.json`. When the manifest recipe version exactly matches
the installed package, the first upgrade captures that known upstream source
as the baseline before changing the package. An older or inconsistent recipe
cannot be reconstructed and fails with `VY-UPG-003`.

## Stable and canary channels

Preview drift without changing anything:

```bash
voicey recipes update-check
voicey recipes update-check --json
```

Upgrade to a stable release:

```bash
voicey upgrade --stable --yes
```

Exercise a prerelease canary:

```bash
voicey upgrade --pre --yes
```

Both upgrade modes run these `uv` operations:

```bash
uv lock --upgrade-package voicey --prerelease if-necessary-or-explicit  # stable
uv lock --upgrade-package voicey --prerelease allow     # canary
uv sync --locked --prerelease <the-same-mode>
uv run --locked --prerelease <the-same-mode> voicey recipes update-check --json
```

The stable resolver may use a prerelease-tagged transitive dependency when an
explicit dependency contract or the available package set requires it. Before
sync, voicey separately parses its own selected version and rolls back if
that version is a prerelease. The CLI preserves `pyproject.toml` and every
recipe-owned source file byte for byte. It reports the locked engine version
and recipe drift from the newly synced process. Use `--json` on `upgrade` for
CI. The repeated mode is required by uv's locked-environment contract:
resolving with a non-default prerelease mode and then omitting it from `sync`
or `run` causes uv to reject the lock as needing an update.

```bash
voicey upgrade --stable --yes --json
```

## Reading the drift report

`voicey.recipe-lock.json` is the exact upstream base that was copied into the
project. It is public, deterministic project metadata and must be committed; it
must never contain credentials.

Each recipe-owned path receives one status:

| Status | Meaning |
|---|---|
| `unchanged` | Local and installed upstream still match the base |
| `local-only` | The project changed; upstream did not |
| `upstream-only` | Upstream changed; the project still matches the base |
| `converged` | Local and upstream made the same change |
| `conflict` | Local and upstream differ from the base and each other |

Machine output contains only paths and SHA-256 digests, not recipe source.
When an update or conflict exists, the report includes an AI-merge prompt that
requires hunk-level review, preserves local integrations and policies, keeps
conversation logic native to the selected runtime, and prohibits both MCP and
wholesale overwrites.

After reviewing and testing a recipe merge:

1. set `recipe.version` in `voicey.jsonc` to the reviewed upstream version;
2. replace the baseline `version` and `files` with that exact installed
   upstream recipe source;
3. run `voicey recipes update-check` and require zero conflicts;
4. run the recipe scenario suite and `voicey test`;
5. commit the project source, manifest, baseline, and changed `uv.lock`
   together.

## Failure and recovery

If lock resolution, sync, lock parsing, or the fresh drift check fails, voicey
restores the previous `uv.lock` and best-effort syncs it. A newly created
lockfile is removed. The error intentionally excludes package-index URLs,
stderr, and environment values.

If a dependency hook changes `pyproject.toml` or recipe-owned source, voicey
restores the prior lock but leaves the unexpected project-source change
visible for inspection; it never overwrites authored source while recovering.
Review `git diff`, restore that change through version control if appropriate,
and rerun only after identifying the dependency behavior.

Next: run `voicey doctor`.
