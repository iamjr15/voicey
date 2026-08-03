# Pipecat quickstart

This path creates a browser voice agent whose conversation logic is a native
Pipecat Flows `NodeConfig`. Budget five minutes after Python, `uv`, and `ffmpeg`
are installed.

## 1. Install the CLI

Until Voicey is published to PyPI, install the reviewed wheel from a private
release artifact:

```bash
uv tool install --from "./voicey-0.0.0.dev0-py3-none-any.whl[pipecat]" voicey
```

After the Python distribution is published, replace the local wheel path with
its pinned package version.

## 2. Create the project

The command supplies product choices but no credentials. In an interactive
terminal, voicey asks for each missing Deepgram, Anthropic, and Cartesia key,
validates it immediately, and writes the project `.env` itself.

<!-- voicey-doc-test:start -->
```bash
voicey init ./hello-pipecat \
  --name hello-pipecat \
  --recipe scratch \
  --description "Answer concise product questions and confirm uncertainty." \
  --channels web \
  --runtime pipecat \
  --models stt=deepgram/nova-3,llm=anthropic/claude-sonnet-5,tts=cartesia/sonic-3.5 \
  --no-draft-prompts \
  --yes
```
<!-- voicey-doc-test:end -->

The generated `flow.py` imports `NodeConfig` from `pipecat.flows`; there is no
voicey flow DSL. `tools.py` contains a typed placeholder function and
`agent.py` contains only the shared engine configuration.

## 3. Verify and talk

```bash
cd hello-pipecat
voicey doctor
voicey dev
```

Open the printed admin playground URL, allow microphone access, and speak. The
public media/signaling listener and protected admin listener are different
ports. Stop with Ctrl-C; the process closes admission, drains active calls, and
prints the next command.

CI runs the exact marked `voicey init` command from a freshly installed
wheel, imports and executes the generated native flow and typed tool, connects
a provider-mocked SmallWebRTC browser peer, and verifies the terminal signed
result under the five-minute budget. A real provider conversation remains a
credentialed gate and is never represented by that provider-mocked proof.

Next: read [Pipecat runtime ownership](runtimes/pipecat.md), then replace the
placeholder tool and prompt using [Configuration](configuration.md).
