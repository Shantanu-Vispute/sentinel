from dataclasses import dataclass

import ollama
from google import genai
from google.genai import types

from config import (
    EMBEDDING_MODEL,
    EMBEDDING_PROVIDER,
    GEMINI_API_KEYS,
    GEMINI_EMBEDDING_MODEL,
    LLM_PROVIDER,
    LLM_TIMEOUT_SECONDS,
    OLLAMA_HOST,
    OLLAMA_MODEL,
    TEMPERATURE,
)

# Ordered by free-tier headroom (RPM/RPD), best first, per AI Studio's quota
# dashboard. gemini-3.1-flash-lite has ~25x the daily budget of everything
# else, so it should absorb nearly all traffic; the rest are thin last resorts.
_GEMINI_CHAT_MODELS = (
    "gemini-3.1-flash-lite",  # RPM 15, RPD 500
    "gemini-2.5-flash-lite",  # RPM 10, RPD 20
    "gemini-2.5-flash",       # RPM 5,  RPD 20
    "gemini-3-flash",         # RPM 5,  RPD 20
    "gemini-3.5-flash",       # RPM 5,  RPD 20
)


@dataclass
class BackendCheck:
    label: str
    ok: bool
    detail: str = ""


@dataclass
class Message:
    content: str


@dataclass
class ChatResponse:
    message: Message
    model: str = ""
    provider: str = ""


@dataclass
class EmbedResponse:
    embeddings: list[list[float]]
    model: str = ""
    provider: str = ""


class OllamaClient:
    provider_name = "ollama"

    def __init__(self):
        self.ollama_client = ollama.Client(
            host=OLLAMA_HOST,
            timeout=LLM_TIMEOUT_SECONDS,
        )

    def chat(
            self,
            messages: list[dict],
            format_schema: dict | None = None,
            **kwargs) -> ChatResponse:
        params = {
            "model": kwargs.get("model", OLLAMA_MODEL),
            "messages": messages,
            "options": {
                "temperature": kwargs.get("temperature", TEMPERATURE),
            },
        }
        if format_schema:
            params["format"] = format_schema
        response = self.ollama_client.chat(**params)
        content = self._extract_chat_text(response)
        return ChatResponse(
            message=Message(content=content),
            model=params["model"],
            provider=self.provider_name,
        )

    def embed(self, input_text: str | list[str]) -> EmbedResponse:
        response = self.ollama_client.embed(
            model=EMBEDDING_MODEL,
            input=input_text,
        )
        embeddings = self._extract_ollama_embeddings(response)
        return EmbedResponse(
            embeddings=embeddings,
            model=EMBEDDING_MODEL,
            provider=self.provider_name,
        )

    def healthcheck(self) -> BackendCheck:
        try:
            self.ollama_client.list()
            return BackendCheck(
                label=f"Ollama @ {OLLAMA_HOST}",
                ok=True,
                detail=f"chat={OLLAMA_MODEL}, embed={EMBEDDING_MODEL}",
            )
        except Exception as exc:
            return BackendCheck(
                label=f"Ollama @ {OLLAMA_HOST}",
                ok=False,
                detail=str(exc),
            )

    @staticmethod
    def _extract_chat_text(response) -> str:
        if isinstance(response, dict):
            return (((response.get("message") or {}).get("content")) or "").strip()
        message = getattr(response, "message", None)
        return (getattr(message, "content", "") or "").strip()

    @staticmethod
    def _extract_ollama_embeddings(response) -> list[list[float]]:
        if isinstance(response, dict):
            return response.get("embeddings", []) or []
        return getattr(response, "embeddings", []) or []


