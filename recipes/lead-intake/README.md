# Lead intake

A dual-runtime workflow for needs-based qualification, consented contact
capture, and follow-up scheduling. Replace `TodoLeadGateway` with the CRM and
calendar integration. Configure retention and consent language for the
operating jurisdiction before production.

Qualification uses only the business need, project timeline, budget range, and
organization size supplied for the inquiry. It must never infer protected
traits or use them in routing.

Run `voicekit doctor`, then `voicekit dev`. Next: complete an inquiry, schedule
a follow-up, and inspect the durable `lead_followup_scheduled` result.
