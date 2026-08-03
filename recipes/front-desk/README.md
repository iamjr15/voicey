# Front desk

A dual-runtime receptionist workflow for approved answers, urgency-aware
triage, durable messages, and warm transfer. Replace `TodoFrontDeskGateway`
with the organization's knowledge and ticket systems. Review the emergency
policy and approved answer corpus before accepting calls.

The recipe requests `warm_transfer_to_human`; LiveKit supplies its native
`WarmTransferTask`, while Pipecat uses voicey's Twilio conference bridge.
When warm transfer is unavailable, the agent offers a message rather than
pretending a person was reached.

Run `voicey doctor`, then `voicey dev --phone`. Next: request a department,
decline transfer, and verify the durable `front_desk_message_taken` outcome.
