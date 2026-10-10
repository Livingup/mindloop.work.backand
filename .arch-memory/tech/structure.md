# 项目结构地图

## 域映射
```text
src/agent_runtime/auth/**                                      [auth]
src/agent_runtime/communication/**                             [communication]
src/agent_runtime/server.py                                    [公共]
src/agent_runtime/runtime_network_monitor.py                   [公共]
tests/test_runtime_auth_session_lifecycle.py                   [auth]
tests/test_runtime_auth_token_transport.py                     [auth]
tests/test_communication_sse_contract.py                       [communication]
tests/test_runtime_network_auth_failure_sampling.py            [公共]
```

## 节点标注
```text
src/agent_runtime/auth/auth_service.py                         AUTH-01 AUTH-02 AUTH-03
src/agent_runtime/server.py                                    AUTH-04 [公共·认证、初始化与网络诊断组合根]
src/agent_runtime/communication/north_gateway/communication_rest_adapter.py COMMUNICATION-01
src/agent_runtime/runtime_network_monitor.py                   [公共·认证失败诊断采样]
```
