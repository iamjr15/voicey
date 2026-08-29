# Voicey naming record

The public product name is **Voicey**, finalized on 2026-07-31.

- The unscoped npm package is published as `voicey@1.0.1` by `iamjr15`. Its
  source lives under `npm/voicey/`, and the root release workflow targets that
  directory. npm uses `npm-v<version>` tags while the Python distribution uses
  `v<version>` tags.
- The PyPI distribution name is reserved by `iamjr15` through the published,
  release/security-gated `voicey==1.0.0` release from exact commit `e414bee`.
  A cache-disabled Python 3.14 install and both public artifact digests were
  verified on 2026-08-29; the superseded `0.0.0.dev0` release is yanked.
- The Python distribution/import namespace, CLI executable, entry-point
  groups, config filenames, environment prefix, telemetry/protocol identifiers,
  public error types/codes, documentation, examples, tests, and CI were renamed
  together.
- Repository hosting is public at `iamjr15/voicey`; domain registration remains
  a separate human-owned release decision.

The source for the npm reservation release is retained in `npm/voicey/`.
