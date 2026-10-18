"""Chat-model client (docs/PLAN.md §5, §8.8, ADR 0009).

Each task maps to a model through configuration (``MODEL_EASY`` / ``MODEL_HARD`` /
``MODEL_JUDGE``); no model id is hard-coded in callers. Every call is metered: token usage is
priced with ``PRICES`` and accumulated on the client. The SDK retries 429/5xx with backoff;
the circuit breaker and model fallback arrive in Phase 5.
"""

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import structlog
from openai import AsyncOpenAI
from openai.types.completion_usage import CompletionUsage

from app.config import Settings

log = structlog.get_logger()

Task = Literal["rewrite", "answer", "judge"]
Message = dict[str, str]


@dataclass(frozen=True)
class Price:
    """USD per 1M tokens."""

    input: float
    cached_input: float
    output: float


# Standard tier, short context; https://developers.openai.com/api/docs/pricing (2026-10-18).
# A model missing here still works; its cost is logged as unknown.
PRICES: dict[str, Price] = {
    "gpt-6-astra": Price(10.00, 1.00, 50.00),
    "gpt-6.1-sol": Price(2.00, 0.10, 10.00),
    "gpt-6-sol": Price(2.00, 0.20, 10.00),
    "gpt-6-luna": Price(0.10, 0.01, 0.50),
    "gpt-5.4-mini": Price(0.75, 0.075, 4.50),
    "gpt-5.4-nano": Price(0.20, 0.02, 1.25),
    "gpt-5-mini": Price(0.25, 0.025, 2.00),
    "gpt-5-nano": Price(0.05, 0.005, 0.40),
    "gpt-4.1-mini": Price(0.40, 0.10, 1.60),
    "gpt-4.1-nano": Price(0.10, 0.025, 0.40),
}


@dataclass(frozen=True)
class Usage:
    task: Task
    model: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int

    @property
    def cost_usd(self) -> float | None:
        price = PRICES.get(self.model)
        if price is None:
            return None
        uncached = self.input_tokens - self.cached_input_tokens
        return (
            uncached * price.input
            + self.cached_input_tokens * price.cached_input
            + self.output_tokens * price.output
        ) / 1_000_000


class ChatModel(Protocol):
    """What the orchestrator needs from an LLM; tests substitute a fake."""

    def model_for(self, task: Task) -> str: ...

    async def complete(
        self, task: Task, messages: list[Message], *, json_schema: dict[str, Any] | None = None
    ) -> str: ...

    def stream(self, task: Task, messages: list[Message]) -> AsyncIterator[str]: ...


@dataclass
class TaskConfig:
    model: str
    max_output_tokens: int
    reasoning_effort: str | None = None  # only for reasoning models; None = model default


@dataclass
class LlmClient:
    client: AsyncOpenAI
    tasks: Mapping[Task, TaskConfig]
    usage: list[Usage] = field(default_factory=list)

    def model_for(self, task: Task) -> str:
        return self._config(task).model

    def metered(self) -> "LlmClient":
        """Same connection and models, its own usage list: the cost of one chat turn."""
        return LlmClient(self.client, self.tasks)

    @property
    def total_cost_usd(self) -> float:
        return sum(u.cost_usd or 0.0 for u in self.usage)

    async def complete(
        self, task: Task, messages: list[Message], *, json_schema: dict[str, Any] | None = None
    ) -> str:
        """One-shot completion; with ``json_schema`` the reply is strict JSON for it."""
        params = self._params(task)
        if json_schema is not None:
            params["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": f"{task}_output", "schema": json_schema, "strict": True},
            }
        response = await self.client.chat.completions.create(
            messages=messages,  # type: ignore[arg-type]
            **params,
        )
        self._record(task, response.model, response.usage)
        return response.choices[0].message.content or ""

    async def stream(self, task: Task, messages: list[Message]) -> AsyncIterator[str]:
        """Yield text deltas; usage is recorded when the stream ends."""
        params = self._params(task)
        response = await self.client.chat.completions.create(  # type: ignore[call-overload]
            messages=messages,
            stream=True,
            stream_options={"include_usage": True},
            **params,
        )
        async for chunk in response:
            if chunk.usage is not None:
                self._record(task, chunk.model, chunk.usage)
            for choice in chunk.choices:
                if choice.delta.content:
                    yield choice.delta.content

    def _config(self, task: Task) -> TaskConfig:
        if task not in self.tasks:
            raise RuntimeError(f"No model configured for task {task!r} (see .env.example)")
        return self.tasks[task]

    def _params(self, task: Task) -> dict[str, Any]:
        config = self._config(task)
        params: dict[str, Any] = {
            "model": config.model,
            "max_completion_tokens": config.max_output_tokens,
        }
        if config.reasoning_effort:
            params["reasoning_effort"] = config.reasoning_effort
        return params

    def _record(self, task: Task, model: str, usage: CompletionUsage | None) -> None:
        if usage is None:
            return
        details = usage.prompt_tokens_details
        entry = Usage(
            task=task,
            # The response names the dated snapshot; price by the configured alias.
            model=self.model_for(task) if model.startswith(self.model_for(task)) else model,
            input_tokens=usage.prompt_tokens,
            cached_input_tokens=(details.cached_tokens or 0) if details else 0,
            output_tokens=usage.completion_tokens,
        )
        self.usage.append(entry)
        log.info(
            "llm.usage",
            task=task,
            model=entry.model,
            input_tokens=entry.input_tokens,
            output_tokens=entry.output_tokens,
            cost_usd=entry.cost_usd,
        )


def make_llm(settings: Settings) -> LlmClient:
    if settings.openai_api_key is None:
        raise RuntimeError("OPENAI_API_KEY is required for chat models")
    client = AsyncOpenAI(
        api_key=settings.openai_api_key.get_secret_value(), max_retries=2, timeout=60
    )
    tasks: dict[Task, TaskConfig] = {}
    if settings.model_easy:
        tasks["rewrite"] = TaskConfig(settings.model_easy, 300, settings.model_easy_reasoning)
    if settings.model_hard:
        tasks["answer"] = TaskConfig(settings.model_hard, 1500, settings.model_hard_reasoning)
    if settings.model_judge:
        tasks["judge"] = TaskConfig(settings.model_judge, 800, settings.model_judge_reasoning)
    return LlmClient(client, tasks)
