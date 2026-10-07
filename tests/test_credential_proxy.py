from __future__ import annotations

from aa_arena.benchmark.credential_proxy import Handler


def test_response_model_extraction_from_responses_sse() -> None:
    payload = (
        b'event: response.created\n'
        b'data: {"type":"response.created","response":{"id":"r","model":"test-model-b"}}\n\n'
        b'event: response.completed\n'
        b'data: {"type":"response.completed","response":{"model":"test-model-b"}}\n\n'
        b'data: [DONE]\n'
    )
    assert Handler._response_models(payload) == {"test-model-b"}
