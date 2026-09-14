# -*- coding: utf-8 -*-
"""提示词片段连续行局部编辑单元测试。

测试覆盖：
1. 聚合根 PromptSnippet.apply_text_edits 业务行为与不变量
   - 单行替换、多行替换、多编辑项顺序应用
   - 插入、删除（replace_lines=[]）、行数膨胀/收缩
   - 唯一匹配规则（0 命中、多命中报错）
   - 参数校验（空 edits、空 search_lines、含换行符）
   - 换行符与末尾换行保留（\\r\\n、\\n、无末尾换行）
   - Unicode 多语言支持
   - name 与 category 保持不变
2. 领域服务 PromptSnippetService
   - 成功编辑并保存仓储
   - expected_text_sha256 乐观并发控制（匹配成功 / 冲突报错）
   - 原子性保证（后续编辑失败时仓储不产生部分更新）
   - 不存在的片段抛出 KeyError (404)
3. REST API 适配层
   - POST /api/prompt-snippets/{name}/content/edits 成功返回 200 及 SnippetResponse
   - 结构非法（空 edits / 空 search_lines）返回 400
   - 片段不存在返回 404
   - 匹配冲突或版本冲突返回 409
   - 现有 GET/PUT/PATCH /content 接口全量替换兼容性
4. 管理脚本 manage-config.mjs 动作映射与参数校验
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_runtime.prompt_template.north_gateway.prompt_snippet_contracts import (
    PromptSnippetCreateRequest,
    PromptSnippetEditRequest,
    PromptSnippetTextEdit,
    PromptSnippetUpdateRequest,
)
from agent_runtime.prompt_template.north_gateway.prompt_snippet_gateway import (
    PromptSnippetGateway,
)
from agent_runtime.prompt_template.north_gateway.prompt_snippet_rest_adapter import (
    create_prompt_snippet_router,
)
from agent_runtime.prompt_template.prompt_snippet import (
    PromptSnippet,
    PromptSnippetConflictError,
    PromptSnippetValidationError,
)
from agent_runtime.prompt_template.prompt_snippet_repository import (
    PromptSnippetRepository,
)
from agent_runtime.prompt_template.prompt_snippet_service import (
    PromptSnippetService,
)


class InMemoryPromptSnippetRepository:
    """内存中的提示词片段仓储，用于测试。"""

    def __init__(self, initial_snippets: list[PromptSnippet] | None = None) -> None:
        self._store: dict[str, PromptSnippet] = {
            s.name: s for s in (initial_snippets or [])
        }

    def find_by_name(self, name: str) -> PromptSnippet | None:
        return self._store.get(name)

    def find_all(self) -> list[PromptSnippet]:
        return list(self._store.values())

    def save(self, snippet: PromptSnippet) -> None:
        self._store[snippet.name] = snippet

    def delete(self, name: str) -> bool:
        return self._store.pop(name, None) is not None

    def exists(self, name: str) -> bool:
        return name in self._store

    def rename(self, old_name: str, new_name: str) -> None:
        snippet = self._store.pop(old_name, None)
        if snippet is not None:
            self._store[new_name] = PromptSnippet(
                name=new_name, text=snippet.text, category=snippet.category
            )

    def reload(self) -> None:
        pass


# ── 1. 聚合根单元测试 ─────────────────────────────────────────────


def test_apply_text_edits_single_line_replacement():
    snippet = PromptSnippet(
        name="test",
        text="line1\nline2\nline3\n",
        category="default",
    )
    updated = snippet.apply_text_edits(
        [PromptSnippetTextEdit(search_lines=["line2"], replace_lines=["line2_modified"])]
    )
    assert updated.text == "line1\nline2_modified\nline3\n"
    assert updated.name == "test"
    assert updated.category == "default"


def test_apply_text_edits_multi_line_replacement():
    snippet = PromptSnippet(
        name="test",
        text="A\nB\nC\nD\n",
        category="default",
    )
    updated = snippet.apply_text_edits(
        [PromptSnippetTextEdit(search_lines=["B", "C"], replace_lines=["X", "Y", "Z"])]
    )
    assert updated.text == "A\nX\nY\nZ\nD\n"


def test_apply_text_edits_deletion():
    snippet = PromptSnippet(
        name="test",
        text="line1\nline2\nline3\n",
        category="default",
    )
    updated = snippet.apply_text_edits(
        [PromptSnippetTextEdit(search_lines=["line2"], replace_lines=[])]
    )
    assert updated.text == "line1\nline3\n"


def test_apply_text_edits_sequential_application():
    snippet = PromptSnippet(
        name="test",
        text="start\nfoo\nbar\nend\n",
        category="default",
    )
    updated = snippet.apply_text_edits([
        PromptSnippetTextEdit(search_lines=["foo"], replace_lines=["baz"]),
        PromptSnippetTextEdit(search_lines=["baz", "bar"], replace_lines=["qux"]),
    ])
    assert updated.text == "start\nqux\nend\n"


def test_apply_text_edits_zero_match_conflict():
    snippet = PromptSnippet(
        name="test",
        text="line1\nline2\n",
        category="default",
    )
    with pytest.raises(PromptSnippetConflictError) as exc_info:
        snippet.apply_text_edits(
            [PromptSnippetTextEdit(search_lines=["nonexistent"], replace_lines=["new"])]
        )
    assert "实际命中 0 次" in str(exc_info.value)


def test_apply_text_edits_multiple_match_conflict():
    snippet = PromptSnippet(
        name="test",
        text="dup\nmid\ndup\n",
        category="default",
    )
    with pytest.raises(PromptSnippetConflictError) as exc_info:
        snippet.apply_text_edits(
            [PromptSnippetTextEdit(search_lines=["dup"], replace_lines=["new"])]
        )
    assert "实际命中 2 次" in str(exc_info.value)


def test_apply_text_edits_empty_edits_validation_error():
    snippet = PromptSnippet(name="test", text="hello\n", category="default")
    with pytest.raises(PromptSnippetValidationError):
        snippet.apply_text_edits([])


def test_apply_text_edits_empty_search_lines_validation_error():
    snippet = PromptSnippet(name="test", text="hello\n", category="default")
    with pytest.raises(PromptSnippetValidationError):
        snippet.apply_text_edits(
            [PromptSnippetTextEdit(search_lines=[], replace_lines=["world"])]
        )


def test_apply_text_edits_embedded_newline_validation_error():
    snippet = PromptSnippet(name="test", text="hello\nworld\n", category="default")
    with pytest.raises(PromptSnippetValidationError):
        snippet.apply_text_edits(
            [PromptSnippetTextEdit(search_lines=["hello\nworld"], replace_lines=["new"])]
        )
    with pytest.raises(PromptSnippetValidationError):
        snippet.apply_text_edits(
            [PromptSnippetTextEdit(search_lines=["hello"], replace_lines=["new\nline"])]
        )


def test_apply_text_edits_crlf_preservation():
    snippet = PromptSnippet(
        name="test",
        text="line1\r\nline2\r\nline3\r\n",
        category="default",
    )
    updated = snippet.apply_text_edits(
        [PromptSnippetTextEdit(search_lines=["line2"], replace_lines=["new2"])]
    )
    assert updated.text == "line1\r\nnew2\r\nline3\r\n"


def test_apply_text_edits_no_final_newline_preservation():
    snippet = PromptSnippet(
        name="test",
        text="line1\nline2",
        category="default",
    )
    updated = snippet.apply_text_edits(
        [PromptSnippetTextEdit(search_lines=["line2"], replace_lines=["new2", "new3"])]
    )
    assert updated.text == "line1\nnew2\nnew3"


def test_apply_text_edits_unicode_preservation():
    snippet = PromptSnippet(
        name="测试片段",
        text="第一行：你好世界\n第二行：🤖 Agent\n第三行：结束\n",
        category="default",
    )
    updated = snippet.apply_text_edits(
        [PromptSnippetTextEdit(search_lines=["第二行：🤖 Agent"], replace_lines=["第二行：🚀 Super Agent"])]
    )
    assert updated.text == "第一行：你好世界\n第二行：🚀 Super Agent\n第三行：结束\n"


# ── 2. 领域服务与原子性测试 ─────────────────────────────────────────


def test_service_edit_snippet_success():
    repo = InMemoryPromptSnippetRepository([
        PromptSnippet(name="snippet_a", text="alpha\nbeta\n", category="coding"),
    ])
    service = PromptSnippetService(repo)
    result = service.edit_snippet_content(
        PromptSnippetEditRequest(
            name="snippet_a",
            edits=[PromptSnippetTextEdit(search_lines=["beta"], replace_lines=["gamma"])],
        )
    )
    assert result.text == "alpha\ngamma\n"
    assert repo.find_by_name("snippet_a").text == "alpha\ngamma\n"
    assert repo.find_by_name("snippet_a").category == "coding"


def test_service_edit_with_expected_sha256_success():
    orig_text = "alpha\nbeta\n"
    expected_sha = hashlib.sha256(orig_text.encode("utf-8")).hexdigest()
    repo = InMemoryPromptSnippetRepository([
        PromptSnippet(name="snippet_a", text=orig_text, category="coding"),
    ])
    service = PromptSnippetService(repo)
    result = service.edit_snippet_content(
        PromptSnippetEditRequest(
            name="snippet_a",
            edits=[PromptSnippetTextEdit(search_lines=["beta"], replace_lines=["gamma"])],
            expected_text_sha256=expected_sha,
        )
    )
    assert result.text == "alpha\ngamma\n"


def test_service_edit_with_expected_sha256_conflict():
    repo = InMemoryPromptSnippetRepository([
        PromptSnippet(name="snippet_a", text="alpha\nbeta\n", category="coding"),
    ])
    service = PromptSnippetService(repo)
    with pytest.raises(PromptSnippetConflictError) as exc_info:
        service.edit_snippet_content(
            PromptSnippetEditRequest(
                name="snippet_a",
                edits=[PromptSnippetTextEdit(search_lines=["beta"], replace_lines=["gamma"])],
                expected_text_sha256="wrong_sha256_hash",
            )
        )
    assert "提示词正文版本冲突" in str(exc_info.value)
    # 仓储正文未被改变
    assert repo.find_by_name("snippet_a").text == "alpha\nbeta\n"


def test_service_edit_atomicity_on_failure():
    orig_text = "step1\nstep2\nstep3\n"
    repo = InMemoryPromptSnippetRepository([
        PromptSnippet(name="atomic_test", text=orig_text, category="default"),
    ])
    service = PromptSnippetService(repo)

    # 第一个 edit 命中，第二个 edit 未命中（将报错）
    with pytest.raises(PromptSnippetConflictError):
        service.edit_snippet_content(
            PromptSnippetEditRequest(
                name="atomic_test",
                edits=[
                    PromptSnippetTextEdit(search_lines=["step1"], replace_lines=["step1_done"]),
                    PromptSnippetTextEdit(search_lines=["nonexistent_step"], replace_lines=["fail"]),
                ],
            )
        )

    # 仓储中的原始文本未发生任何部分更新
    assert repo.find_by_name("atomic_test").text == orig_text


def test_service_edit_not_found():
    repo = InMemoryPromptSnippetRepository([])
    service = PromptSnippetService(repo)
    with pytest.raises(KeyError):
        service.edit_snippet_content(
            PromptSnippetEditRequest(
                name="missing",
                edits=[PromptSnippetTextEdit(search_lines=["foo"], replace_lines=["bar"])],
            )
        )


# ── 3. REST 适配层测试 ───────────────────────────────────────────


@pytest.fixture
def test_client():
    repo = InMemoryPromptSnippetRepository([
        PromptSnippet(name="my_prompt", text="line A\nline B\nline C\n", category="system"),
    ])
    service = PromptSnippetService(repo)
    gateway = PromptSnippetGateway(service)
    router = create_prompt_snippet_router(gateway)

    app = FastAPI()
    app.include_router(router, prefix="/api/prompt-snippets")
    return TestClient(app)


def test_rest_edit_snippet_content_success(test_client):
    res = test_client.post(
        "/api/prompt-snippets/my_prompt/content/edits",
        json={
            "edits": [
                {
                    "search_lines": ["line B"],
                    "replace_lines": ["line B modified"],
                }
            ]
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["name"] == "my_prompt"
    assert data["text"] == "line A\nline B modified\nline C\n"
    assert data["category"] == "system"


def test_rest_edit_snippet_content_with_sha256_success(test_client):
    orig_sha = hashlib.sha256("line A\nline B\nline C\n".encode("utf-8")).hexdigest()
    res = test_client.post(
        "/api/prompt-snippets/my_prompt/content/edits",
        json={
            "expected_text_sha256": orig_sha,
            "edits": [
                {
                    "search_lines": ["line C"],
                    "replace_lines": ["line C new"],
                }
            ],
        },
    )
    assert res.status_code == 200
    assert res.json()["text"] == "line A\nline B\nline C new\n"


def test_rest_edit_snippet_content_sha256_mismatch_409(test_client):
    res = test_client.post(
        "/api/prompt-snippets/my_prompt/content/edits",
        json={
            "expected_text_sha256": "invalid_hash",
            "edits": [
                {
                    "search_lines": ["line B"],
                    "replace_lines": ["new"],
                }
            ],
        },
    )
    assert res.status_code == 409
    assert "提示词正文版本冲突" in res.json()["detail"]


def test_rest_edit_snippet_content_match_conflict_409(test_client):
    res = test_client.post(
        "/api/prompt-snippets/my_prompt/content/edits",
        json={
            "edits": [
                {
                    "search_lines": ["does not exist"],
                    "replace_lines": ["new"],
                }
            ]
        },
    )
    assert res.status_code == 409
    assert "实际命中 0 次" in res.json()["detail"]


def test_rest_edit_snippet_content_empty_edits_400(test_client):
    res = test_client.post(
        "/api/prompt-snippets/my_prompt/content/edits",
        json={"edits": []},
    )
    assert res.status_code == 400


def test_rest_edit_snippet_content_empty_search_lines_400(test_client):
    res = test_client.post(
        "/api/prompt-snippets/my_prompt/content/edits",
        json={
            "edits": [
                {
                    "search_lines": [],
                    "replace_lines": ["new"],
                }
            ]
        },
    )
    assert res.status_code == 400


def test_rest_edit_snippet_content_not_found_404(test_client):
    res = test_client.post(
        "/api/prompt-snippets/nonexistent/content/edits",
        json={
            "edits": [
                {
                    "search_lines": ["line B"],
                    "replace_lines": ["new"],
                }
            ]
        },
    )
    assert res.status_code == 404


def test_rest_backward_compatibility_get_put_patch_content(test_client):
    # GET /content
    res_get = test_client.get("/api/prompt-snippets/my_prompt/content")
    assert res_get.status_code == 200
    assert res_get.json() == {"name": "my_prompt", "text": "line A\nline B\nline C\n"}

    # PUT /content (全量覆盖)
    res_put = test_client.put(
        "/api/prompt-snippets/my_prompt/content",
        json={"text": "full replacement text"},
    )
    assert res_put.status_code == 200
    assert res_put.json()["text"] == "full replacement text"

    # PATCH /content (全量覆盖)
    res_patch = test_client.patch(
        "/api/prompt-snippets/my_prompt/content",
        json={"text": "patch replacement text"},
    )
    assert res_patch.status_code == 200
    assert res_patch.json()["text"] == "patch replacement text"


# ── 4. 管理脚本 manage-config.mjs 参数校验与路由测试 ───────────────


def test_script_edit_content_validation():
    script_path = (
        Path(__file__).resolve().parent.parent
        / "builtin-skills"
        / "spread-ai-config-management"
        / "scripts"
        / "manage-config.mjs"
    )

    # 1. 非 prompt 资源使用 edit-content 应被拒绝
    proc = subprocess.run(
        ["node", str(script_path), "agent", "edit-content", "--id", "test_id", "--file", "dummy.json"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "edit-content is only supported for prompt." in (proc.stderr or proc.stdout)

    # 2. 缺少 --id 参数应报错
    proc = subprocess.run(
        ["node", str(script_path), "prompt", "edit-content", "--file", "dummy.json"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "edit-content requires --id." in (proc.stderr or proc.stdout)

    # 3. 缺少 --file 参数应报错
    proc = subprocess.run(
        ["node", str(script_path), "prompt", "edit-content", "--id", "my_prompt"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "edit-content requires --file." in (proc.stderr or proc.stdout)

    # 4. edit-content 不支持 --sub-path
    proc = subprocess.run(
        ["node", str(script_path), "prompt", "edit-content", "--id", "my_prompt", "--file", "dummy.json", "--sub-path", "sub"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "edit-content does not support --sub-path." in (proc.stderr or proc.stdout)
