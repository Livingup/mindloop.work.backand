# -*- coding: utf-8 -*-
from __future__ import annotations

from agent_runtime.system_settings.persistence_inventory import (
    MACHINE_LOCAL_CONFIG,
    PROJECT_WORKSPACE_DATA,
    RUNTIME_HISTORY_DIAGNOSTIC_STATE,
    USER_GLOBAL_REUSABLE_CONFIG,
    build_persistence_inventory,
 )


def test_persistence_inventory_classifies_user_global_and_local_state() -> None:
    inventory = build_persistence_inventory(
        config_path="C:/Users/alice/.spread-ai/agent_runtime.yaml",
        raw_config={
            "llm_model_registry": {"registry_dir": "C:/Users/alice/.spread-ai/llm-model-registry"},
            "agent_definition": {"definitions_dir": "C:/Users/alice/.spread-ai/agents"},
            "prompt_template": {"snippets_dir": "C:/Users/alice/.spread-ai/prompt-snippets"},
            "mcp_client": {"config_dir": "C:/Users/alice/.spread-ai/mcp-servers"},
            "scene": {
                "scenes_db_path": "C:/Users/alice/.spread-ai/scene/scene.db",
                "recent_workspaces_path": "C:/Users/alice/.spread-ai/recent-workspaces.json",
            },
        },
    )
    items = {item.item_id: item for item in inventory}

    assert items["llm_model_registry"].scope == USER_GLOBAL_REUSABLE_CONFIG
    assert items["llm_model_registry"].server_sync_candidate is True
    assert items["llm_model_registry"].storage_location.endswith("llm_model_registry.sqlite3")
    assert items["agent_definitions"].scope == USER_GLOBAL_REUSABLE_CONFIG
    assert items["prompt_snippets"].scope == USER_GLOBAL_REUSABLE_CONFIG
    assert items["mcp_service_definitions"].sync_recommendation == "field_level_review"

    assert items["runtime_config_yaml"].scope == MACHINE_LOCAL_CONFIG
    assert items["browser_runtime_storage"].scope == MACHINE_LOCAL_CONFIG
    assert items["scene_project_data"].scope == PROJECT_WORKSPACE_DATA
    assert items["conversation_history"].scope == RUNTIME_HISTORY_DIAGNOSTIC_STATE
    assert items["agent_runtime_state"].server_sync_candidate is False


def test_persistence_inventory_preserves_all_scope_buckets() -> None:
    scopes = {
        item.scope
        for item in build_persistence_inventory(
            config_path="/home/alice/.spread-ai/agent_runtime.yaml",
            raw_config={},
        )
    }

    assert scopes == {
        USER_GLOBAL_REUSABLE_CONFIG,
        MACHINE_LOCAL_CONFIG,
        PROJECT_WORKSPACE_DATA,
        RUNTIME_HISTORY_DIAGNOSTIC_STATE,
    }
