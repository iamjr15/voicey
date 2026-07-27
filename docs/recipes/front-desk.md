# Front-desk recipe

`front-desk@1.0.0` answers only from an approved knowledge lookup, triages
without diagnosing, takes confirmed callback messages, and asks for consent
before warm transfer. Immediate safety emergencies are directed to local
emergency services without a hold or transfer.

The Pipecat runtime uses voicekit's Twilio conference bridge for warm transfer;
LiveKit uses its native warm-transfer task. Replace `TodoFrontDeskGateway`,
configure the transfer destination, approved corpus, emergency language, and
Results receiver.

```bash
voicekit init ./front-desk \
  --name front-desk \
  --recipe front-desk \
  --runtime livekit \
  --channels phone \
  --phone-provider twilio \
  --models stt=deepgram/nova-3,llm=anthropic/claude-sonnet-5,tts=cartesia/sonic-3.5 \
  --no-draft-prompts \
  --yes
```

Next: run `voicekit test`, then certify the conference-bridge checklist in
`docs/GAPS.md` on a funded Twilio account.
