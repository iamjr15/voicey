# Lead-intake recipe

`lead-intake@1.0.0` qualifies from business-need facts, obtains explicit
follow-up consent, captures confirmed contact details, and schedules a follow-up
only after successful CRM capture. Protected traits and unrelated sensitive
data are prohibited inputs.

The Pipecat variant is a native `NodeConfig`. The LiveKit workflow hands off to
a contact specialist using pinned native name and email tasks. Replace
`TodoLeadGateway`, configure retention/consent language, and configure Results.

## Demo audio

<audio controls src="../assets/recipes/lead-intake-demo.mp3">
  <a href="../assets/recipes/lead-intake-demo.mp3">Download the lead-intake demo</a>.
</audio>

The checked-in illustrative exchange demonstrates follow-up consent and
read-back confirmation. Its source is
[demo-transcripts.json](../assets/recipes/demo-transcripts.json); generated
system speech is not provider or latency evidence.

## Create the project

```bash
voicey init ./lead-intake \
  --name lead-intake \
  --recipe lead-intake \
  --runtime livekit \
  --channels web \
  --models stt=deepgram/nova-3,llm=anthropic/claude-sonnet-5,tts=cartesia/sonic-3.5 \
  --no-draft-prompts \
  --yes
```

## Production customization map

| File / setting | Owner action |
|---|---|
| `tools.py:TodoLeadGateway` | Replace the stub with the CRM API, idempotency key, lawful retention, and deletion path. |
| `prompts/system.md` | Define qualifying business facts and prohibit protected-trait inference. |
| `prompts/failure.md` | Define a safe follow-up fallback without claiming a stored lead. |
| `prompts/voicemail.md` | Request only the minimum approved callback information. |
| `agent.py:Results` | Redact or omit contact fields not required downstream. |
| `testing.jsonc` | Select the local Ollama judge or an explicit reviewed cloud override. |

## Verification

`voicey test --report junit` covers consent refusal, correction, contact
confirmation, CRM failure, scheduling order, and both native runtime compilers.
Once all four non-sensitive qualification facts are present, the read-only
`qualify_inquiry` step runs without an extra confirmation; contact persistence
still requires explicit consent and final read-back. The 2026-08-03 Anthropic
API text certification passed all six cases first attempt on each runtime.

Next: run `voicey test --report junit`, then inspect that a consent refusal
produces no durable lead.
