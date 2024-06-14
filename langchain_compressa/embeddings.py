from __future__ import annotations

import logging
import os
import warnings
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Literal,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

import tiktoken

from langchain_core.embeddings import Embeddings
from langchain_core.pydantic_v1 import (
    Extra,
    Field,
    root_validator,
)
from langchain_core.utils import (
    convert_to_secret_str,
    get_from_dict_or_env,
    get_pydantic_field_names,
)

logger = logging.getLogger(__name__)


def _process_batched_chunked_embeddings(
    num_texts: int,
    tokens: List[Union[List[int], str]],
    batched_embeddings: List[List[float]],
    indices: List[int],
    skip_empty: bool,
) -> List[Optional[List[float]]]:
    # for each text, this is the list of embeddings (list of list of floats)
    # corresponding to the chunks of the text
    results: List[List[List[float]]] = [[] for _ in range(num_texts)]

    # for each text, this is the token length of each chunk
    # for transformers tokenization, this is the string length
    # for tiktoken, this is the number of tokens
    num_tokens_in_batch: List[List[int]] = [[] for _ in range(num_texts)]

    for i in range(len(indices)):
        if skip_empty and len(batched_embeddings[i]) == 1:
            continue
        results[indices[i]].append(batched_embeddings[i])
        num_tokens_in_batch[indices[i]].append(len(tokens[i]))

    # for each text, this is the final embedding
    embeddings: List[Optional[List[float]]] = []
    for i in range(num_texts):
        # an embedding for each chunk
        _result: List[List[float]] = results[i]

        if len(_result) == 0:
            # this will be populated with the embedding of an empty string
            # in the sync or async code calling this
            embeddings.append(None)
            continue

        elif len(_result) == 1:
            # if only one embedding was produced, use it
            embeddings.append(_result[0])
            continue

        else:
            # else we need to weighted average
            # should be same as
            # average = np.average(_result, axis=0, weights=num_tokens_in_batch[i])
            total_weight = sum(num_tokens_in_batch[i])
            average = [
                sum(
                    val * weight
                    for val, weight in zip(embedding, num_tokens_in_batch[i])
                )
                / total_weight
                for embedding in zip(*_result)
            ]

            # should be same as
            # embeddings.append((average / np.linalg.norm(average)).tolist())
            magnitude = sum(val**2 for val in average) ** 0.5
            embeddings.append([val / magnitude for val in average])

    return embeddings


