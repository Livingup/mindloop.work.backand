# -*- coding: utf-8 -*-
"""测试 LLM Registry 脱敏 API Key 还原逻辑。"""
import pytest

from agent_runtime.llm_model_registry.llm_provider_definition import (
    LlmApiKey,
    LlmProviderDefinition,
    LlmProviderModelBinding,
    LlmRetryPolicy,
)
from agent_runtime.llm_model_registry.north_gateway.llm_model_registry_rest_adapter import (
    LlmApiKeyBody,
    LlmProviderBody,
    LlmProviderModelBindingBody,
    _is_masked_api_key,
    _matches_api_key_mask,
    _restore_api_keys_from_current,
    _restore_binding_body,
    _restore_provider_body,
)


def test_is_masked_api_key():
    assert _is_masked_api_key("sk-4********CSVp") is True
    assert _is_masked_api_key("********") is True
    assert _is_masked_api_key("sk-4****CSVp") is True
    assert _is_masked_api_key("sk-4CZkydSy5mpiVTMyyEM48fwCNjyfeO74xuVq3muOwC1sCSVp") is False
    assert _is_masked_api_key("${DEEPSEEK_API_KEY}") is False


def test_matches_api_key_mask():
    real = "sk-4CZkydSy5mpiVTMyyEM48fwCNjyfeO74xuVq3muOwC1sCSVp"
    assert _matches_api_key_mask("sk-4********CSVp", real) is True
    assert _matches_api_key_mask("sk-4*********CSVp", real) is True
    assert _matches_api_key_mask("sk-X********CSVp", real) is False
    assert _matches_api_key_mask("sk-4********XXXX", real) is False

    short_real = "123456"
    assert _matches_api_key_mask("********", short_real) is True
    assert _matches_api_key_mask("****", short_real) is True


def test_restore_unnamed_key_when_renamed():
    """测试原 Key 没有名字，用户在前端为其新增名字并提交脱敏占位符。"""
    current_keys = (LlmApiKey(key="sk-real-secret-12345678", name=None),)
    body_keys = [LlmApiKeyBody(key="sk-r********5678", name="刀-0.2pro")]

    restored = _restore_api_keys_from_current(body_keys, current_keys)
    assert restored is not None
    assert len(restored) == 1
    assert restored[0].key == "sk-real-secret-12345678"
    assert restored[0].name == "刀-0.2pro"


def test_restore_named_key_when_renamed():
    """测试原 Key 已有名字，用户在前端重命名该 Key 并提交脱敏占位符。"""
    current_keys = (LlmApiKey(key="sk-real-secret-12345678", name="old-name"),)
    body_keys = [LlmApiKeyBody(key="sk-r********5678", name="new-name")]

    restored = _restore_api_keys_from_current(body_keys, current_keys)
    assert restored is not None
    assert len(restored) == 1
    assert restored[0].key == "sk-real-secret-12345678"
    assert restored[0].name == "new-name"


def test_restore_reordered_keys():
    """测试多个 Key 调整轮换顺序后，各自真实 Key 被正确保留到新顺序。"""
    current_keys = (
        LlmApiKey(key="sk-AAAA-secret-1111", name="KeyA"),
        LlmApiKey(key="sk-BBBB-secret-2222", name="KeyB"),
    )
    # 调换顺序：KeyB 在前，KeyA 在后，且 KeyB 改了名字
    body_keys = [
        LlmApiKeyBody(key="sk-B********2222", name="KeyB-renamed"),
        LlmApiKeyBody(key="sk-A********1111", name="KeyA"),
    ]

    restored = _restore_api_keys_from_current(body_keys, current_keys)
    assert restored is not None
    assert len(restored) == 2
    assert restored[0].key == "sk-BBBB-secret-2222"
    assert restored[0].name == "KeyB-renamed"
    assert restored[1].key == "sk-AAAA-secret-1111"
    assert restored[1].name == "KeyA"


