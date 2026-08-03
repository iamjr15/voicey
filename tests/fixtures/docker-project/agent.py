"""Provider-lazy deployment fixture; health startup makes no model calls."""

from voicey import Agent, Limits, Models, Results, Web

agent = Agent(
    name="docker-fixture",
    runtime="pipecat",
    models=Models(
        stt="deepgram/nova-3",
        llm="anthropic/claude-sonnet-5",
        tts="cartesia/sonic-3.5",
    ),
    persona="Greet the caller.",
    flow="flow:entry",
    tools="tools",
    web=Web(enabled=True, allowed_origins=["https://app.example"]),
    results=Results(
        webhook="https://receiver.example/results",
        secret_env="VOICEY_WEBHOOK_SECRET",  # pragma: allowlist secret
    ),
    limits=Limits(max_duration_s=10, silence_hangup_s=5),
)
