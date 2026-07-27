# Lead-intake recipe

`lead-intake@1.0.0` qualifies from business-need facts, obtains explicit
follow-up consent, captures confirmed contact details, and schedules a follow-up
only after successful CRM capture. Protected traits and unrelated sensitive
data are prohibited inputs.

The Pipecat variant is a native `NodeConfig`. The LiveKit workflow hands off to
a contact specialist using pinned native name and email tasks. Replace
`TodoLeadGateway`, configure retention/consent language, and configure Results.

```bash
voicekit init ./lead-intake \
  --name lead-intake \
  --recipe lead-intake \
  --runtime livekit \
  --channels web \
  --models stt=deepgram/nova-3,llm=anthropic/claude-sonnet-5,tts=cartesia/sonic-3.5 \
  --no-draft-prompts \
  --yes
```

Next: run `voicekit test --report junit`, then inspect that a consent refusal
produces no durable lead.