def test_restore_mixed_new_and_masked_keys():
    """测试新增一个明文 Key 同时保留一个脱敏 Key。"""
    current_keys = (LlmApiKey(key="sk-AAAA-secret-1111", name="KeyA"),)
    body_keys = [
        LlmApiKeyBody(key="sk-BRAND-NEW-KEY-9999", name="NewKey"),
        LlmApiKeyBody(key="sk-A********1111", name="KeyA"),
    ]

    restored = _restore_api_keys_from_current(body_keys, current_keys)
    assert restored is not None
    assert len(restored) == 2
    assert restored[0].key == "sk-BRAND-NEW-KEY-9999"
    assert restored[0].name == "NewKey"
    assert restored[1].key == "sk-AAAA-secret-1111"
    assert restored[1].name == "KeyA"


def test_restore_raises_on_unmatched_masked_key():
    """测试如果提交的脱敏占位符无法匹配任何已有真实 Key，严禁落库并抛出 ValueError。"""
    current_keys = (LlmApiKey(key="sk-AAAA-secret-1111", name="KeyA"),)
    body_keys = [LlmApiKeyBody(key="sk-UNKNOWN********9999", name="FakeKey")]

    with pytest.raises(ValueError, match="包含脱敏占位符且无法匹配已有真实密钥"):
        _restore_api_keys_from_current(body_keys, current_keys)


def test_restore_raises_on_new_provider_with_masked_key():
    """测试新增服务商时如果直接提交脱敏 Key，抛出 ValueError。"""
    body_keys = [LlmApiKeyBody(key="sk-r********5678", name="FakeKey")]

    with pytest.raises(ValueError, match="包含脱敏占位符且无法匹配已有真实密钥"):
        _restore_api_keys_from_current(body_keys, ())


def test_restore_provider_body_integration():
    """测试 _restore_provider_body 在服务商编辑改名时成功还原真实 API Key。"""
    current = LlmProviderDefinition(
        provider_id="codexapis",
        group="default",
        api_base_url="http://localhost:8080/v1",
        custom_headers={"X-Relay-Endpoint-Id": "codexapis"},
        api_keys=(LlmApiKey(key="sk-4CZkydSy5mpiVTMyyEM48fwCNjyfeO74xuVq3muOwC1sCSVp", name=None),),
        retry=LlmRetryPolicy(max_retries_per_key=3, initial_delay_seconds=1.0, max_delay_seconds=30.0, retryable_status_codes=(429, 500, 502)),
        models=(),
    )

    body = LlmProviderBody(
        group="default",
        api_base_url="http://localhost:8080/v1",
        custom_headers={"X-Relay-Endpoint-Id": "codexapis"},
        api_keys=[LlmApiKeyBody(key="sk-4********CSVp", name="刀-0.2pro")],
        retry={"max_retries_per_key": 3, "initial_delay_seconds": 1.0, "max_delay_seconds": 30.0, "retryable_status_codes": [429, 500, 502]},
        models=[],
    )

    restored = _restore_provider_body(body, current)
    assert restored.api_keys[0].key == "sk-4CZkydSy5mpiVTMyyEM48fwCNjyfeO74xuVq3muOwC1sCSVp"
    assert restored.api_keys[0].name == "刀-0.2pro"


def test_restore_binding_body_integration():
    """测试 _restore_binding_body 在模型接入项编辑改名时成功还原真实 API Key。"""
    current_binding = LlmProviderModelBinding(
        id="gpt-5.5",
        model_id="gpt-5.5",
        model_alias="gpt-5.5",
        api_format="openai_responses",
        tool_choice=None,
        thinking=None,
        custom_request_params=None,
        api_base_url=None,
        custom_headers={},
        api_keys=(LlmApiKey(key="sk-model-binding-real-key-8888", name="old"),),
    )

    body = LlmProviderModelBindingBody(
        id="gpt-5.5",
        model_id="gpt-5.5",
        model_alias="gpt-5.5",
        api_format="openai_responses",
        tool_choice=None,
        thinking=None,
        custom_request_params=None,
        api_base_url=None,
        custom_headers={},
        api_keys=[LlmApiKeyBody(key="sk-m********8888", name="new-name")],
    )

    restored = _restore_binding_body(body, current_binding)
    assert restored.api_keys[0].key == "sk-model-binding-real-key-8888"
    assert restored.api_keys[0].name == "new-name"


