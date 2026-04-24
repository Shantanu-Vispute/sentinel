import ollama

from config import (
    EMBEDDING_MODEL,
    LLM_TIMEOUT_SECONDS,
    OLLAMA_HOST,
    OLLAMA_MODEL,
    TEMPERATURE,
)

class LLMClient:
    def __init__(self):
        self.backend_name = "ollama"
        self.ollama_client = ollama.Client(
            host=OLLAMA_HOST,
            timeout=LLM_TIMEOUT_SECONDS,
        )
        print("LLM: OLLAMA")
        print(f"   Host:            {OLLAMA_HOST}")
        print(f"   Chat Model:      {OLLAMA_MODEL}")
        print(f"   Embedding Model: {EMBEDDING_MODEL}")
        print(f"   Timeout:         {LLM_TIMEOUT_SECONDS}s")

    def chat(
            self,
            messages: list[dict],
            format_schema: dict | None = None,
            **kwargs):
        params = {
            "model": kwargs.get("model", OLLAMA_MODEL),
            "messages": messages,
            "options": {
                "temperature": kwargs.get("temperature", TEMPERATURE),
            },
        }
        if format_schema:
            params["format"] = format_schema
        return self.ollama_client.chat(**params)

    def embed(self, input_text: str | list[str]):
        return self.ollama_client.embed(
            model=EMBEDDING_MODEL, input=input_text)

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