class GeminiClient:
    provider_name = "gemini"

    def __init__(self):
        if not GEMINI_API_KEYS:
            raise ValueError(
                "GEMINI_API_KEY or GEMINI_API_KEYS is required when LLM_PROVIDER or "
                "EMBEDDING_PROVIDER is set to gemini."
            )
        self.clients = [
            genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(timeout=LLM_TIMEOUT_SECONDS * 1000),
            )
            for api_key in GEMINI_API_KEYS
        ]

    def chat(
            self,
            messages: list[dict],
            format_schema: dict | None = None,
            **kwargs) -> ChatResponse:
        override_model = kwargs.get("model")
        models = [override_model] if override_model else list(_GEMINI_CHAT_MODELS)
        temperature = kwargs.get("temperature", TEMPERATURE)
        system_instruction, contents = self._split_messages(messages)
        last_error = None

        for model_name in models:
            for key_index, client in enumerate(self.clients, 1):
                try:
                    response = client.models.generate_content(
                        model=model_name,
                        contents=contents,
                        config=self._build_generate_config(
                            model_name=model_name,
                            system_instruction=system_instruction,
                            temperature=temperature,
                            format_schema=format_schema,
                        ),
                    )
                    return ChatResponse(
                        message=Message(content=self._extract_text(response)),
                        model=model_name,
                        provider=self.provider_name,
                    )
                except Exception as exc:
                    if self._is_model_unavailable_error(exc):
                        last_error = exc
                        print(
                            f"      WARN: Gemini model {model_name} is unavailable "
                            f"(retired/renamed); trying next model"
                        )
                        if override_model:
                            raise
                        break
                    if not self._is_rate_limit_error(exc):
                        raise
                    last_error = exc
                    print(
                        f"      WARN: Gemini model {model_name} hit a rate limit "
                        f"on key {key_index}/{len(self.clients)}; trying next key/model"
                    )
                    if override_model and key_index == len(self.clients):
                        raise

        tried = ", ".join(models)
        raise RuntimeError(
            f"All Gemini chat models/keys were rate-limited: {tried}"
        ) from last_error

    def embed(self, input_text: str | list[str]) -> EmbedResponse:
        last_error = None
        for key_index, client in enumerate(self.clients, 1):
            try:
                response = client.models.embed_content(
                    model=GEMINI_EMBEDDING_MODEL,
                    contents=input_text,
                )
                embeddings = []
                if getattr(response, "embeddings", None):
                    embeddings = [item.values for item in response.embeddings]
                elif getattr(response, "embedding", None):
                    embeddings = [response.embedding.values]
                return EmbedResponse(
                    embeddings=embeddings,
                    model=GEMINI_EMBEDDING_MODEL,
                    provider=self.provider_name,
                )
            except Exception as exc:
                if not self._is_rate_limit_error(exc):
                    raise
                last_error = exc
                print(
                    f"      WARN: Gemini embedding hit a rate limit on key "
                    f"{key_index}/{len(self.clients)}; trying next key"
                )
        raise RuntimeError("All Gemini embedding keys were rate-limited") from last_error

    def healthcheck(self) -> BackendCheck:
        last_error = None
        ok_count = 0
        for client in self.clients:
            try:
                pager = client.models.list(
                    config=types.ListModelsConfig(page_size=1)
                )
                next(iter(pager), None)
                ok_count += 1
            except Exception as exc:
                last_error = exc
        if ok_count:
            return BackendCheck(
                label="Gemini API",
                ok=True,
                detail=(
                    f"keys={ok_count}/{len(self.clients)}, "
                    f"chat={', '.join(_GEMINI_CHAT_MODELS)}, "
                    f"embed={GEMINI_EMBEDDING_MODEL}"
                ),
            )
        try:
            raise last_error or RuntimeError("No Gemini API keys configured")
        except Exception as exc:
            return BackendCheck(
                label="Gemini API",
                ok=False,
                detail=str(exc),
            )

    @staticmethod
    def _split_messages(messages: list[dict]) -> tuple[str | None, list[types.Content]]:
        system_parts = []
        contents = []
        for message in messages:
            role = (message.get("role") or "user").strip().lower()
            content = message.get("content", "")
            if isinstance(content, list):
                text = "\n".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and part.get("text")
                )
            else:
                text = str(content)
            if role == "system":
                if text.strip():
                    system_parts.append(text.strip())
                continue
            gemini_role = "model" if role == "assistant" else "user"
            contents.append(
                types.Content(
                    role=gemini_role,
                    parts=[types.Part(text=text)],
                )
            )
        if not contents:
            contents.append(
                types.Content(role="user", parts=[types.Part(text="")])
            )
        system_instruction = "\n\n".join(system_parts).strip() or None
        return system_instruction, contents

    @staticmethod
    def _build_generate_config(
            model_name: str,
            system_instruction: str | None,
            temperature: float,
            format_schema: dict | None) -> types.GenerateContentConfig:
        config_kwargs = {
            "temperature": temperature,
            "thinking_config": GeminiClient._thinking_config_for_model(model_name),
        }
        if system_instruction:
            config_kwargs["system_instruction"] = system_instruction
        if format_schema:
            config_kwargs["response_mime_type"] = "application/json"
            config_kwargs["response_json_schema"] = format_schema
        return types.GenerateContentConfig(**config_kwargs)

    @staticmethod
    def _thinking_config_for_model(model_name: str) -> types.ThinkingConfig:
        if model_name in {
                "gemini-3.1-flash-lite-preview",
                "gemini-3-flash-preview"}:
            return types.ThinkingConfig(thinking_level="minimal")
        return types.ThinkingConfig(thinking_budget=0)

    @staticmethod
    def _extract_text(response) -> str:
        if getattr(response, "text", None):
            return response.text.strip()
        parts = []
        for candidate in getattr(response, "candidates", []) or []:
            content = getattr(candidate, "content", None)
            for part in getattr(content, "parts", []) or []:
                text = getattr(part, "text", None)
                if text:
                    parts.append(text)
        return "\n".join(parts).strip()

    @staticmethod
    def _is_rate_limit_error(exc: Exception) -> bool:
        status_code = getattr(exc, "status_code", None)
        if status_code == 429:
            return True
        message = str(exc).lower()
        rate_limit_markers = (
            "429",
            "rate limit",
            "quota",
            "resource_exhausted",
            "too many requests",
        )
        return any(marker in message for marker in rate_limit_markers)

    @staticmethod
    def _is_model_unavailable_error(exc: Exception) -> bool:
        """True if the model itself is gone (retired/renamed), not just rate-limited.

        Retrying other API keys against a dead model wastes calls for no benefit;
        the caller should skip straight to the next model instead.
        """
        status_code = getattr(exc, "status_code", None)
        if status_code == 404:
            return True
        message = str(exc).lower()
        markers = ("not_found", "no longer available", "is not found")
        return any(marker in message for marker in markers)


