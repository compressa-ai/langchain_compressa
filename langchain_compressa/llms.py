"""Compressa large language models."""

from __future__ import annotations

import logging
import os
import sys
from typing import (
    AbstractSet,
    Any,
    AsyncIterator,
    Collection,
    Dict,
    Iterator,
    List,
    Literal,
    Mapping,
    Optional,
    Set,
    Tuple,
    Union,
)

import openai
import tiktoken

from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.llms import BaseLLM
from langchain_core.outputs import Generation, GenerationChunk, LLMResult
from langchain_core.pydantic_v1 import Field, SecretStr, root_validator
from langchain_core.utils import (
    convert_to_secret_str,
    get_from_dict_or_env,
    get_pydantic_field_names,
)
from langchain_core.utils.utils import build_extra_kwargs

logger = logging.getLogger(__name__)

def _update_token_usage(
    keys: Set[str], response: Dict[str, Any], token_usage: Dict[str, Any]
) -> None:
    """Update token usage."""
    _keys_to_use = keys.intersection(response["usage"])
    for _key in _keys_to_use:
        if _key not in token_usage:
            token_usage[_key] = response["usage"][_key]
        else:
            token_usage[_key] += response["usage"][_key]


def _stream_response_to_generation_chunk(
    stream_response: Dict[str, Any],
) -> GenerationChunk:
    """Convert a stream response to a generation chunk."""
    if not stream_response["choices"]:
        return GenerationChunk(text="")
    return GenerationChunk(
        text=stream_response["choices"][0]["text"],
        generation_info=dict(
            finish_reason=stream_response["choices"][0].get("finish_reason", None),
            logprobs=stream_response["choices"][0].get("logprobs", None),
        ),
    )


