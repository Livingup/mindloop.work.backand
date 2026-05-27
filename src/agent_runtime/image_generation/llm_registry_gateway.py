from __future__ import annotations

from agent_runtime.llm_api_protocol import API_FORMAT_OPENAI_IMAGES_GENERATIONS, API_FORMAT_OPENAI_RESPONSES
from agent_runtime.llm_model_registry.llm_execution_policy import LlmExecutionPolicy
from agent_runtime.llm_model_registry.llm_model_metadata import LlmModelMetadata
from agent_runtime.llm_model_registry.llm_model_registry_service import (
    ResolvedLlmExecutionPolicy,
    ResolvedLlmExecutionPolicyCandidate,
    ResolvedLlmProviderModel,
)
from agent_runtime.llm_model_registry.llm_provider_definition import LlmProviderDefinition, LlmProviderModelBinding
from agent_runtime.llm_model_registry.north_gateway.llm_model_registry_contracts import (
    LlmExecutionPolicySaveRequest,
    LlmModelMetadataSaveRequest,
    LlmProviderSaveRequest,
)
from agent_runtime.llm_model_registry.north_gateway.llm_model_registry_gateway_impl import LlmModelRegistryGatewayImpl

GPT_IMAGE_AGENT_MODEL_ID = "gpt-5.5"


class ImageGenerationLlmRegistryGateway:
    def __init__(
        self,
        *,
        image_registry_gateway: LlmModelRegistryGatewayImpl,
        llm_registry_gateway: LlmModelRegistryGatewayImpl,
    ) -> None:
        self._image_registry_gateway = image_registry_gateway
        self._llm_registry_gateway = llm_registry_gateway

    def list_model_metadata(self) -> list[LlmModelMetadata]:
        return _merge_by_model_id(
            self._image_registry_gateway.list_model_metadata(),
            [item for item in self._llm_registry_gateway.list_model_metadata() if _is_gpt_image_agent_model(item)],
        )

    def get_model_metadata(self, model_id: str) -> LlmModelMetadata:
        try:
            return self._image_registry_gateway.get_model_metadata(model_id)
        except KeyError:
            metadata = self._llm_registry_gateway.get_model_metadata(model_id)
            if not _is_gpt_image_agent_model(metadata):
                raise
            return metadata

    def save_model_metadata(self, request: LlmModelMetadataSaveRequest) -> LlmModelMetadata:
        return self._image_registry_gateway.save_model_metadata(request)

    def delete_model_metadata(self, model_id: str) -> None:
        self._image_registry_gateway.delete_model_metadata(model_id)

    def ensure_builtin_model_metadata(self, metadata: LlmModelMetadata) -> LlmModelMetadata:
        return self._image_registry_gateway.ensure_builtin_model_metadata(metadata)

    def list_providers(self) -> list[LlmProviderDefinition]:
        return _merge_by_provider_id(
            self._filter_provider_bindings(self._image_registry_gateway.list_providers()),
            self._filter_provider_bindings(self._llm_registry_gateway.list_providers()),
        )

    def get_provider(self, provider_id: str) -> LlmProviderDefinition:
        providers: list[LlmProviderDefinition] = []
        try:
            provider = self._image_registry_gateway.get_provider(provider_id)
            filtered = self._filter_provider_bindings([provider])
            if filtered:
                providers.extend(filtered)
        except KeyError:
            pass
        try:
            provider = self._llm_registry_gateway.get_provider(provider_id)
        except KeyError:
            if providers:
                return providers[0]
            raise
        filtered = self._filter_provider_bindings([provider])
        if filtered:
            providers = _merge_by_provider_id(providers, filtered)
        if providers:
            return providers[0]
        raise KeyError(f"LLM 服务商不存在或不支持图片生成: {provider_id}")

    def save_provider(self, request: LlmProviderSaveRequest) -> LlmProviderDefinition:
        invalid_bindings = [
            f"{binding.id}: {binding.model_id}/{binding.api_format}"
            for binding in request.models
            if binding.api_format != API_FORMAT_OPENAI_IMAGES_GENERATIONS
        ]
        if invalid_bindings:
            raise ValueError(
                "图片生成独立 Registry 只能保存 openai_images_generations 服务商接入；"
                "gpt-5.5/openai_responses 接入请在主 LLM Registry 中维护: "
                + ", ".join(invalid_bindings)
            )
        return self._image_registry_gateway.save_provider(request)

    def delete_provider(self, provider_id: str) -> None:
        self._image_registry_gateway.delete_provider(provider_id)

    def list_execution_policies(self) -> list[LlmExecutionPolicy]:
        return self._image_registry_gateway.list_execution_policies()

    def get_execution_policy(self, policy_group_id: str) -> LlmExecutionPolicy:
        return self._image_registry_gateway.get_execution_policy(policy_group_id)

    def save_execution_policy(self, request: LlmExecutionPolicySaveRequest) -> LlmExecutionPolicy:
        policy = request.to_policy()
        self._validate_policy_references(policy)
        return self._image_registry_gateway.save_execution_policy(request)

    def delete_execution_policy(self, policy_group_id: str) -> None:
        self._image_registry_gateway.delete_execution_policy(policy_group_id)

    def resolve_execution_policy(self, policy_group_id: str) -> ResolvedLlmExecutionPolicy:
        policy = self._image_registry_gateway.get_execution_policy(policy_group_id)
        if not policy.enabled:
            raise ValueError(f"LLM 执行策略组已禁用: {policy_group_id}")
        candidates = policy.enabled_candidates()
        if not candidates:
            raise ValueError(f"LLM 执行策略组没有可用候选项: {policy_group_id}")
        resolved_candidates = tuple(
            ResolvedLlmExecutionPolicyCandidate(
                candidate_id=candidate.id,
                provider_id=candidate.provider_id,
                model_binding_id=candidate.model_binding_id,
                resolved_model=self.resolve_provider_model(candidate.provider_id, candidate.model_binding_id),
            )
            for candidate in candidates
        )
        return ResolvedLlmExecutionPolicy(
            policy_group_id=policy.policy_group_id,
            name=policy.name,
            description=policy.description,
            unsupported_input_modality_strategy=policy.unsupported_input_modality_strategy,
            candidates=resolved_candidates,
        )

    def list_provider_model_bindings(self, provider_id: str) -> list[LlmProviderModelBinding]:
        return list(self.get_provider(provider_id).models)

    def resolve_provider_model(self, provider_id: str, binding_id: str) -> ResolvedLlmProviderModel:
        try:
            resolved = self._image_registry_gateway.resolve_provider_model(provider_id, binding_id)
            if _is_supported_image_generation_binding(resolved.model_id, resolved.api_format):
                return resolved
        except KeyError:
            pass
        resolved = self._llm_registry_gateway.resolve_provider_model(provider_id, binding_id)
        if not _is_supported_image_generation_binding(resolved.model_id, resolved.api_format):
            raise KeyError(f"LLM 服务商模型接入项不支持图片生成: {provider_id}/{binding_id}")
        return resolved

    def _validate_policy_references(self, policy: LlmExecutionPolicy) -> None:
        missing_candidates: list[str] = []
        for candidate in policy.candidates:
            try:
                self.resolve_provider_model(candidate.provider_id, candidate.model_binding_id)
            except Exception:
                missing_candidates.append(f"{candidate.id}: {candidate.provider_id}/{candidate.model_binding_id}")
        if missing_candidates:
            raise ValueError(
                "LLM 执行策略组候选项引用了不存在或不支持图片生成的服务商模型接入项: "
                + ", ".join(missing_candidates)
            )
        if policy.enabled and not policy.enabled_candidates():
            raise ValueError("启用状态的 LLM 执行策略组至少需要一个启用候选项。")

    def _filter_provider_bindings(self, providers: list[LlmProviderDefinition]) -> list[LlmProviderDefinition]:
        filtered: list[LlmProviderDefinition] = []
        for provider in providers:
            bindings = tuple(
                binding
                for binding in provider.models
                if _is_supported_image_generation_binding(binding.model_id, binding.api_format)
            )
            if bindings:
                filtered.append(_replace_provider_models(provider, bindings))
        return filtered


