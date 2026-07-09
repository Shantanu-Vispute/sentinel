import pytest

from digest.llm_client import GeminiClient, _GEMINI_CHAT_MODELS


class FakeAPIError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def not_found_error(model_name):
    return FakeAPIError(
        f"404 NOT_FOUND. {{'error': {{'code': 404, 'message': 'This model "
        f"models/{model_name} is no longer available. Please update your code "
        f"to use a newer model for the latest features and improvements.', "
        f"'status': 'NOT_FOUND'}}}}",
        status_code=404,
    )


def rate_limit_error():
    return FakeAPIError(
        "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': "
        "'You exceeded your current quota', 'status': 'RESOURCE_EXHAUSTED'}}",
        status_code=429,
    )


class FakeModels:
    def __init__(self, behavior):
        # behavior: dict[model_name] -> Exception | "ok"
        self.behavior = behavior
        self.calls = []

    def generate_content(self, model, contents, config):
        self.calls.append(model)
        outcome = self.behavior.get(model, "ok")
        if outcome == "ok":
            return FakeResponse()
        raise outcome


class FakeResponse:
    text = "hello"


class FakeClient:
    def __init__(self, behavior):
        self.models = FakeModels(behavior)


def make_client(behaviors: list[dict]) -> GeminiClient:
    """behaviors: one dict per fake API key, each mapping model_name -> outcome."""
    client = GeminiClient.__new__(GeminiClient)
    client.clients = [FakeClient(b) for b in behaviors]
    return client


# --- classifier unit tests ---

def test_is_model_unavailable_error_detects_not_found():
    assert GeminiClient._is_model_unavailable_error(not_found_error("gemini-3.1-flash-lite-preview")) is True


def test_is_model_unavailable_error_ignores_rate_limit():
    assert GeminiClient._is_model_unavailable_error(rate_limit_error()) is False


def test_is_rate_limit_error_detects_429():
    assert GeminiClient._is_rate_limit_error(rate_limit_error()) is True


def test_is_rate_limit_error_ignores_not_found():
    assert GeminiClient._is_rate_limit_error(not_found_error("gemini-3.1-flash-lite-preview")) is False


def test_is_rate_limit_error_ignores_unrelated_errors():
    exc = FakeAPIError("connection reset by peer")
    assert GeminiClient._is_rate_limit_error(exc) is False
    assert GeminiClient._is_model_unavailable_error(exc) is False


# --- chat() fallback rotation tests ---

def test_chat_skips_dead_model_without_retrying_all_keys():
    first_model = _GEMINI_CHAT_MODELS[0]
    second_model = _GEMINI_CHAT_MODELS[1]
    behaviors = [
        {first_model: not_found_error(first_model), second_model: "ok"},
        {first_model: not_found_error(first_model), second_model: "ok"},
    ]
    client = make_client(behaviors)
    response = client.chat(messages=[{"role": "user", "content": "hi"}])
    assert response.model == second_model
    # Each new model starts again at key 1: only the first key gets tried for
    # the second (working) model, since it succeeds immediately there.
    assert client.clients[0].models.calls == [first_model, second_model]
    # Neither key should have been retried against the dead first model twice.
    assert client.clients[1].models.calls == []


def test_chat_rotates_keys_on_rate_limit_before_moving_to_next_model():
    first_model = _GEMINI_CHAT_MODELS[0]
    behaviors = [
        {first_model: rate_limit_error()},
        {first_model: "ok"},
    ]
    client = make_client(behaviors)
    response = client.chat(messages=[{"role": "user", "content": "hi"}])
    assert response.model == first_model
    assert client.clients[0].models.calls == [first_model]
    assert client.clients[1].models.calls == [first_model]


def test_chat_raises_when_all_models_and_keys_exhausted():
    behaviors = [
        {m: rate_limit_error() for m in _GEMINI_CHAT_MODELS},
        {m: rate_limit_error() for m in _GEMINI_CHAT_MODELS},
    ]
    client = make_client(behaviors)
    with pytest.raises(RuntimeError, match="All Gemini chat models/keys were rate-limited"):
        client.chat(messages=[{"role": "user", "content": "hi"}])


def test_chat_reraises_unrelated_errors_immediately():
    first_model = _GEMINI_CHAT_MODELS[0]
    behaviors = [
        {first_model: FakeAPIError("totally unrelated failure")},
        {first_model: "ok"},
    ]
    client = make_client(behaviors)
    with pytest.raises(FakeAPIError, match="totally unrelated failure"):
        client.chat(messages=[{"role": "user", "content": "hi"}])
    # Never even tried the second key/model — unrelated errors should not be swallowed.
    assert client.clients[1].models.calls == []
