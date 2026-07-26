# Human-only rename procedure

`voicekit` is a placeholder. Do not execute this procedure until the final public name is selected and collision checks are complete.

Use one dedicated, reviewable commit before the first publish:

1. Rename `src/voicekit/` and update every Python import.
2. Update `project.name`, the `voicekit` console script, wheel package path, and all optional-extra install strings in `pyproject.toml`.
3. Rename entry-point groups `voicekit.telephony` and `voicekit.providers`.
4. Rename manifest/config filenames (`voicekit.jsonc`) and environment-variable prefixes where the product spec requires it.
5. Update CLI text, error URLs/codes only if the naming policy requires it, documentation, recipes, examples, Docker artifacts, CI, and security-policy text.
6. Regenerate config/webhook/CLI schema snapshots and the API reference.
7. Run `rg -i "voicekit"` and classify every remaining hit as intentional history or a missed rename.
8. Run the full Python/runtime matrix, first-party recipes, package build/install smoke, container scan, and both quickstarts.
9. Re-run package, executable, repository, and domain collision checks.

Publishing, repository creation, package registration, and domain registration remain human-only.
