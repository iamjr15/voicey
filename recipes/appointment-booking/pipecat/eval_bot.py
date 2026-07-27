"""Pipecat Evals entrypoint; run this file with ``-t eval``."""

from __future__ import annotations

from agent import agent
from pipecat.runner.types import EvalRunnerArguments, RunnerArguments

from voicekit import Phone
from voicekit.errors import VoicekitError
from voicekit.runtimes.pipecat import run_eval_agent

_EVAL_TRANSFER_NUMBER = "+15555550199"
eval_agent = agent.model_copy(
    update={
        "phone": agent.phone
        or Phone(
            provider="twilio",
            number="+15555550198",
            inbound=True,
            outbound=False,
        ),
        "behavior": agent.behavior.model_copy(update={"transfer_number": _EVAL_TRANSFER_NUMBER}),
    }
)


async def bot(runner_args: RunnerArguments) -> None:
    """Run the copied agent through Pipecat's native eval transport."""
    if not isinstance(runner_args, EvalRunnerArguments):
        raise VoicekitError("VK-RUN-001", detail="eval_bot.py must run with `-t eval`.")
    await run_eval_agent(eval_agent, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
