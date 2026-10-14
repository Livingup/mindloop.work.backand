import unittest
from unittest.mock import patch

from agent_runtime.scene.scene_session_registry import SceneSessionRegistry


class SceneSessionRegistryTests(unittest.TestCase):
    def test_last_client_leave_keeps_vacant_scene_until_expire_policy_allows(self) -> None:
        expired = []
        allow_expire = False

        def can_expire(session):
            return allow_expire

        registry = SceneSessionRegistry(
            on_expired=expired.append,
            can_expire=can_expire,
            stale_timeout=10,
            sweep_interval=1,
        )

        with patch("agent_runtime.scene.scene_session_registry.time.time", return_value=1000.0):
            client_session_id = registry.open_session(
                "scene-1",
                "D:/workspace",
                client_session_id="client-1",
                device_id="device-1",
                client_kind="desktop",
            )

        with patch("agent_runtime.scene.scene_session_registry.time.time", return_value=1001.0):
            registry.close_session("scene-1", client_session_id)
            diagnostics = registry.get_session_diagnostics("scene-1")

        self.assertEqual(diagnostics["active_client_count"], 0)
        self.assertEqual(diagnostics["vacant_since_at"], 1001.0)
        self.assertEqual(expired, [])
        self.assertEqual(len(registry.list_sessions()), 1)

        with patch("agent_runtime.scene.scene_session_registry.time.time", return_value=2000.0):
            registry._sweep_once()

        self.assertEqual(expired, [])
        self.assertEqual(len(registry.list_sessions()), 1)

        allow_expire = True
        with patch("agent_runtime.scene.scene_session_registry.time.time", return_value=2001.0):
            registry._sweep_once()

        self.assertEqual(len(expired), 1)
        self.assertEqual(expired[0].scene_id, "scene-1")
        self.assertEqual(registry.list_sessions(), [])

    def test_reattach_clears_vacant_state_before_policy_expires(self) -> None:
        expired = []
        registry = SceneSessionRegistry(
            on_expired=expired.append,
            can_expire=lambda session: True,
            stale_timeout=10,
            sweep_interval=1,
        )

        with patch("agent_runtime.scene.scene_session_registry.time.time", return_value=1000.0):
            registry.open_session("scene-1", "D:/workspace", client_session_id="client-1")
        with patch("agent_runtime.scene.scene_session_registry.time.time", return_value=1001.0):
            registry.close_session("scene-1", "client-1")
        with patch("agent_runtime.scene.scene_session_registry.time.time", return_value=1002.0):
            registry.open_session("scene-1", "D:/workspace", client_session_id="client-2")
            diagnostics = registry.get_session_diagnostics("scene-1")

        self.assertEqual(expired, [])
        self.assertTrue(diagnostics["is_open"])
        self.assertEqual(diagnostics["active_client_count"], 1)
        self.assertIsNone(diagnostics["vacant_since_at"])
        self.assertIsNone(diagnostics["vacant_age_seconds"])

    def test_stale_clients_mark_scene_vacant_before_expiring_scene(self) -> None:
        expired = []
        allow_expire = False

        def can_expire(session):
            return allow_expire

        registry = SceneSessionRegistry(
            on_expired=expired.append,
            can_expire=can_expire,
            stale_timeout=10,
            sweep_interval=1,
        )

        with patch("agent_runtime.scene.scene_session_registry.time.time", return_value=1000.0):
            registry.open_session("scene-1", "D:/workspace", client_session_id="client-1")

        with patch("agent_runtime.scene.scene_session_registry.time.time", return_value=1011.0):
            registry._sweep_once()
            diagnostics = registry.get_session_diagnostics("scene-1")

        self.assertEqual(expired, [])
        self.assertEqual(diagnostics["active_client_count"], 0)
        self.assertEqual(diagnostics["vacant_since_at"], 1011.0)

        allow_expire = True
        with patch("agent_runtime.scene.scene_session_registry.time.time", return_value=1012.0):
            registry._sweep_once()

        self.assertEqual(len(expired), 1)
        self.assertEqual(registry.list_sessions(), [])


if __name__ == "__main__":
    unittest.main()
