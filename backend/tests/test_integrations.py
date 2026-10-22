import json
import unittest
from uuid import uuid4

import httpx

from beework.integrations.litellm import NO_MODELS_ALIAS, GatewayError, LiteLLMClient, model_alias
from beework.integrations.ragflow import KnowledgeEngineError, RAGFlowClient


class RecordingTransport(httpx.MockTransport):
    def __init__(self, responses: dict[tuple[str, str], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return responses.get(
                (request.method, request.url.path), httpx.Response(404, json={"detail": "not found"})
            )

        super().__init__(handler)


class LiteLLMClientTests(unittest.TestCase):
    def test_upsert_registers_catalog_entry_under_stable_alias(self) -> None:
        model_id = uuid4()
        transport = RecordingTransport({("POST", "/model/new"): httpx.Response(200, json={})})
        with LiteLLMClient("http://gateway", "sk-master", transport=transport) as client:
            client.upsert_model(
                model_id=model_id,
                provider="azure_openai",
                provider_model_id="gpt-4o-prod",
                endpoint_url="https://example.openai.azure.com",
                api_key="secret-key",
                api_version="2024-10-21",
                rpm_limit=60,
                model_type="chat",
            )
        delete_request, create_request = transport.requests
        self.assertEqual(delete_request.url.path, "/model/delete")
        body = json.loads(create_request.content)
        self.assertEqual(body["model_name"], model_alias(model_id))
        self.assertEqual(body["model_info"]["id"], str(model_id))
        self.assertEqual(body["litellm_params"]["model"], "azure/gpt-4o-prod")
        self.assertEqual(body["litellm_params"]["api_version"], "2024-10-21")
        self.assertEqual(body["litellm_params"]["rpm"], 60)
        self.assertEqual(create_request.headers["Authorization"], "Bearer sk-master")

    def test_team_without_models_gets_unmatchable_allowlist(self) -> None:
        transport = RecordingTransport(
            {("POST", "/team/new"): httpx.Response(200, json={"team_id": "team-1"})}
        )
        with LiteLLMClient("http://gateway", "sk-master", transport=transport) as client:
            team_id = client.ensure_team(team_id=None, alias="beework:t:production", models=[])
        self.assertEqual(team_id, "team-1")
        # An empty list would mean "all models" in LiteLLM.
        self.assertEqual(json.loads(transport.requests[0].content)["models"], [NO_MODELS_ALIAS])

    def test_gateway_errors_are_reported_without_leaking_keys(self) -> None:
        transport = RecordingTransport(
            {
                ("POST", "/chat/completions"): httpx.Response(
                    401, json={"error": {"message": "Invalid proxy server token passed"}}
                )
            }
        )
        with LiteLLMClient("http://gateway", "sk-master", transport=transport) as client:
            with self.assertRaises(GatewayError) as raised:
                client.chat_completion(
                    api_key="sk-tenant", model="bw-x", messages=[{"role": "user", "content": "hi"}]
                )
        self.assertIn("401", str(raised.exception))
        self.assertNotIn("sk-tenant", str(raised.exception))
        self.assertEqual(transport.requests[0].headers["Authorization"], "Bearer sk-tenant")


class RAGFlowClientTests(unittest.TestCase):
    def test_application_error_code_is_raised_even_with_http_200(self) -> None:
        transport = RecordingTransport(
            {("POST", "/api/v1/datasets"): httpx.Response(200, json={"code": 102, "message": "Duplicated name"})}
        )
        with RAGFlowClient("http://ragflow", "rag-key", transport=transport) as client:
            with self.assertRaises(KnowledgeEngineError):
                client.create_dataset(name="kb")

    def test_retrieval_maps_chunks_with_source_location(self) -> None:
        transport = RecordingTransport(
            {
                ("POST", "/api/v1/retrieval"): httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "chunks": [
                                {
                                    "id": "c1",
                                    "document_id": "d1",
                                    "document_keyword": "manual.pdf",
                                    "content": "退货需在 7 天内申请",
                                    "similarity": 0.82,
                                }
                            ]
                        },
                    },
                )
            }
        )
        with RAGFlowClient("http://ragflow", "rag-key", transport=transport) as client:
            chunks = client.retrieve(question="怎么退货", dataset_ids=["ds1"])
        self.assertEqual(chunks[0]["document_name"], "manual.pdf")
        self.assertEqual(chunks[0]["chunk_id"], "c1")
        body = json.loads(transport.requests[0].content)
        self.assertEqual(body["dataset_ids"], ["ds1"])
        self.assertEqual(transport.requests[0].headers["Authorization"], "Bearer rag-key")


if __name__ == "__main__":
    unittest.main()
