# -*- coding: utf-8 -*-
from datetime import datetime, timezone
from pathlib import Path

from agent_runtime.agent.agent_instance import (
    AGENT_INSTANCE_STATUS_READY,
    AGENT_INSTANCE_STATUS_RUNNING,
    EXECUTION_SUBSTATE_NOTE_UPDATE,
    AgentInstance,
)
from agent_runtime.agent.infrastructure.sqlite_agent_runtime_repository import (
    SqliteAgentRuntimeRepository,
)


def test_agent_instance_note_substate_transitions() -> None:
    now = datetime.now(timezone.utc)
    instance = AgentInstance(
        instance_id="inst-001",
        task_id="task-001",
        thread_id="thread-001",
        current_working_directory="/test/workspace",
        agent_definition_id="def-001",
        llm_provider_id="provider-openai",
        llm_model_binding_id="gpt-4o",
        conversation_id="conv-001",
        trace_id="trace-001",
        status=AGENT_INSTANCE_STATUS_RUNNING,
        created_at=now,
        updated_at=now,
    )
    assert instance.execution_substate is None

    # 进入 note_update
    in_note = instance.enter_note_update()
    assert in_note.execution_substate == EXECUTION_SUBSTATE_NOTE_UPDATE
    assert in_note.updated_at >= now

    # 退出 note_update
    exited = in_note.exit_note_update()
    assert exited.execution_substate is None
    assert exited.updated_at >= in_note.updated_at


def test_sqlite_agent_instance_execution_substate_persistence(tmp_path: Path) -> None:
    repo = SqliteAgentRuntimeRepository(tmp_path)
    now = datetime.now(timezone.utc)
    instance = AgentInstance(
        instance_id="inst-persist-001",
        task_id="task-persist-001",
        thread_id="thread-persist-001",
        current_working_directory="/test/workspace",
        agent_definition_id="def-001",
        llm_provider_id="provider-openai",
        llm_model_binding_id="gpt-4o",
        conversation_id="conv-001",
        trace_id="trace-001",
        status=AGENT_INSTANCE_STATUS_RUNNING,
        execution_substate=EXECUTION_SUBSTATE_NOTE_UPDATE,
        created_at=now,
        updated_at=now,
    )
    repo.save_agent_instance(instance)

    loaded = repo.get_agent_instance("inst-persist-001")
    assert loaded is not None
    assert loaded.execution_substate == EXECUTION_SUBSTATE_NOTE_UPDATE

    # 退出并保存
    exited = loaded.exit_note_update()
    repo.save_agent_instance(exited)

    reloaded = repo.get_agent_instance("inst-persist-001")
    assert reloaded is not None
    assert reloaded.execution_substate is None

    repo.close()