class LLMClient:
    def __init__(self):
        self._backends: dict[str, object] = {}
        self.chat_backend = self._get_backend(LLM_PROVIDER)
        self.embedding_backend = self._get_backend(EMBEDDING_PROVIDER)
        self.backend_name = (
            f"chat={self.chat_backend.provider_name}, "
            f"embed={self.embedding_backend.provider_name}"
        )
        self._print_config()

    def chat(
            self,
            messages: list[dict],
            format_schema: dict | None = None,
            **kwargs) -> ChatResponse:
        return self.chat_backend.chat(messages, format_schema, **kwargs)

    def embed(self, input_text: str | list[str]) -> EmbedResponse:
        return self.embedding_backend.embed(input_text)

    def healthcheck(self) -> list[BackendCheck]:
        results = []
        seen = set()
        for backend in (self.chat_backend, self.embedding_backend):
            name = backend.provider_name
            if name in seen:
                continue
            seen.add(name)
            results.append(backend.healthcheck())
        return results

    def _get_backend(self, provider: str):
        provider_name = provider.strip().lower()
        if provider_name not in self._backends:
            if provider_name == "gemini":
                self._backends[provider_name] = GeminiClient()
            elif provider_name == "ollama":
                self._backends[provider_name] = OllamaClient()
            else:
                raise ValueError(f"Unsupported provider: {provider}")
        return self._backends[provider_name]

    def _print_config(self):
        print("LLM CONFIG")
        print(f"   Chat Provider:   {self.chat_backend.provider_name}")
        if self.chat_backend.provider_name == "gemini":
            print(f"   Gemini Keys:     {len(GEMINI_API_KEYS)}")
            print(f"   Chat Models:     {' -> '.join(_GEMINI_CHAT_MODELS)}")
        else:
            print(f"   Chat Model:      {OLLAMA_MODEL}")
            print(f"   Ollama Host:     {OLLAMA_HOST}")
        print(f"   Embed Provider:  {self.embedding_backend.provider_name}")
        if self.embedding_backend.provider_name == "gemini":
            if self.chat_backend.provider_name != "gemini":
                print(f"   Gemini Keys:     {len(GEMINI_API_KEYS)}")
            print(f"   Embed Model:     {GEMINI_EMBEDDING_MODEL}")
        else:
            print(f"   Embed Model:     {EMBEDDING_MODEL}")
        print(f"   Timeout:         {LLM_TIMEOUT_SECONDS}s")


_client = None


def get_llm_client() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client


def chat(messages: list[dict], format_schema: dict | None = None, **kwargs):
    return get_llm_client().chat(messages, format_schema, **kwargs)


def embed(input_text: str | list[str]):
    return get_llm_client().embed(input_text)
