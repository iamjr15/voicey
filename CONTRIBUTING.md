# Contributing to Voicey

Thanks for helping improve the `voicey` npm package.

## Before you start

- Search the [issue tracker](https://github.com/iamjr15/voicey/issues) for an
  existing report or proposal.
- Open an issue before starting a substantial feature so the intended package
  surface can be discussed.
- Keep pull requests focused, documented, and free of credentials or personal
  data.

## Local checks

Use a current Node.js LTS release, then run these commands from the repository
root:

```sh
npm pack --dry-run
node --input-type=module -e 'import pkg from "./index.js"; console.log(pkg)'
```

The GitHub Actions workflow runs equivalent checks on every push and pull
request. Please make sure they pass before requesting review.

## Pull requests

Describe the user-facing behavior, tests, and documentation changes in the
pull request. Do not change the version or create a release tag; maintainers
own releases and npm publishing.
