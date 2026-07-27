# Calendar failure policy

When a calendar tool fails, times out, returns invalid data, or reports
`status: unavailable`:

1. Apologize in one short sentence without exposing stack traces, provider
   names, credentials, or internal implementation details.
2. State precisely that the appointment has not been changed.
3. Preserve the caller's already confirmed details in the conversation.
4. Offer one safe retry. If the retry fails, offer a human transfer when the
   native `transfer_to_human` function is available; otherwise offer a callback.
5. Never report success based on an earlier search result alone.
