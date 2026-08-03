# Front-desk recipe

`front-desk@1.0.0` answers only from an approved knowledge lookup, triages
without diagnosing, takes confirmed callback messages, and asks for consent
before warm transfer. Immediate safety emergencies are directed to local
emergency services without a hold or transfer.

The Pipecat runtime uses voicey's Twilio conference bridge for warm transfer;
LiveKit uses its native warm-transfer task. Replace `TodoFrontDeskGateway`,
configure the transfer destination, approved corpus, emergency language, and
Results receiver.

## Demo audio

<audio controls src="../assets/recipes/front-desk-demo.mp3">
  <a href="../assets/recipes/front-desk-demo.mp3">Download the front-desk demo</a>.
</audio>

The checked-in illustrative exchange demonstrates reason capture and explicit
briefing consent. Its source is
[demo-transcripts.json](../assets/recipes/demo-transcripts.json); generated
system speech is not provider or latency evidence.

## Create the project

```bash
voicey init ./front-desk \
  --name front-desk \
  --recipe front-desk \
  --runtime livekit \
  --channels phone \
  --phone-provider twilio \
  --models stt=deepgram/nova-3,llm=anthropic/claude-sonnet-5,tts=cartesia/sonic-3.5 \
  --no-draft-prompts \
  --yes
```

## Production customization map

| File / setting | Owner action |
|---|---|
| `tools.py:TodoFrontDeskGateway` | Replace approved lookup and message storage with authenticated, idempotent business APIs. |
| `prompts/system.md` | Supply the approved knowledge boundary, departments, hours, and non-diagnostic language. |
| `prompts/failure.md` | Define callback and outage language without fabricating availability. |
| `prompts/voicemail.md` | Keep the message under 20 seconds and free of sensitive detail. |
| `VOICEY_TRANSFER_NUMBER` | Inject the reviewed E.164 transfer destination at runtime. |
| `agent.py:Results` | Configure the verified receiver and redaction policy. |

## Verification

`voicey test` covers approved-knowledge refusal, confirmed message capture,
emergency language, transfer consent, decline, and native handoff compilation
on both runtimes. After a successful warm-transfer tool result, the caller is
told the handoff is complete and the response ends rather than contradicting
the terminal status. The 2026-08-03 Anthropic API text certification passed all
six cases first attempt on each runtime; it did not place a phone call.

Next: run `voicey test`, then certify the conference-bridge checklist in
`docs/GAPS.md` on a funded Twilio account.