def test_modify_key_value_in_place_single_key():
    """验证真正需要修改 Key 值时：在原有位置直接输入新 Key，新 Key 必须直接生效，绝不被旧 Key 覆盖。"""
    current_keys = (LlmApiKey(key="sk-old-secret-11111111", name="刀-0.2pro"),)
    # 用户把输入框的脱敏值清空，输入了全新的真实 Key
    body_keys = [LlmApiKeyBody(key="sk-brand-new-secret-22222222", name="刀-0.2pro")]

    restored = _restore_api_keys_from_current(body_keys, current_keys)
    assert restored is not None
    assert len(restored) == 1
    assert restored[0].key == "sk-brand-new-secret-22222222"
    assert restored[0].name == "刀-0.2pro"


def test_modify_key_value_and_name_together():
    """验证同时修改 Key 名和 Key 值时，新名和新 Key 均直接生效。"""
    current_keys = (LlmApiKey(key="sk-old-secret-11111111", name="old-name"),)
    body_keys = [LlmApiKeyBody(key="sk-brand-new-secret-22222222", name="new-name")]

    restored = _restore_api_keys_from_current(body_keys, current_keys)
    assert restored is not None
    assert len(restored) == 1
    assert restored[0].key == "sk-brand-new-secret-22222222"
    assert restored[0].name == "new-name"


def test_modify_one_key_value_while_leaving_other_keys_masked():
    """验证多 Key 场景下，用户仅修改其中一个 Key 的值，其他未修改的 Key（脱敏态）保留真实值，被修改的 Key 换成新值。"""
    current_keys = (
        LlmApiKey(key="sk-AAAA-secret-1111", name="KeyA"),
        LlmApiKey(key="sk-BBBB-secret-2222", name="KeyB"),
    )
    # KeyA 被替换为全新的明文 Key，KeyB 保持脱敏占位符
    body_keys = [
        LlmApiKeyBody(key="sk-NEW-FOR-A-99999999", name="KeyA"),
        LlmApiKeyBody(key="sk-B********2222", name="KeyB"),
    ]

    restored = _restore_api_keys_from_current(body_keys, current_keys)
    assert restored is not None
    assert len(restored) == 2
    # KeyA 成功修改为新值
    assert restored[0].key == "sk-NEW-FOR-A-99999999"
    assert restored[0].name == "KeyA"
    # KeyB 成功还原旧真实值
    assert restored[1].key == "sk-BBBB-secret-2222"
    assert restored[1].name == "KeyB"


def test_modify_key_to_environment_variable():
    """验证用户将 Key 修改为环境变量占位符 ${ENV_VAR} 时直接生效。"""
    current_keys = (LlmApiKey(key="sk-old-secret-11111111", name="KeyA"),)
    body_keys = [LlmApiKeyBody(key="${OPENAI_API_KEY}", name="KeyA")]

    restored = _restore_api_keys_from_current(body_keys, current_keys)
    assert restored is not None
    assert len(restored) == 1
    assert restored[0].key == "${OPENAI_API_KEY}"
    assert restored[0].name == "KeyA"


def test_modify_all_keys_to_new_values():
    """验证将所有 Key 全部换成新 Key 时，全部直接生效。"""
    current_keys = (
        LlmApiKey(key="sk-old-0", name="Key0"),
        LlmApiKey(key="sk-old-1", name="Key1"),
    )
    body_keys = [
        LlmApiKeyBody(key="sk-new-0", name="Key0"),
        LlmApiKeyBody(key="sk-new-1", name="Key1"),
    ]

    restored = _restore_api_keys_from_current(body_keys, current_keys)
    assert restored is not None
    assert len(restored) == 2
    assert restored[0].key == "sk-new-0"
    assert restored[1].key == "sk-new-1"

