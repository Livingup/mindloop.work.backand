# -*- coding: utf-8 -*-
from __future__ import annotations

from threading import RLock

from agent_runtime.llm_http_trace.config import LlmHttpTraceConfig
from agent_runtime.llm_http_trace.infrastructure import SqliteLlmHttpTraceRepository
from agent_runtime.llm_http_trace.service import LlmHttpTraceService


class LlmHttpTraceFactory:
    _lock = RLock()
    _service: LlmHttpTraceService | None = None
    _config_signature: tuple[bool, str, int] | None = None

    @classmethod
    def create(cls) -> LlmHttpTraceService:
        with cls._lock:
            config = cls._read_config()
            signature = (config.enabled, config.data_dir, config.max_body_bytes)
            if cls._service is None or cls._config_signature != signature:
                repository = SqliteLlmHttpTraceRepository(
                    data_dir_accessor=cls._build_data_dir_accessor(config),
                )
                cls._service = LlmHttpTraceService(repository, config)
                cls._config_signature = signature
            return cls._service

    @staticmethod
    def _read_config() -> LlmHttpTraceConfig:
        try:
            from agent_runtime.config import ConfigRegistry

            return ConfigRegistry.instance().llm_http_trace
        except RuntimeError:
            return LlmHttpTraceConfig()

    @staticmethod
    def _build_data_dir_accessor(config: LlmHttpTraceConfig):
        try:
            from agent_runtime.config import ConfigRegistry

            ConfigRegistry.instance()
        except RuntimeError:
            return lambda: config.data_dir
        return lambda: ConfigRegistry.instance().llm_http_trace.data_dir
