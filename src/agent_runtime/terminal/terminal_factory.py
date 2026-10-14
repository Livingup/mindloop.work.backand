from __future__ import annotations

from threading import RLock

from agent_runtime.agent.agent_runtime_factory import AgentRuntimeFactory
from agent_runtime.communication.communication_factory import CommunicationFactory
from agent_runtime.terminal.infrastructure.pty_terminal_process import PtyTerminalProcessManager
from agent_runtime.terminal.infrastructure.sqlite_terminal_repository import SqliteTerminalRepository
from agent_runtime.terminal.north_gateway.terminal_gateway_impl import TerminalGatewayImpl
from agent_runtime.terminal.terminal_config import TerminalConfig
from agent_runtime.terminal.terminal_service import TerminalRuntimeScopeResolver, TerminalService


class TerminalFactory:
    _lock = RLock()
    _gateway: TerminalGatewayImpl | None = None
    _config_signature: tuple[object, ...] | None = None

    @classmethod
    def create(cls) -> TerminalGatewayImpl:
        with cls._lock:
            config = cls._read_config()
            signature = (
                config.data_dir,
                config.default_shell,
                config.max_output_chars_per_read,
                config.max_live_terminals,
                config.max_live_terminals_per_agent,
                config.output_coalesce_interval_ms,
                config.output_coalesce_max_chars,
                config.output_coalesce_max_chunks,
                config.output_coalesce_max_bytes,
                config.output_ring_max_chunks,
                config.output_ring_max_chars,
            )
            if cls._gateway is None or cls._config_signature != signature:
                communication_gateway = CommunicationFactory.create()
                agent_runtime_gateway = AgentRuntimeFactory.create(
                    communication_gateway=communication_gateway,
                )
                repository = SqliteTerminalRepository(
                    data_dir_accessor=cls._build_data_dir_accessor(config),
                )
                repository.reset_runtime_state()
                process_manager = PtyTerminalProcessManager()
                scope_resolver = TerminalRuntimeScopeResolver(
                    agent_runtime_gateway=agent_runtime_gateway,
                    communication_gateway=communication_gateway,
                )
                service = TerminalService(
                    repository=repository,
                    process_manager=process_manager,
                    scope_resolver=scope_resolver,
                    communication_gateway=communication_gateway,
                    default_shell=config.default_shell,
                    max_output_chars_per_read=config.max_output_chars_per_read,
                    max_live_terminals=config.max_live_terminals,
                    max_live_terminals_per_agent=config.max_live_terminals_per_agent,
                    output_coalesce_interval_ms=config.output_coalesce_interval_ms,
                    output_coalesce_max_chars=config.output_coalesce_max_chars,
                    output_coalesce_max_chunks=config.output_coalesce_max_chunks,
                    output_coalesce_max_bytes=config.output_coalesce_max_bytes,
                    output_ring_max_chunks=config.output_ring_max_chunks,
                    output_ring_max_chars=config.output_ring_max_chars,
                )
                cls._gateway = TerminalGatewayImpl(service)
                cls._config_signature = signature
            return cls._gateway

    @staticmethod
    def _read_config() -> TerminalConfig:
        try:
            from agent_runtime.config import ConfigRegistry

            return ConfigRegistry.instance().terminal
        except RuntimeError:
            return TerminalConfig()

    @staticmethod
    def _build_data_dir_accessor(config: TerminalConfig):
        try:
            from agent_runtime.config import ConfigRegistry

            ConfigRegistry.instance()
        except RuntimeError:
            return lambda: config.data_dir
        return lambda: ConfigRegistry.instance().terminal.data_dir
