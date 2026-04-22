"""提示词模板服务北向网关功能测试。"""
from __future__ import annotations

import pytest

from agent_runtime.prompt_template.prompt_template_config import PromptTemplateConfig
from agent_runtime.prompt_template.prompt_template_factory import PromptTemplateFactory
from agent_runtime.prompt_template.north_gateway.prompt_snippet_contracts import (
    PromptSnippetCreateRequest,
    PromptSnippetUpdateRequest,
)
from agent_runtime.prompt_template.north_gateway.prompt_snippet_gateway import (
    PromptSnippetGateway,
)


@pytest.fixture()
def gateway(tmp_path) -> PromptSnippetGateway:
    """使用临时目录通过工厂创建北向网关实例。"""
    config = PromptTemplateConfig(snippets_dir=str(tmp_path / "snippets"))
    return PromptTemplateFactory.create(config)


# ── 创建 ─────────────────────────────────────────────────────────


def test_create_snippet(gateway: PromptSnippetGateway) -> None:
    """创建片段后可通过 name 查询到。"""
    req = PromptSnippetCreateRequest(name="测试规则", text="这是测试内容。")
    result = gateway.create_snippet(req)

    assert result.name == "测试规则"
    assert result.text == "这是测试内容。"

    fetched = gateway.get_snippet("测试规则")
    assert fetched.name == "测试规则"
    assert fetched.text == "这是测试内容。"


def test_create_duplicate_name_raises(gateway: PromptSnippetGateway) -> None:
    """name 已存在时创建应抛出 ValueError。"""
    req = PromptSnippetCreateRequest(name="重复名", text="内容A")
    gateway.create_snippet(req)

    with pytest.raises(ValueError, match="重复名"):
        gateway.create_snippet(PromptSnippetCreateRequest(name="重复名", text="内容B"))


# ── 查询 ─────────────────────────────────────────────────────────


def test_get_nonexistent_raises(gateway: PromptSnippetGateway) -> None:
    """查询不存在的片段应抛出 KeyError。"""
    with pytest.raises(KeyError, match="不存在的片段"):
        gateway.get_snippet("不存在的片段")


def test_list_snippets_empty(gateway: PromptSnippetGateway) -> None:
    """空目录时列表为空。"""
    assert gateway.list_snippets() == []


def test_list_snippets(gateway: PromptSnippetGateway) -> None:
    """列出所有已创建的片段。"""
    gateway.create_snippet(PromptSnippetCreateRequest(name="片段A", text="内容A"))
    gateway.create_snippet(PromptSnippetCreateRequest(name="片段B", text="内容B"))

    snippets = gateway.list_snippets()
    names = {s.name for s in snippets}
    assert names == {"片段A", "片段B"}


# ── 更新 ─────────────────────────────────────────────────────────


def test_update_text(gateway: PromptSnippetGateway) -> None:
    """更新片段文本内容。"""
    gateway.create_snippet(PromptSnippetCreateRequest(name="原始片段", text="旧内容"))

    result = gateway.update_snippet(
        PromptSnippetUpdateRequest(current_name="原始片段", new_text="新内容"),
    )
    assert result.name == "原始片段"
    assert result.text == "新内容"

    fetched = gateway.get_snippet("原始片段")
    assert fetched.text == "新内容"


def test_update_name(gateway: PromptSnippetGateway) -> None:
    """更新片段描述名。"""
    gateway.create_snippet(PromptSnippetCreateRequest(name="旧名称", text="内容"))

    result = gateway.update_snippet(
        PromptSnippetUpdateRequest(current_name="旧名称", new_name="新名称"),
    )
    assert result.name == "新名称"

    # 旧名称不可用。
    with pytest.raises(KeyError):
        gateway.get_snippet("旧名称")

    # 新名称可用。
    assert gateway.get_snippet("新名称").text == "内容"


def test_update_nonexistent_raises(gateway: PromptSnippetGateway) -> None:
    """更新不存在的片段应抛出 KeyError。"""
    with pytest.raises(KeyError):
        gateway.update_snippet(
            PromptSnippetUpdateRequest(current_name="不存在", new_text="x"),
        )


def test_update_name_conflict_raises(gateway: PromptSnippetGateway) -> None:
    """更新 name 时与已有片段冲突应抛出 ValueError。"""
    gateway.create_snippet(PromptSnippetCreateRequest(name="片段A", text="A"))
    gateway.create_snippet(PromptSnippetCreateRequest(name="片段B", text="B"))

    with pytest.raises(ValueError, match="片段A"):
        gateway.update_snippet(
            PromptSnippetUpdateRequest(current_name="片段B", new_name="片段A"),
        )


# ── 删除 ─────────────────────────────────────────────────────────


def test_delete_snippet(gateway: PromptSnippetGateway) -> None:
    """删除片段后不可查询。"""
    gateway.create_snippet(PromptSnippetCreateRequest(name="待删除", text="内容"))
    gateway.delete_snippet("待删除")

    with pytest.raises(KeyError):
        gateway.get_snippet("待删除")
    assert gateway.list_snippets() == []


def test_delete_nonexistent_is_noop(gateway: PromptSnippetGateway) -> None:
    """删除不存在的片段应静默跳过。"""
    gateway.delete_snippet("不存在的片段")


# ── 扫描索引 ─────────────────────────────────────────────────────


def test_scan_rebuilds_index(tmp_path) -> None:
    """重新创建网关实例时应从文件系统扫描重建索引。"""
    snippets_dir = str(tmp_path / "snippets")
    config = PromptTemplateConfig(snippets_dir=snippets_dir)

    # 第一个实例创建片段。
    gw1 = PromptTemplateFactory.create(config)
    gw1.create_snippet(PromptSnippetCreateRequest(name="持久化片段", text="内容"))

    # 第二个实例应能从文件扫描到。
    gw2 = PromptTemplateFactory.create(config)
    fetched = gw2.get_snippet("持久化片段")
    assert fetched.text == "内容"


def test_scan_subdirectory(tmp_path) -> None:
    """扫描应递归遍历子目录。"""
    snippets_dir = tmp_path / "snippets"
    sub_dir = snippets_dir / "coding"
    sub_dir.mkdir(parents=True)

    # 手动在子目录中创建片段文件。
    import yaml
    snippet_file = sub_dir / "test-snippet.yaml"
    with open(snippet_file, "w", encoding="utf-8") as f:
        yaml.dump({"name": "子目录片段", "text": "子目录内容"}, f, allow_unicode=True)

    config = PromptTemplateConfig(snippets_dir=str(snippets_dir))
    gw = PromptTemplateFactory.create(config)
    fetched = gw.get_snippet("子目录片段")
    assert fetched.text == "子目录内容"
