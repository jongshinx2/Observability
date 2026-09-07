from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LLMRequestOptions:
    max_tokens: int = 1024
    reasoning_mode: str = "omit"
    reasoning_effort: str = "none"
    enable_thinking: bool = False

    def __post_init__(self) -> None:
        if self.max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        if self.reasoning_mode not in {"omit", "effort", "chat_template"}:
            raise ValueError("reasoning_mode must be omit, effort, or chat_template")
        if self.reasoning_effort not in {"none", "low", "medium", "high"}:
            raise ValueError("reasoning_effort must be none, low, medium, or high")

    def as_kwargs(self) -> dict[str, Any]:
        options: dict[str, Any] = {"max_tokens": self.max_tokens}
        if self.reasoning_mode == "effort":
            options["reasoning_effort"] = self.reasoning_effort
        elif self.reasoning_mode == "chat_template":
            options["extra_body"] = {
                "chat_template_kwargs": {"enable_thinking": self.enable_thinking}
            }
        return options

    @property
    def requests_no_thinking(self) -> bool:
        return (
            self.reasoning_mode == "effort" and self.reasoning_effort == "none"
        ) or (
            self.reasoning_mode == "chat_template" and not self.enable_thinking
        )
