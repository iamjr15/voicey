# Restaurant-reservations recipe

`restaurant-reservations@1.0.0` collects party size, date/time, timezone,
contact details, and special requests. It searches before offering at most
three slots, requires final confirmation, and offers an explicitly
non-guaranteed waitlist when no table is available.

The Pipecat variant is a native `NodeConfig`; the LiveKit variant uses a native
waitlist-specialist handoff. Replace `TodoRestaurantGateway`, configure business
hours and seating/allergy policy, and configure the Results receiver.

## Demo audio

<audio controls src="../assets/recipes/restaurant-reservations-demo.mp3">
  <a href="../assets/recipes/restaurant-reservations-demo.mp3">Download the restaurant-reservations demo</a>.
</audio>

The checked-in illustrative exchange demonstrates bounded alternative slots
and explicit final selection. Its source is
[demo-transcripts.json](../assets/recipes/demo-transcripts.json); generated
system speech is not provider or latency evidence.

## Create the project

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

## Production customization map

| File / setting | Owner action |
|---|---|
| `tools.py:TodoRestaurantGateway` | Replace search/reserve/waitlist stubs with authenticated, idempotent reservation APIs. |
| `prompts/system.md` | Add business hours, timezone, seating limits, and reviewed allergy language. |
| `prompts/failure.md` | Define outage and callback behavior without promising a table. |
| `prompts/voicemail.md` | Keep the message brief and avoid reservation/contact detail. |
| `agent.py:Results` | Configure the verified receiver and field-level contact redaction. |
| `testing.jsonc` | Add profiles for peak hours, accents, corrections, and unavailable inventory. |

## Verification

`voicekit test` covers search-before-offer, at most three alternatives,
non-guaranteed waitlist language, correction, confirmation, mutation failure,
and both native runtime compilers.

Next: run `voicekit test`, then repeat with `--runtime livekit`.
