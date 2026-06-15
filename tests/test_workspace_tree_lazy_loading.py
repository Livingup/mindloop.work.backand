from __future__ import annotations

from agent_runtime.file_tools.workspace_rest_adapter import _read_children


def test_workspace_tree_marks_depth_boundary_folders_as_unloaded(tmp_path):
    (tmp_path / "level1" / "level2" / "level3").mkdir(parents=True)
    (tmp_path / "level1" / "root_file.txt").write_text("root", encoding="utf-8")

    nodes = _read_children(tmp_path, depth=0, max_depth=1, max_entries_per_directory=80)

    level1 = next(node for node in nodes if node.name == "level1")
    assert level1.children_loaded is True
    level2 = next(node for node in level1.children if node.name == "level2")
    assert level2.children_loaded is False
    assert level2.children == []


def test_workspace_tree_lazy_request_returns_bounded_subtree(tmp_path):
    (tmp_path / "level1" / "level2" / "level3").mkdir(parents=True)
    (tmp_path / "level1" / "level2" / "level2_file.txt").write_text("level2", encoding="utf-8")
    (tmp_path / "level1" / "level2" / "level3" / "level3_file.txt").write_text("level3", encoding="utf-8")

    nodes = _read_children(
        tmp_path / "level1" / "level2",
        depth=0,
        max_depth=1,
        max_entries_per_directory=80,
    )

    level3 = next(node for node in nodes if node.name == "level3")
    assert level3.children_loaded is True
    assert [child.name for child in level3.children] == ["level3_file.txt"]
    assert any(node.name == "level2_file.txt" and node.type == "file" for node in nodes)
