"""Call-result recording and Standard Webhooks primitives."""

from voicekit.results.recorder import (
    CallResultBuffer,
    result_context,
    set,
    set_outcome,
)
from voicekit.results.signing import (
    SignedWebhook,
    WebhookSigner,
    encode_secret,
)

__all__ = [
    "CallResultBuffer",
    "SignedWebhook",
    "WebhookSigner",
    "encode_secret",
    "result_context",
    "set",
    "set_outcome",
]
