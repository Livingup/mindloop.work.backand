"""提示词片段 REST API 适配器测试。"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_runtime.prompt_template.north_gateway.prompt_snippet_rest_adapter import (
    create_prompt_snippet_router,
)
from agent_runtime.prompt_template.prompt_template_config import PromptTemplateConfig
from agent_runtime.prompt_template.prompt_template_factory import PromptTemplateFactory


@pytest.fixture()
def client(tmp_path) -> TestClient:
    """创建 REST API 测试客户端。"""
    config = PromptTemplateConfig(snippets_dir=str(tmp_path / "snippets"))
    gateway = PromptTemplateFactory.create(config)

    app = FastAPI()
    app.include_router(
        create_prompt_snippet_router(gateway),
        prefix="/api/prompt-snippets",
    )
    return TestClient(app)


# ── 创建 ─────────────────────────────────────────────────────────


def test_rest_create(client: TestClient) -> None:
    """REST 创建片段。"""
    resp = client.post(
        "/api/prompt-snippets/",
        json={"name": "测试片段", "text": "内容"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "测试片段"
    assert data["text"] == "内容"


def test_rest_create_duplicate(client: TestClient) -> None:
    """REST 创建重复片段应返回 409。"""
    client.post("/api/prompt-snippets/", json={"name": "重复", "text": "A"})
    resp = client.post("/api/prompt-snippets/", json={"name": "重复", "text": "B"})
    assert resp.status_code == 409


# ── 列表 ─────────────────────────────────────────────────────────


def test_rest_list_empty(client: TestClient) -> None:
    """空目录时返回空数组。"""
    resp = client.get("/api/prompt-snippets/")
    assert resp.status_code == 200
    assert resp.json() == []


def test_rest_list(client: TestClient) -> None:
    """列出已创建的片段。"""
    client.post("/api/prompt-snippets/", json={"name": "A", "text": "内容A"})
    client.post("/api/prompt-snippets/", json={"name": "B", "text": "内容B"})
    resp = client.get("/api/prompt-snippets/")
    assert resp.status_code == 200
    names = {s["name"] for s in resp.json()}
    assert names == {"A", "B"}


# ── 查询 ─────────────────────────────────────────────────────────


def test_rest_get(client: TestClient) -> None:
    """REST 查询片段。"""
    client.post("/api/prompt-snippets/", json={"name": "查询目标", "text": "内容"})
    resp = client.get("/api/prompt-snippets/查询目标")
    assert resp.status_code == 200
    assert resp.json()["name"] == "查询目标"


def test_rest_get_nonexistent(client: TestClient) -> None:
    """REST 查询不存在的片段应返回 404。"""
    resp = client.get("/api/prompt-snippets/不存在")
    assert resp.status_code == 404


# ── 更新 ─────────────────────────────────────────────────────────


def test_rest_update_text(client: TestClient) -> None:
    """REST 更新片段文本。"""
    client.post("/api/prompt-snippets/", json={"name": "待更新", "text": "旧内容"})
    resp = client.put(
        "/api/prompt-snippets/待更新",
        json={"new_text": "新内容"},
    )
    assert resp.status_code == 200
    assert resp.json()["text"] == "新内容"


def test_rest_update_name(client: TestClient) -> None:
    """REST 更新片段描述名。"""
    client.post("/api/prompt-snippets/", json={"name": "旧名", "text": "内容"})
    resp = client.put(
        "/api/prompt-snippets/旧名",
        json={"new_name": "新名"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "新名"


def test_rest_update_nonexistent(client: TestClient) -> None:
    """REST 更新不存在的片段应返回 404。"""
    resp = client.put(
        "/api/prompt-snippets/不存在",
        json={"new_text": "x"},
    )
    assert resp.status_code == 404


def test_rest_update_no_fields(client: TestClient) -> None:
    """REST 更新时未指定任何字段应返回 400。"""
    client.post("/api/prompt-snippets/", json={"name": "片段", "text": "内容"})
    resp = client.put("/api/prompt-snippets/片段", json={})
    assert resp.status_code == 400


# ── 分类目录 ───────────────────────────────────────────


def test_rest_create_with_category(client: TestClient) -> None:
    """REST 创建片段时指定分类目录。"""
    resp = client.post(
        "/api/prompt-snippets/",
        json={"name": "编码规则", "text": "内容", "category": "coding"},
    )
    assert resp.status_code == 201
    assert resp.json()["category"] == "coding"


def test_rest_create_without_category(client: TestClient) -> None:
    """不指定 category 时默认为 "default"。"""
    resp = client.post(
        "/api/prompt-snippets/",
        json={"name": "无分类", "text": "内容"},
    )
    assert resp.status_code == 201
    assert resp.json()["category"] == "default"


def test_rest_update_category(client: TestClient) -> None:
    """REST 更新片段分类目录。"""
    client.post("/api/prompt-snippets/", json={"name": "片段", "text": "内容"})
    resp = client.put(
        "/api/prompt-snippets/片段",
        json={"new_category": "architecture"},
    )
    assert resp.status_code == 200
    assert resp.json()["category"] == "architecture"


# ── 删除 ─────────────────────────────────────────────────────────


def test_rest_delete(client: TestClient) -> None:
    """REST 删除片段。"""
    client.post("/api/prompt-snippets/", json={"name": "待删除", "text": "内容"})
    resp = client.delete("/api/prompt-snippets/待删除")
    assert resp.status_code == 204

    # 确认已删除。
    assert client.get("/api/prompt-snippets/").json() == []


def test_rest_delete_nonexistent(client: TestClient) -> None:
    """REST 删除不存在的片段应静默返回 204。"""
    resp = client.delete("/api/prompt-snippets/不存在")
    assert resp.status_code == 204