class BaseCompressaLLM(BaseLLM):
    """Base Compressa large language model class."""

    client: Any = Field(default=None, exclude=True)  #: :meta private:
    async_client: Any = Field(default=None, exclude=True)  #: :meta private:
    model_name: str = Field(default="/app/resources/models/models/compressa-ai_Llama-3-8B-Instruct", alias="model")
    """Model name to use."""
    temperature: float = 0.7
    """What sampling temperature to use."""
    max_tokens: int = 256
    """The maximum number of tokens to generate in the completion."""
    top_p: float = 1
    """Total probability mass of tokens to consider at each step."""
    frequency_penalty: float = 0
    """Penalizes repeated tokens according to frequency."""
    presence_penalty: float = 0
    """Penalizes repeated tokens."""
    n: int = 1
    """How many completions to generate for each prompt."""
    best_of: int = 1
    """Generates best_of completions server-side and returns the "best"."""
    model_kwargs: Dict[str, Any] = Field(default_factory=dict)
    """Holds any model parameters valid for `create` call not explicitly specified."""
    compressa_api_key: Optional[SecretStr] = Field(default=None, alias="api_key")
    """Automatically inferred from env var `COMPRESSA_API_KEY` if not provided."""
    compressa_api_base: Optional[str] = Field(default="https://compressa-api.mil-team.ru/v1", alias="base_url")
    """Base URL path for API requests"""
    batch_size: int = 20
    """Batch size to use when passing multiple documents to generate."""
    request_timeout: Union[float, Tuple[float, float], Any, None] = Field(
        default=None, alias="timeout"
    )
    """Timeout for requests to Compressa completion API. Can be float, httpx.Timeout or 
        None."""
    logit_bias: Optional[Dict[str, float]] = Field(default_factory=dict)
    """Adjust the probability of specific tokens being generated."""
    max_retries: int = 2
    """Maximum number of retries to make when generating."""
    streaming: bool = False
    """Whether to stream the results or not."""
    allowed_special: Union[Literal["all"], AbstractSet[str]] = set()
    """Set of special tokens that are allowed。"""
    disallowed_special: Union[Literal["all"], Collection[str]] = "all"
    """Set of special tokens that are not allowed。"""
    tiktoken_model_name: Optional[str] = "Salesforce/SFR-Embedding-Mistral"
    """The model name to pass to tiktoken when using this class."""
    default_headers: Union[Mapping[str, str], None] = None
    default_query: Union[Mapping[str, object], None] = None
    # Configure a custom httpx client. See the
    # [httpx documentation](https://www.python-httpx.org/api/#client) for more details.
    http_client: Union[Any, None] = None
    """Optional httpx.Client. Only used for sync invocations. Must specify 
        http_async_client as well if you'd like a custom client for async invocations.
    """
    http_async_client: Union[Any, None] = None
    """Optional httpx.AsyncClient. Only used for async invocations. Must specify 
        http_client as well if you'd like a custom client for sync invocations."""

    class Config:
        """Configuration for this pydantic object."""

        allow_population_by_field_name = True

    @root_validator(pre=True)
    def build_extra(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        """Build extra kwargs from additional params that were passed in."""
        all_required_field_names = get_pydantic_field_names(cls)
        extra = values.get("model_kwargs", {})
        values["model_kwargs"] = build_extra_kwargs(
            extra, values, all_required_field_names
        )
        return values

    @root_validator()
    def validate_environment(cls, values: Dict) -> Dict:
        """Validate that api key and python package exists in environment."""
        if values["n"] < 1:
            raise ValueError("n must be at least 1.")
        if values["streaming"] and values["n"] > 1:
            raise ValueError("Cannot stream results when n > 1.")
        if values["streaming"] and values["best_of"] > 1:
            raise ValueError("Cannot stream results when best_of > 1.")

        compressa_api_key = get_from_dict_or_env(
            values, "compressa_api_key", "COMPRESSA_API_KEY"
        )
        values["compressa_api_key"] = (
            convert_to_secret_str(compressa_api_key) if compressa_api_key else None
        )
        values["compressa_api_base"] = values["compressa_api_base"] or os.getenv(
            "COMPRESSA_API_BASE"
        )
        
        client_params = {
            "api_key": (
                values["compressa_api_key"].get_secret_value()
                if values["compressa_api_key"]
                else None
            ),
            "base_url": values["compressa_api_base"],
            "timeout": values["request_timeout"],
            "max_retries": values["max_retries"],
            "default_headers": values["default_headers"],
            "default_query": values["default_query"],
        }
        if not values.get("client"):
            sync_specific = {"http_client": values["http_client"]}
            values["client"] = openai.OpenAI(
                **client_params, **sync_specific
            ).completions
        if not values.get("async_client"):
            async_specific = {"http_client": values["http_async_client"]}
            values["async_client"] = openai.AsyncOpenAI(
                **client_params, **async_specific
            ).completions

        return values

    @property
    def _default_params(self) -> Dict[str, Any]:
        """Get the default parameters for calling Compressa API."""
        normal_params: Dict[str, Any] = {
            "temperature": self.temperature,
            "top_p": self.top_p,
            "frequency_penalty": self.frequency_penalty,
            "presence_penalty": self.presence_penalty,
            "n": self.n,
            "logit_bias": self.logit_bias,
        }

        if self.max_tokens is not None:
            normal_params["max_tokens"] = self.max_tokens

        normal_params["best_of"] = self.best_of

        return {**normal_params, **self.model_kwargs}

    def _stream(
        self,
        prompt: str,
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> Iterator[GenerationChunk]:
        params = {**self._invocation_params, **kwargs, "stream": True}
        self.get_sub_prompts(params, [prompt], stop)  # this mutates params
        for stream_resp in self.client.create(prompt=prompt, **params):
            if not isinstance(stream_resp, dict):
                stream_resp = stream_resp.model_dump()
            chunk = _stream_response_to_generation_chunk(stream_resp)

            if run_manager:
                run_manager.on_llm_new_token(
                    chunk.text,
                    chunk=chunk,
                    verbose=self.verbose,
                    logprobs=(
                        chunk.generation_info["logprobs"]
                        if chunk.generation_info
                        else None
                    ),
                )
            yield chunk

    async def _astream(
        self,
        prompt: str,
        stop: Optional[List[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> AsyncIterator[GenerationChunk]:
        params = {**self._invocation_params, **kwargs, "stream": True}
        self.get_sub_prompts(params, [prompt], stop)  # this mutates params
        async for stream_resp in await self.async_client.create(
            prompt=prompt, **params
        ):
            if not isinstance(stream_resp, dict):
                stream_resp = stream_resp.model_dump()
            chunk = _stream_response_to_generation_chunk(stream_resp)

            if run_manager:
                await run_manager.on_llm_new_token(
                    chunk.text,
                    chunk=chunk,
                    verbose=self.verbose,
                    logprobs=(
                        chunk.generation_info["logprobs"]
                        if chunk.generation_info
                        else None
                    ),
                )
            yield chunk

    @property
    def _llm_type(self) -> str:
        """Return type of LLM."""
        return "compressa-llm"

    def _generate(
        self,
        prompts: List[str],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> LLMResult:
        """Call out to Compressa's endpoint with k unique prompts.

        Args:
            prompts: The prompts to pass into the model.
            stop: Optional list of stop words to use when generating.

        Returns:
            The full LLM output.

        Example:
            .. code-block:: python

                response = compressa.generate(["Tell me a joke."])
        """
        # TODO: write a unit test for this
        params = self._invocation_params
        params = {**params, **kwargs}
        sub_prompts = self.get_sub_prompts(params, prompts, stop)
        choices = []
        token_usage: Dict[str, int] = {}
        # Get the token usage from the response.
        # Includes prompt, completion, and total tokens used.
        _keys = {"completion_tokens", "prompt_tokens", "total_tokens"}
        system_fingerprint: Optional[str] = None
        for _prompts in sub_prompts:
            if self.streaming:
                if len(_prompts) > 1:
                    raise ValueError("Cannot stream results with multiple prompts.")

                generation: Optional[GenerationChunk] = None
                for chunk in self._stream(_prompts[0], stop, run_manager, **kwargs):
                    if generation is None:
                        generation = chunk
                    else:
                        generation += chunk
                assert generation is not None
                choices.append(
                    {
                        "text": generation.text,
                        "finish_reason": (
                            generation.generation_info.get("finish_reason")
                            if generation.generation_info
                            else None
                        ),
                        "logprobs": (
                            generation.generation_info.get("logprobs")
                            if generation.generation_info
                            else None
                        ),
                    }
                )
            else:
                response = self.client.create(prompt=_prompts, **params)
                if not isinstance(response, dict):
                    # V1 client returns the response in an PyDantic object instead of
                    # dict. For the transition period, we deep convert it to dict.
                    response = response.model_dump()

                # Sometimes the AI Model calling will get error, we should raise it.
                # Otherwise, the next code 'choices.extend(response["choices"])'
                # will throw a "TypeError: 'NoneType' object is not iterable" error
                # to mask the true error. Because 'response["choices"]' is None.
                if response.get("error"):
                    raise ValueError(response.get("error"))

                choices.extend(response["choices"])
                _update_token_usage(_keys, response, token_usage)
                if not system_fingerprint:
                    system_fingerprint = response.get("system_fingerprint")
        return self.create_llm_result(
            choices,
            prompts,
            params,
            token_usage,
            system_fingerprint=system_fingerprint,
        )

    async def _agenerate(
        self,
        prompts: List[str],
        stop: Optional[List[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> LLMResult:
        """Call out to Compressa's endpoint async with k unique prompts."""
        params = self._invocation_params
        params = {**params, **kwargs}
        sub_prompts = self.get_sub_prompts(params, prompts, stop)
        choices = []
        token_usage: Dict[str, int] = {}
        # Get the token usage from the response.
        # Includes prompt, completion, and total tokens used.
        _keys = {"completion_tokens", "prompt_tokens", "total_tokens"}
        system_fingerprint: Optional[str] = None
        for _prompts in sub_prompts:
            if self.streaming:
                if len(_prompts) > 1:
                    raise ValueError("Cannot stream results with multiple prompts.")

                generation: Optional[GenerationChunk] = None
                async for chunk in self._astream(
                    _prompts[0], stop, run_manager, **kwargs
                ):
                    if generation is None:
                        generation = chunk
                    else:
                        generation += chunk
                assert generation is not None
                choices.append(
                    {
                        "text": generation.text,
                        "finish_reason": (
                            generation.generation_info.get("finish_reason")
                            if generation.generation_info
                            else None
                        ),
                        "logprobs": (
                            generation.generation_info.get("logprobs")
                            if generation.generation_info
                            else None
                        ),
                    }
                )
            else:
                response = await self.async_client.create(prompt=_prompts, **params)
                if not isinstance(response, dict):
                    response = response.model_dump()
                choices.extend(response["choices"])
                _update_token_usage(_keys, response, token_usage)
        return self.create_llm_result(
            choices,
            prompts,
            params,
            token_usage,
            system_fingerprint=system_fingerprint,
        )
    
    def get_sub_prompts(
        self,
        params: Dict[str, Any],
        prompts: List[str],
        stop: Optional[List[str]] = None,
    ) -> List[List[str]]:
        """Get the sub prompts for llm call."""
        if stop is not None:
            if "stop" in params:
                raise ValueError("`stop` found in both the input and default params.")
            params["stop"] = stop
        if params["max_tokens"] == -1:
            if len(prompts) != 1:
                raise ValueError(
                    "max_tokens set to -1 not supported for multiple inputs."
                )
            params["max_tokens"] = self.max_tokens_for_prompt(prompts[0])
        sub_prompts = [
            prompts[i : i + self.batch_size]
            for i in range(0, len(prompts), self.batch_size)
        ]
        return sub_prompts

    def create_llm_result(
        self,
        choices: Any,
        prompts: List[str],
        params: Dict[str, Any],
        token_usage: Dict[str, int],
        *,
        system_fingerprint: Optional[str] = None,
    ) -> LLMResult:
        """Create the LLMResult from the choices and prompts."""
        generations = []
        n = params.get("n", self.n)
        for i, _ in enumerate(prompts):
            sub_choices = choices[i * n : (i + 1) * n]
            generations.append(
                [
                    Generation(
                        text=choice["text"],
                        generation_info=dict(
                            finish_reason=choice.get("finish_reason"),
                            logprobs=choice.get("logprobs"),
                        ),
                    )
                    for choice in sub_choices
                ]
            )
        llm_output = {"token_usage": token_usage, "model_name": self.model_name}
        if system_fingerprint:
            llm_output["system_fingerprint"] = system_fingerprint
        return LLMResult(generations=generations, llm_output=llm_output)

    @property
    def _invocation_params(self) -> Dict[str, Any]:
        """Get the parameters used to invoke the model."""
        return self._default_params

    @property
    def _identifying_params(self) -> Mapping[str, Any]:
        """Get the identifying parameters."""
        return {**{"model_name": self.model_name}, **self._default_params}

    def get_token_ids(self, text: str) -> List[int]:
        """Get the token IDs using the tiktoken package."""
        if self.custom_get_token_ids is not None:
            return self.custom_get_token_ids(text)
        # tiktoken NOT supported for Python < 3.8
        if sys.version_info[1] < 8:
            return super().get_num_tokens(text)

        model_name = self.tiktoken_model_name or self.model_name
        try:
            enc = tiktoken.encoding_for_model(model_name)
        except KeyError:
            enc = tiktoken.get_encoding("Salesforce/SFR-Embedding-Mistral")

        return enc.encode(
            text,
            allowed_special=self.allowed_special,
            disallowed_special=self.disallowed_special,
        )
    

class CompressaLLM(BaseCompressaLLM):
    """Compressa large language models.

    To use, you should have the environment variable ``COMPRESSA_API_KEY``
    set with your API key, or pass it as a named parameter to the constructor.

    Any parameters that are valid to be passed to the create call can be passed
    in, even if not explicitly saved on this class.

    Example:
        .. code-block:: python

            from langchain_compressa import CompressaLLM

            model = CompressaLLM()
    """

    @classmethod
    def get_lc_namespace(cls) -> List[str]:
        """Get the namespace of the langchain object."""
        return ["langchain", "llms", "compressa"]

    @classmethod
    def is_lc_serializable(cls) -> bool:
        """Return whether this model can be serialized by Langchain."""
        return True

    @property
    def _invocation_params(self) -> Dict[str, Any]:
        return {**{"model": self.model_name}, **super()._invocation_params}

    @property
    def lc_secrets(self) -> Dict[str, str]:
        return {"compressa_api_key": "COMPRESSA_API_KEY"}

    @property
    def lc_attributes(self) -> Dict[str, Any]:
        attributes: Dict[str, Any] = {}
        if self.compressa_api_base:
            attributes["compressa_api_base"] = self.compressa_api_base

        return attributes

