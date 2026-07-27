# Restaurant-reservations recipe

`restaurant-reservations@1.0.0` collects party size, date/time, timezone,
contact details, and special requests. It searches before offering at most
three slots, requires final confirmation, and offers an explicitly
non-guaranteed waitlist when no table is available.

The Pipecat variant is a native `NodeConfig`; the LiveKit variant uses a native
waitlist-specialist handoff. Replace `TodoRestaurantGateway`, configure business
hours and seating/allergy policy, and configure the Results receiver.

```bash
voicekit init ./restaurant \
  --name restaurant \
  --recipe restaurant-reservations \
  --runtime pipecat \
  --channels web \
  --models stt=deepgram/nova-3,llm=anthropic/claude-sonnet-5,tts=cartesia/sonic-3.5 \
  --no-draft-prompts \
  --yes
```

Next: run `voicekit test`, then repeat with `--runtime livekit`.
