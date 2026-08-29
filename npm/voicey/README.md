# Voicey

[![Continuous Integration](https://github.com/iamjr15/voicey/actions/workflows/ci.yml/badge.svg)](https://github.com/iamjr15/voicey/actions/workflows/ci.yml)
[![npm version](https://img.shields.io/npm/v/voicey?logo=npm)](https://www.npmjs.com/package/voicey)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](https://github.com/iamjr15/voicey/blob/main/LICENSE)

Voicey is an open-source toolchain for building, testing, deploying, and
operating production voice agents with [Pipecat](https://www.pipecat.ai/) and
[LiveKit](https://livekit.io/). It is designed to keep conversation logic in
the native framework APIs while supplying the surrounding operational tooling.

## Package status

`voicey` is the official JavaScript package for the project. Its current,
intentional surface is a small ESM metadata entry point; the JavaScript CLI and
runtime integrations are not yet published. This README documents the package
as it exists today so users do not need to infer support from the broader
project roadmap.

## Install

Install with a current Node.js LTS release:

```sh
npm install voicey
```

## Use

```js
import voicey, { name, version } from "voicey";

console.log(`${name}@${version}`);
console.log(voicey);
```

The default export is an immutable object containing the package `name` and
`version`.

## Development

```sh
git clone https://github.com/iamjr15/voicey.git
cd voicey/npm/voicey

# Verify the exact files that npm would publish.
npm pack --dry-run

# Verify the public ESM entry point.
node --input-type=module -e 'import pkg from "./index.js"; console.log(pkg)'
```

The monorepo's continuous integration runs both checks for every push and pull
request. An exact `npm-v<package-version>` tag triggers npm publishing through
GitHub Actions OIDC; Python releases use the separate `v<version>` namespace.
Neither release path requires a long-lived registry token in GitHub.

## Get help

For bugs, feature requests, and questions, use the
[issue tracker](https://github.com/iamjr15/voicey/issues). Please include the
package version, Node.js version, a minimal reproduction, and the expected and
actual behavior.

## Contributing

Contributions are welcome. Read the
[contribution guide](https://github.com/iamjr15/voicey/blob/main/CONTRIBUTING.md)
before opening a pull request.

## Security

Do not report suspected vulnerabilities in a public issue. Follow the
[security policy](https://github.com/iamjr15/voicey/blob/main/SECURITY.md)
instead.

## License

Voicey is licensed under [Apache-2.0](https://github.com/iamjr15/voicey/blob/main/LICENSE).
