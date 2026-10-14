# 项目结构地图

## 域映射
```text
src/agent_runtime/auth/**                                      [auth]
src/agent_runtime/communication/**                             [communication]
src/agent_runtime/task_note/**                                 [task_note]
src/agent_runtime/context_compression/**                       [task_note]
src/agent_runtime/server.py                                    [公共]
src/agent_runtime/runtime_network_monitor.py                   [公共]
tests/test_runtime_auth_session_lifecycle.py                   [auth]
tests/test_runtime_auth_token_transport.py                     [auth]
tests/test_communication_sse_contract.py                       [communication]
tests/test_task_note_*.py                                      [task_note]
tests/test_context_compression_*.py                            [task_note]
tests/test_note_context_compression_flow.py                    [task_note]
tests/test_conversation_task_note_tracking.py                  [task_note]
tests/test_runtime_network_auth_failure_sampling.py            [公共]
```

## 节点标注
```text
src/agent_runtime/auth/auth_service.py                         AUTH-01 AUTH-02 AUTH-03
src/agent_runtime/server.py                                    AUTH-04 [公共·认证、初始化与网络诊断组合根；task_note·启动时执行 ensure_task_note_schema]
src/agent_runtime/communication/north_gateway/communication_rest_adapter.py COMMUNICATION-01
src/agent_runtime/runtime_network_monitor.py                   [公共·认证失败诊断采样]
src/agent_runtime/task_note/task_note_tracking_coordinator.py  TASK_NOTE-16 TASK_NOTE-17 TASK_NOTE-18 [task_note·tracking 唯一推进点 U1/U2/U3]
src/agent_runtime/task_note/task_note_tool.py                  TASK_NOTE-07 TASK_NOTE-08 TASK_NOTE-15 [task_note·note_update_end]
src/agent_runtime/task_note/task_note_store.py                 TASK_NOTE-04 TASK_NOTE-10 TASK_NOTE-11 TASK_NOTE-12 TASK_NOTE-19
src/agent_runtime/task_note/task_note_snapshot_service.py      TASK_NOTE-03 TASK_NOTE-13
src/agent_runtime/task_note/task_note_event_recorder.py        TASK_NOTE-02
src/agent_runtime/task_note/task_note_observer_runner.py       TASK_NOTE-14 [task_note·观察会话循环]
src/agent_runtime/task_note/task_note_observer_binding.py      TASK_NOTE-08 [task_note·目标实例与 run_id 注入]
src/agent_runtime/task_note/task_note_observer_scheduler.py    [task_note·观察启动与 units 序列化]
src/agent_runtime/task_note/task_note_context_assembler.py     [task_note·替换内容组装]
src/agent_runtime/task_note/task_note_unit_slicer.py           [task_note·增量切片]
src/agent_runtime/context_compression/gate.py                  [task_note·压缩门禁]
src/agent_runtime/conversation_message/infrastructure/sqlite_conversation_repository.py [task_note·tracking CAS，save 不写 tracking]
src/agent_runtime/agent/agent_execution_engine.py              TASK_NOTE-01 TASK_NOTE-18 [task_note·观察与替换调用时机]
src/agent_runtime/subagent/subagent_runtime.py                 [task_note·fork_message_id 初始化子会话 tracking]
src/agent_runtime/context_compression/archive_repository.py    [task_note·遗留待清理]
src/agent_runtime/context_compression/job_store.py             [task_note·遗留待清理]
src/agent_runtime/context_compression/conversation_rewriter.py [task_note·遗留待清理]
src/agent_runtime/context_compression/event_publisher.py       [task_note·遗留待清理]
src/agent_runtime/context_compression/range_planner.py         [task_note·遗留待清理]
src/agent_runtime/context_compression/history_markdown.py      [task_note·遗留待清理]
src/agent_runtime/context_compression/prompt_repository.py     [task_note·遗留待清理]
```
