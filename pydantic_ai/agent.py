from __future__ import annotations as _annotations

import asyncio
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from typing import Any, Callable, Generic, Literal, cast, overload

from typing_extensions import assert_never

from . import _retriever as _r, _system_prompt, _utils, messages as _messages, models as _models, result as _result
from .call import AgentDeps
from .result import ResultData

__all__ = ('Agent',)
KnownModelName = Literal['openai:gpt-4o', 'openai:gpt-4-turbo', 'openai:gpt-4', 'openai:gpt-3.5-turbo']


@dataclass(init=False)
class Agent(Generic[AgentDeps, ResultData]):
    """Main class for creating "agents" - a way to have a specific type of "conversation" with an LLM."""

    # slots mostly for my sanity — knowing what attributes are available
    _model: _models.Model | None
    _result_tool: _result.ResultSchema[ResultData] | None
    _result_validators: list[_result.ResultValidator[AgentDeps, ResultData]]
    _allow_text_result: bool
    _system_prompts: tuple[str, ...]
    _retrievers: dict[str, _r.Retriever[AgentDeps, Any]]
    _default_retries: int
    _system_prompt_functions: list[_system_prompt.SystemPromptRunner[AgentDeps]]
    _default_deps: AgentDeps
    _max_result_retries: int
    _current_result_retry: int

    def __init__(
        self,
        model: _models.Model | KnownModelName | None = None,
        result_type: type[_result.ResultData] = str,
        *,
        system_prompt: str | Sequence[str] = (),
        # type here looks odd, but it's required os you can avoid "partially unknown" type errors with `deps=None`
        deps: AgentDeps | tuple[()] = (),
        retries: int = 1,
        result_tool_name: str = 'final_result',
        result_tool_description: str = 'The final response which ends this conversation',
        result_retries: int | None = None,
    ):
        self._model = _models.infer_model(model) if model is not None else None

        self._result_tool = _result.ResultSchema[result_type].build(
            result_type, result_tool_name, result_tool_description
        )
        # if the result tool is None, or its schema allows `str`, we allow plain text results
        self._allow_text_result = self._result_tool is None or self._result_tool.allow_text_result

        self._system_prompts = (system_prompt,) if isinstance(system_prompt, str) else tuple(system_prompt)
        self._retrievers: dict[str, _r.Retriever[AgentDeps, Any]] = {}
        self._default_deps = cast(AgentDeps, None if deps == () else deps)
        self._default_retries = retries
        self._system_prompt_functions = []
        self._max_result_retries = result_retries if result_retries is not None else retries
        self._current_result_retry = 0
        self._result_validators = []

    async def run(
        self,
        user_prompt: str,
        *,
        message_history: list[_messages.Message] | None = None,
        model: _models.Model | KnownModelName | None = None,
        deps: AgentDeps | None = None,
    ) -> _result.RunResult[_result.ResultData]:
        """Run the agent with a user prompt in async mode.

        Args:
            user_prompt: User input to start/continue the conversation.
            message_history: History of the conversation so far.
            model: Optional model to use for this run, required if `model` was not set when creating the agent.
            deps: Optional dependencies to use for this run.

        Returns:
            The result of the run.
        """
        if model is not None:
            model_ = _models.infer_model(model)
        elif self._model is not None:
            model_ = self._model
        else:
            raise RuntimeError('`model` must be set either when creating the agent or when calling it.')

        if deps is None:
            deps = self._default_deps

        if message_history is not None:
            # shallow copy messages
            messages = message_history.copy()
        else:
            messages = await self._init_messages(deps)

        messages.append(_messages.UserPrompt(user_prompt))

        functions: list[_models.AbstractToolDefinition] = list(self._retrievers.values())
        if self._result_tool is not None:
            functions.append(self._result_tool)

        result_tool_name = self._result_tool and self._result_tool.name
        agent_model = model_.agent_model(self._allow_text_result, functions, result_tool_name)

        for retriever in self._retrievers.values():
            retriever.reset()

        while True:
            llm_message = await agent_model.request(messages)
            opt_result = await self._handle_model_response(messages, llm_message, deps)
            if opt_result is not None:
                return _result.RunResult(opt_result.value, messages, cost=_result.Cost(0))

    def run_sync(
        self,
        user_prompt: str,
        *,
        message_history: list[_messages.Message] | None = None,
        model: _models.Model | KnownModelName | None = None,
        deps: AgentDeps | None = None,
    ) -> _result.RunResult[_result.ResultData]:
        """Run the agent with a user prompt synchronously.

        This is a convenience method that wraps `self.run` with `asyncio.run()`.

        Args:
            user_prompt: User input to start/continue the conversation.
            message_history: History of the conversation so far.
            model: Optional model to use for this run, required if `model` was not set when creating the agent.
            deps: Optional dependencies to use for this run.

        Returns:
            The result of the run.
        """
        return asyncio.run(self.run(user_prompt, message_history=message_history, model=model, deps=deps))

    def system_prompt(
        self, func: _system_prompt.SystemPromptFunc[AgentDeps]
    ) -> _system_prompt.SystemPromptFunc[AgentDeps]:
        """Decorator to register a system prompt function that takes `CallContext` as it's only argument."""
        self._system_prompt_functions.append(_system_prompt.SystemPromptRunner(func))
        return func

    def result_validator(
        self, func: _result.ResultValidatorFunc[AgentDeps, ResultData]
    ) -> _result.ResultValidatorFunc[AgentDeps, ResultData]:
        """Decorator to register a result validator function."""
        self._result_validators.append(_result.ResultValidator(func))
        return func

    @overload
    def retriever_context(self, func: _r.RetrieverContextFunc[AgentDeps, _r.P], /) -> _r.Retriever[AgentDeps, _r.P]: ...

    @overload
    def retriever_context(
        self, /, *, retries: int | None = None
    ) -> Callable[[_r.RetrieverContextFunc[AgentDeps, _r.P]], _r.Retriever[AgentDeps, _r.P]]: ...

    def retriever_context(
        self, func: _r.RetrieverContextFunc[AgentDeps, _r.P] | None = None, /, *, retries: int | None = None
    ) -> Any:
        """Decorator to register a retriever function."""
        if func is None:

            def retriever_decorator(
                func_: _r.RetrieverContextFunc[AgentDeps, _r.P],
            ) -> _r.Retriever[AgentDeps, _r.P]:
                # noinspection PyTypeChecker
                return self._register_retriever(_utils.Either(left=func_), retries)

            return retriever_decorator
        else:
            return self._register_retriever(_utils.Either(left=func), retries)

    @overload
    def retriever_plain(self, func: _r.RetrieverPlainFunc[_r.P], /) -> _r.Retriever[AgentDeps, _r.P]: ...

    @overload
    def retriever_plain(
        self, /, *, retries: int | None = None
    ) -> Callable[[_r.RetrieverPlainFunc[_r.P]], _r.Retriever[AgentDeps, _r.P]]: ...

    def retriever_plain(self, func: _r.RetrieverPlainFunc[_r.P] | None = None, /, *, retries: int | None = None) -> Any:
        """Decorator to register a retriever function."""
        if func is None:

            def retriever_decorator(func_: _r.RetrieverPlainFunc[_r.P]) -> _r.Retriever[AgentDeps, _r.P]:
                # noinspection PyTypeChecker
                return self._register_retriever(_utils.Either(right=func_), retries)

            return retriever_decorator
        else:
            return self._register_retriever(_utils.Either(right=func), retries)

    def _register_retriever(
        self, func: _r.RetrieverEitherFunc[AgentDeps, _r.P], retries: int | None
    ) -> _r.Retriever[AgentDeps, _r.P]:
        """Private utility to register a retriever function."""
        retries_ = retries if retries is not None else self._default_retries
        retriever = _r.Retriever[AgentDeps, _r.P](func, retries_)

        if self._result_tool and self._result_tool.name == retriever.name:
            raise ValueError(f'Retriever name conflicts with result schema name: {retriever.name!r}')

        if retriever.name in self._retrievers:
            raise ValueError(f'Retriever name conflicts with existing retriever: {retriever.name!r}')

        self._retrievers[retriever.name] = retriever
        return retriever

    async def _handle_model_response(
        self, messages: list[_messages.Message], llm_message: _messages.LLMMessage, deps: AgentDeps
    ) -> _utils.Option[ResultData]:
        """Process a single response from the model.

        Returns:
            Return `None` to continue the conversation, or a result to end it.
        """
        messages.append(llm_message)
        if llm_message.role == 'llm-response':
            # plain string response
            if self._allow_text_result:
                return _utils.Some(cast(ResultData, llm_message.content))
            else:
                messages.append(_messages.PlainResponseForbidden())
        elif llm_message.role == 'llm-tool-calls':
            if self._result_tool is not None:
                # if there's a result schema, and any of the calls match that name, return the result
                # NOTE: this means we ignore any other tools called here
                call = next((c for c in llm_message.calls if c.tool_name == self._result_tool.name), None)
                if call is not None:
                    try:
                        result = self._result_tool.validate(call)
                        result = await self._validate_result(result, deps, call)
                    except _result.ToolRetryError as e:
                        self._incr_result_retry()
                        messages.append(e.tool_retry)
                        return None
                    else:
                        return _utils.Some(result)

            # otherwise we run all retriever functions in parallel
            coros: list[Awaitable[_messages.Message]] = []
            for call in llm_message.calls:
                retriever = self._retrievers.get(call.tool_name)
                if retriever is None:
                    # TODO return message?
                    raise ValueError(f'Unknown function name: {call.tool_name!r}')
                coros.append(retriever.run(deps, call))
            messages += await asyncio.gather(*coros)
        else:
            assert_never(llm_message)

    async def _validate_result(self, result: ResultData, deps: AgentDeps, tool_call: _messages.ToolCall) -> ResultData:
        for validator in self._result_validators:
            result = await validator.validate(result, deps, self._current_result_retry, tool_call)
        return result

    def _incr_result_retry(self) -> None:
        self._current_result_retry += 1
        if self._current_result_retry > self._max_result_retries:
            raise RuntimeError(f'Exceeded maximum retries ({self._max_result_retries}) for result validation')

    async def _init_messages(self, deps: AgentDeps) -> list[_messages.Message]:
        """Build the initial messages for the conversation."""
        messages: list[_messages.Message] = [_messages.SystemPrompt(p) for p in self._system_prompts]
        for sys_prompt_runner in self._system_prompt_functions:
            prompt = await sys_prompt_runner.run(deps)
            messages.append(_messages.SystemPrompt(prompt))
        return messages
