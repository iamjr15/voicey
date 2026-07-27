# Restaurant reservations

A dual-runtime conversation design for table availability, confirmed
reservations, and waitlist fallback. Pipecat receives native
`pipecat.flows.NodeConfig` source; LiveKit receives native `Agent` handoffs.

Before production, replace `TodoRestaurantGateway` with the restaurant's
booking system, author business hours and seating policy in `prompts/system.md`,
and configure the verified Results receiver. Keep mutations idempotent and
preserve the tool return shapes or update the scenario contract with them.

Run `voicekit doctor`, then `voicekit dev`. Next: try an unavailable time,
accept the waitlist, and confirm the durable `restaurant_waitlisted` outcome.
