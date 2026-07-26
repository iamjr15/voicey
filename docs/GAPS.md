# External verification gaps

This file tracks gates that are fully implemented but cannot be truthfully marked green without credentials, paid resources, physical hardware, cloud access, or wall-clock time.

| Gate | Status | Exact runbook command | Requirement |
|---|---|---|---|
| P0 physical-handset check | not-ready | Added with the P0 walking-skeleton runbook | Physical handset and a provisioned carrier number |
| P1 Twilio nightly certification | not-ready | Added with the P1 certification harness | Twilio live credentials, funded number, PSTN |
| P2 Twilio–LiveKit certification | not-ready | Added with the P2 certification harness | LiveKit project, Twilio Elastic SIP trunk, PSTN |
| P2 Telnyx certification, both paths | not-ready | Added with the P2 certification harness | Funded Telnyx and LiveKit accounts |
| P3 tier-3 PSTN loopback | not-ready | Added with the P3 live-test harness | Certified carrier accounts and PSTN |
| P3 cloud deploys | not-ready | Added with each P3 deploy target | Pipecat Cloud, LiveKit Cloud, and Fly access |
| P4 Railway deploy | not-ready | Added with the P4 Railway target | Railway access |
| P4 24-hour soak | not-ready | Added with the P4 soak harness | 24 hours of uninterrupted runner time |

Statuses change to `ready-to-run, pending credentials/time` only after the harness, configuration, and exact command exist. A row moves to the completion report as green only after the command actually passes.
