import tempfile
import unittest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_runtime.context_compression.north_gateway.context_compression_rest_adapter import (
    create_context_compression_router,
)
from agent_runtime.context_compression.settings_repository import (
    ContextCompressionSettingsRepository,
)


class ContextCompressionRestAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._settings_repo = ContextCompressionSettingsRepository(
            data_dir_accessor=lambda: self._temp_dir.name
        )
        app = FastAPI()
        router = create_context_compression_router(
            settings_repository=self._settings_repo,
        )
        app.include_router(router, prefix="/api/context-compression")
        self._client = TestClient(app)

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def test_get_and_update_settings(self) -> None:
        # 1. 初始获取设置
        resp = self._client.get("/api/context-compression/settings")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["enabled"])
        self.assertIsNone(data["llm_policy_group_id"])

        # 2. 更新设置（绑定策略 6.1sol）
        update_resp = self._client.put(
            "/api/context-compression/settings",
            json={"llm_policy_group_id": "6.1sol"},
        )
        self.assertEqual(update_resp.status_code, 200)
        update_data = update_resp.json()
        self.assertTrue(update_data["enabled"])
        self.assertEqual(update_data["llm_policy_group_id"], "6.1sol")
        self.assertIsNotNone(update_data["updated_at"])

        # 3. 再次获取确认已持久化
        resp2 = self._client.get("/api/context-compression/settings")
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.json()["llm_policy_group_id"], "6.1sol")

        # 4. 置空策略（使用系统默认）
        empty_resp = self._client.put(
            "/api/context-compression/settings",
            json={"llm_policy_group_id": None},
        )
        self.assertEqual(empty_resp.status_code, 200)
        self.assertIsNone(empty_resp.json()["llm_policy_group_id"])

    def test_get_observer_prompt(self) -> None:
        resp = self._client.get("/api/context-compression/observer-prompt")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("prompt_text", data)
        self.assertTrue(len(data["prompt_text"]) > 0)
