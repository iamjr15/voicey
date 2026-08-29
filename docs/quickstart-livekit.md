# LiveKit quickstart

This path creates a browser voice agent whose conversation logic is a native
`livekit.agents.Agent`. Budget five minutes after Python, `uv`, and `ffmpeg`
are installed. A LiveKit project is required for the real browser call.

## 1. Install the CLI

Install the pinned stable release with the LiveKit runtime:

```bash
uv tool install 'voicey[livekit]==1.0.0'
```

## 2. Create the project

The command supplies product choices but no credentials. In an interactive
terminal, voicey asks for each missing provider key and the LiveKit project
values, validates them with read-only requests, and writes `.env` itself.

<!-- voicey-doc-test:start -->
```bash
voicey init ./hello-livekit \
  --name hello-livekit \
  --recipe scratch \
  --description "Answer concise product questions and confirm uncertainty." \
  --channels web \
  --runtime livekit \
  --models stt=deepgram/nova-3,llm=anthropic/claude-sonnet-5,tts=cartesia/sonic-3.5 \
  --no-draft-prompts \
  --yes
```
<!-- voicey-doc-test:end -->

The generated `flow.py` returns a native LiveKit `Agent`; there is no voicey
workflow DSL. `tools.py` contains a typed placeholder function and `agent.py`
contains only the shared engine configuration.

## 3. Verify and talk

```bash
cd hello-livekit
voicey doctor
voicey dev
```

Open the printed admin playground URL, allow microphone access, and speak. The
admin listener exchanges a one-use voicey credential for a short-lived,
least-privilege room token on the public listener; provider secrets never enter
the browser. Stop with Ctrl-C to drain the native worker.

CI runs the exact marked `voicey init` command from a freshly installed
wheel, imports and instantiates the generated native `Agent` and typed tool,
constructs the pinned `AgentServer`/`AgentSession` provider-mocked path, and
verifies the terminal signed result under the five-minute budget. A real
LiveKit room and microphone conversation remain credentialed evidence.

Next: read [LiveKit runtime ownership](runtimes/livekit.md), then provision a
phone path from the appropriate [carrier guide](index.md#carriers).
