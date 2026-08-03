# Recipe quality checklist

Every first-party recipe, and every community recipe seeking certification,
must satisfy this checklist on both native runtimes.

## Structure and ownership

- `recipe.jsonc` identifies a version, minimum engine, both runtime variants,
  and every integration point.
- `pipecat/flow.py` is native `pipecat.flows` code; `livekit/flow.py` is a
  native LiveKit `Agent` workflow. A shared scenario schema is test input, not
  a conversation DSL.
- Shared business operations are ordinary typed functions in `tools.py`, behind
  an explicit Protocol and a TODO-marked deterministic stub.
- Prompts separate greeting, system policy, safe failure, and voicemail policy.
- The README and docs page identify every production customization.

## Conversation safety

- A mutation needs explicit confirmation and never becomes spoken success
  before its tool succeeds.
- The newest correction supersedes stale caller input.
- Failure language reveals no internal or provider detail and offers a safe
  retry or escalation.
- Voicemail is brief and contains no workflow PII.
- The recipe defines escalation, out-of-scope, and dangerous-request behavior.
- Contact retention and consent are explicit wherever personal data is stored.

## Verification

- At least one happy-path result assertion, one correction or consent case, one
  integration failure/out-of-scope case, and one voicemail/privacy case compile
  through both runtime-native test adapters.
- Native flow entrypoints import and return their pinned framework types.
- Every LiveKit handoff preserves engine-supplied tools and chat context.
- Pipecat text and audio execution use the production eval bot; LiveKit text
  and audio use its production session.
- The built wheel contains shared files and each selected runtime variant.
- Reference-stack latency stays within the product p50/p95 gate; credentialed
  execution remains pending-live until it actually runs.

Community recipes use the `community/` namespace and the same CI. They are not
described as certified until a maintainer completes this checklist and the live
provider suite.

Next: run `voicey test`, `voicey test --audio`, and then the recipe's
credentialed commands from `docs/GAPS.md`.