class CompressaEmbeddings(Embeddings):
    """CompressaEmbeddings embedding model.

    To use, you should have the
    environment variable ``COMPRESSA_API_KEY`` set with your API key or pass it
    as a named parameter to the constructor.

    Example:
        .. code-block:: python

            from langchain_compressa import CompressaEmbeddings

            model = CompressaEmbeddings()
    """

    model: str = "/app/resources/models/models/Salesforce_SFR-Embedding-Mistral"
    compressa_api_base: Optional[str] = Field(default="https://compressa-api.mil-team.ru/v1", alias="base_url")
    chunk_size: int = 1000
    """Maximum number of texts to embed in each batch"""

    client: Any = Field(default=None, exclude=True)  #: :meta private:
    async_client: Any = Field(default=None, exclude=True)  #: :meta private:

    tiktoken_enabled: bool = True
    tiktoken_model_name: Optional[str] = "Salesforce/SFR-Embedding-Mistral"
    show_progress_bar: bool = False
    """Whether to show a progress bar when embedding."""
    model_kwargs: Dict[str, Any] = {"encoding_format": float}
    """Holds any model parameters valid for `create` call not explicitly specified."""
    allowed_special: Union[Literal["all"], Set[str], None] = None
    disallowed_special: Union[Literal["all"], Set[str], Sequence[str], None] = None
    skip_empty: bool = False
    embedding_ctx_length: int = 8191
    """The maximum number of tokens to embed at once."""
    check_embedding_ctx_length: bool = True
    """Whether to check the token length of inputs and automatically split inputs 
        longer than embedding_ctx_length."""

    class Config:
        """Configuration for this pydantic object."""

        extra = Extra.forbid
        allow_population_by_field_name = True

    @root_validator(pre=True)
    def build_extra(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        """Build extra kwargs from additional params that were passed in."""
        all_required_field_names = get_pydantic_field_names(cls)
        extra = values.get("model_kwargs", {})
        for field_name in list(values):
            if field_name in extra:
                raise ValueError(f"Found {field_name} supplied twice.")
            if field_name not in all_required_field_names:
                warnings.warn(
                    f"""WARNING! {field_name} is not default parameter.
                    {field_name} was transferred to model_kwargs.
                    Please confirm that {field_name} is what you intended."""
                )
                extra[field_name] = values.pop(field_name)

        invalid_model_kwargs = all_required_field_names.intersection(extra.keys())
        if invalid_model_kwargs:
            raise ValueError(
                f"Parameters {invalid_model_kwargs} should be specified explicitly. "
                f"Instead they were passed in as part of `model_kwargs` parameter."
            )

        values["model_kwargs"] = extra
        return values


    @root_validator()
    def validate_environment(cls, values: Dict) -> Dict:
        """Validate that api key and python package exists in environment."""
        compressa_api_key = get_from_dict_or_env(
            values, "compressa_api_key", "COMPRESSA_API_KEY"
        )
        values["compressa_api_key"] = (
            convert_to_secret_str(compressa_api_key) if compressa_api_key else None
        )
        values["compressa_api_base"] = values["compressa_api_base"] or os.getenv(
            "COMPRESSA_API_BASE"
        )
        return values
    
    @property
    def _invocation_params(self) -> Dict[str, Any]:
        params: Dict = {"model": self.model, **self.model_kwargs}
        return params

    def _tokenize(
        self, texts: List[str], chunk_size: int
    ) -> Tuple[Iterable[int], List[Union[List[int], str]], List[int]]:
        """
        Take the input `texts` and `chunk_size` and return 3 iterables as a tuple:

        We have `batches`, where batches are sets of individual texts
        we want responses from the compressa api. The length of a single batch is
        `chunk_size` texts.

        Each individual text is also split into multiple texts based on the
        `embedding_ctx_length` parameter (based on number of tokens).

        This function returns a 3-tuple of the following:

        _iter: An iterable of the starting index in `tokens` for each *batch*
        tokens: A list of tokenized texts, where each text has already been split
            into sub-texts based on the `embedding_ctx_length` parameter. In the
            case of tiktoken, this is a list of token arrays. In the case of
            HuggingFace transformers, this is a list of strings.
        indices: An iterable of the same length as `tokens` that maps each token-array
            to the index of the original text in `texts`.
        """
        tokens: List[Union[List[int], str]] = []
        indices: List[int] = []
        model_name = self.tiktoken_model_name or self.model

        # If tiktoken flag set to False
        if not self.tiktoken_enabled:
            try:
                from transformers import AutoTokenizer
            except ImportError:
                raise ValueError(
                    "Could not import transformers python package. "
                    "This is needed for CompressaEmbeddings to work without "
                    "`tiktoken`. Please install it with `pip install transformers`. "
                )

            tokenizer = AutoTokenizer.from_pretrained(
                pretrained_model_name_or_path=model_name
            )
            for i, text in enumerate(texts):
                # Tokenize the text using HuggingFace transformers
                tokenized: List[int] = tokenizer.encode(text, add_special_tokens=False)

                # Split tokens into chunks respecting the embedding_ctx_length
                for j in range(0, len(tokenized), self.embedding_ctx_length):
                    token_chunk: List[int] = tokenized[
                        j : j + self.embedding_ctx_length
                    ]

                    # Convert token IDs back to a string
                    chunk_text: str = tokenizer.decode(token_chunk)
                    tokens.append(chunk_text)
                    indices.append(i)
        else:
            try:
                encoding = tiktoken.encoding_for_model(model_name)
            except KeyError:
                encoding = tiktoken.get_encoding("Salesforce/SFR-Embedding-Mistral")
            encoder_kwargs: Dict[str, Any] = {
                k: v
                for k, v in {
                    "allowed_special": self.allowed_special,
                    "disallowed_special": self.disallowed_special,
                }.items()
                if v is not None
            }
            for i, text in enumerate(texts):

                if encoder_kwargs:
                    token = encoding.encode(text, **encoder_kwargs)
                else:
                    token = encoding.encode_ordinary(text)

                # Split tokens into chunks respecting the embedding_ctx_length
                for j in range(0, len(token), self.embedding_ctx_length):
                    tokens.append(token[j : j + self.embedding_ctx_length])
                    indices.append(i)

        if self.show_progress_bar:
            try:
                from tqdm.auto import tqdm

                _iter: Iterable = tqdm(range(0, len(tokens), chunk_size))
            except ImportError:
                _iter = range(0, len(tokens), chunk_size)
        else:
            _iter = range(0, len(tokens), chunk_size)
        return _iter, tokens, indices
    
    def _get_len_safe_embeddings(
        self, texts: List[str], *, chunk_size: Optional[int] = None
    ) -> List[List[float]]:
        """
        Generate length-safe embeddings for a list of texts.

        This method handles tokenization and embedding generation, respecting the
        set embedding context length and chunk size. It supports both tiktoken
        and HuggingFace tokenizer based on the tiktoken_enabled flag.

        Args:
            texts (List[str]): A list of texts to embed.
            engine (str): The engine or model to use for embeddings.
            chunk_size (Optional[int]): The size of chunks for processing embeddings.

        Returns:
            List[List[float]]: A list of embeddings for each input text.
        """
        _chunk_size = chunk_size or self.chunk_size
        _iter, tokens, indices = self._tokenize(texts, _chunk_size)
        batched_embeddings: List[List[float]] = []
        for i in _iter:
            response = self.client.create(
                input=tokens[i : i + _chunk_size], **self._invocation_params
            )
            if not isinstance(response, dict):
                response = response.model_dump()
            batched_embeddings.extend(r["embedding"] for r in response["data"])

        embeddings = _process_batched_chunked_embeddings(
            len(texts), tokens, batched_embeddings, indices, self.skip_empty
        )
        _cached_empty_embedding: Optional[List[float]] = None

        def empty_embedding() -> List[float]:
            nonlocal _cached_empty_embedding
            if _cached_empty_embedding is None:
                average_embedded = self.client.create(
                    input="", **self._invocation_params
                )
                if not isinstance(average_embedded, dict):
                    average_embedded = average_embedded.model_dump()
                _cached_empty_embedding = average_embedded["data"][0]["embedding"]
            return _cached_empty_embedding

        return [e if e is not None else empty_embedding() for e in embeddings]

    async def _aget_len_safe_embeddings(
        self, texts: List[str], *, chunk_size: Optional[int] = None
    ) -> List[List[float]]:
        """
        Asynchronously generate length-safe embeddings for a list of texts.

        This method handles tokenization and asynchronous embedding generation,
        respecting the set embedding context length and chunk size. It supports both
        `tiktoken` and HuggingFace `tokenizer` based on the tiktoken_enabled flag.

        Args:
            texts (List[str]): A list of texts to embed.
            chunk_size (Optional[int]): The size of chunks for processing embeddings.

        Returns:
            List[List[float]]: A list of embeddings for each input text.
        """

        _chunk_size = chunk_size or self.chunk_size
        _iter, tokens, indices = self._tokenize(texts, _chunk_size)
        batched_embeddings: List[List[float]] = []
        _chunk_size = chunk_size or self.chunk_size
        for i in range(0, len(tokens), _chunk_size):
            response = await self.async_client.create(
                input=tokens[i : i + _chunk_size], **self._invocation_params
            )

            if not isinstance(response, dict):
                response = response.model_dump()
            batched_embeddings.extend(r["embedding"] for r in response["data"])

        embeddings = _process_batched_chunked_embeddings(
            len(texts), tokens, batched_embeddings, indices, self.skip_empty
        )
        _cached_empty_embedding: Optional[List[float]] = None

        async def empty_embedding() -> List[float]:
            nonlocal _cached_empty_embedding
            if _cached_empty_embedding is None:
                average_embedded = await self.async_client.create(
                    input="", **self._invocation_params
                )
                if not isinstance(average_embedded, dict):
                    average_embedded = average_embedded.model_dump()
                _cached_empty_embedding = average_embedded["data"][0]["embedding"]
            return _cached_empty_embedding

        return [e if e is not None else await empty_embedding() for e in embeddings]

    def embed_documents(
        self, texts: List[str], chunk_size: Optional[int] = 0
    ) -> List[List[float]]:
        """Call out to Compressa's embedding endpoint for embedding search docs.

        Args:
            texts: The list of texts to embed.
            chunk_size: The chunk size of embeddings. If None, will use the chunk size
                specified by the class.

        Returns:
            List of embeddings, one for each text.
        """
        if not self.check_embedding_ctx_length:
            embeddings: List[List[float]] = []
            for text in texts:
                response = self.client.create(
                    input=text,
                    **self._invocation_params,
                )
                if not isinstance(response, dict):
                    response = response.dict()
                embeddings.extend(r["embedding"] for r in response["data"])
            return embeddings

        # NOTE: to keep things simple, we assume the list may contain texts longer
        #       than the maximum context and use length-safe embedding function.
        return self._get_len_safe_embeddings(texts)

    def embed_query(self, text: str) -> List[float]:
        """Call out to Compressa's embedding endpoint for embedding query text.

        Args:
            text: The text to embed.

        Returns:
            Embedding for the text.
        """
        return self.embed_documents([text])[0]

    async def aembed_documents(
        self, texts: List[str], chunk_size: Optional[int] = 0
    ) -> List[List[float]]:
        """Call out to Compressa's embedding endpoint async for embedding search docs.

        Args:
            texts: The list of texts to embed.
            chunk_size: The chunk size of embeddings. If None, will use the chunk size
                specified by the class.

        Returns:
            List of embeddings, one for each text.
        """
        if not self.check_embedding_ctx_length:
            embeddings: List[List[float]] = []
            for text in texts:
                response = await self.async_client.create(
                    input=text,
                    **self._invocation_params,
                )
                if not isinstance(response, dict):
                    response = response.dict()
                embeddings.extend(r["embedding"] for r in response["data"])
            return embeddings

        # NOTE: to keep things simple, we assume the list may contain texts longer
        #       than the maximum context and use length-safe embedding function.
        return await self._aget_len_safe_embeddings(texts)

    
    async def aembed_query(self, text: str) -> List[float]:
        """Call out to Compressa's embedding endpoint async for embedding query text.

        Args:
            text: The text to embed.

        Returns:
            Embedding for the text.
        """
        embeddings = await self.aembed_documents([text])
        return embeddings[0]