def _is_gpt_image_agent_model(metadata: LlmModelMetadata) -> bool:
    return metadata.model_id == GPT_IMAGE_AGENT_MODEL_ID


def _is_supported_image_generation_binding(model_id: str, api_format: str) -> bool:
    if api_format == API_FORMAT_OPENAI_IMAGES_GENERATIONS:
        return True
    return model_id == GPT_IMAGE_AGENT_MODEL_ID and api_format == API_FORMAT_OPENAI_RESPONSES


def _replace_provider_models(provider: LlmProviderDefinition, models: tuple[LlmProviderModelBinding, ...]) -> LlmProviderDefinition:
    return LlmProviderDefinition(
        provider_id=provider.provider_id,
        group=provider.group,
        api_base_url=provider.api_base_url,
        custom_headers=dict(provider.custom_headers),
        api_keys=provider.api_keys,
        retry=provider.retry,
        models=models,
    )


def _merge_by_model_id(primary: list[LlmModelMetadata], secondary: list[LlmModelMetadata]) -> list[LlmModelMetadata]:
    result = list(primary)
    seen = {item.model_id for item in result}
    for item in secondary:
        if item.model_id not in seen:
            result.append(item)
            seen.add(item.model_id)
    return result


def _merge_by_provider_id(primary: list[LlmProviderDefinition], secondary: list[LlmProviderDefinition]) -> list[LlmProviderDefinition]:
    result = list(primary)
    indexes = {item.provider_id: index for index, item in enumerate(result)}
    for item in secondary:
        index = indexes.get(item.provider_id)
        if index is None:
            result.append(item)
            indexes[item.provider_id] = len(result) - 1
        else:
            result[index] = _merge_provider_models(result[index], item)
    return result


def _merge_provider_models(primary: LlmProviderDefinition, secondary: LlmProviderDefinition) -> LlmProviderDefinition:
    models = list(primary.models)
    seen = {item.id for item in models}
    for item in secondary.models:
        if item.id not in seen:
            models.append(item)
            seen.add(item.id)
    return _replace_provider_models(primary, tuple(models))
