# -*- coding: utf-8 -*-
"""Agent Task Note Markdown 现场工作手册格式化器与反查引擎。

职责：
1. 切回业务状态时，将 Note 数据库表结构数据组织成结构化、可直接指导业务推进的 Markdown 现场工作手记（拒绝原始数据表 JSON 倾倒）；
2. 源码行号反查（Source Code Reverse Lookup）：针对 path + line_ranges 索引，反查本地磁盘文件，提取真实源码行并标注 1-based 行号；
3. 命令行反查（Command Execution Reverse Lookup）：针对记录中的 command_id，反查真实 command_line，规范标注为“命令行执行 ID”；
4. 父子协作反查与专线对话流：展现子 Agent 拓扑关系以及父子专线双向通信流水；
5. 远程 Agent 通信反查：精准展现 remote_agent_instance_id 与远程双向通信流水；
6. 任务清单与进展行动指针：将 todos 格式化为 Markdown 复选框，突出展示 next_step 作为最高优先级执行指针。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Sequence

logger = logging.getLogger("agent_runtime.task_note.markdown_formatter")

_LANG_MAP: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "jsx",
    ".json": "json",
    ".sql": "sql",
    ".html": "html",
    ".css": "css",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".md": "markdown",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".ini": "ini",
    ".xml": "xml",
}


def _detect_lang(path_str: str) -> str:
    ext = os.path.splitext(path_str)[1].lower()
    return _LANG_MAP.get(ext, "")


def reverse_lookup_source_code(
    path: str | None,
    line_ranges: list[dict[str, Any]] | list[list[int]] | str | None,
    current_working_directory: str = "",
    max_lines_per_range: int = 150,
) -> str | None:
    """根据 path 与 line_ranges 从磁盘读取真实源码行并格式化为带行号的代码块。"""
    if not path or not line_ranges:
        return None

    # 解析 line_ranges 结构
    parsed_ranges: list[tuple[int, int]] = []
    if isinstance(line_ranges, str):
        try:
            raw_ranges = json.loads(line_ranges)
        except Exception:
            raw_ranges = []
    else:
        raw_ranges = line_ranges

    if isinstance(raw_ranges, Sequence):
        for item in raw_ranges:
            if isinstance(item, dict):
                start = item.get("start_line") or item.get("start") or 1
                end = item.get("end_line") or item.get("end") or start
                parsed_ranges.append((int(start), int(end)))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                parsed_ranges.append((int(item[0]), int(item[1])))

    if not parsed_ranges:
        return None

    # 定位磁盘物理文件
    resolved_path: Path
    p = Path(path)
    if p.is_absolute():
        resolved_path = p
    elif current_working_directory:
        resolved_path = Path(current_working_directory) / p
    else:
        resolved_path = p

    if not resolved_path.exists() or not resolved_path.is_file():
        # 尝试使用当前工作目录直接找相对文件名
        if not resolved_path.exists() and current_working_directory:
            resolved_path = Path(current_working_directory) / p.name
        if not resolved_path.exists() or not resolved_path.is_file():
            range_desc = ", ".join(f"L{s}-L{e}" for s, e in parsed_ranges)
            return f"*(代码文件 `{path}` [{range_desc}] 暂未在磁盘找到)*"

    # 读取文件内容
    lines: list[str] = []
    for enc in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
        try:
            lines = resolved_path.read_text(encoding=enc).splitlines()
            break
        except Exception:
            continue
    else:
        return f"*(代码文件 `{path}` 读取编码异常)*"

    total_lines = len(lines)
    lang = _detect_lang(path)
    snippets: list[str] = []

    for start_line, end_line in parsed_ranges:
        actual_start = max(1, start_line)
        actual_end = min(total_lines, end_line)
        if actual_start > total_lines:
            snippets.append(f"// [行区间 L{start_line}-L{end_line} 超出文件最大行号 {total_lines}]")
            continue

        selected_lines = lines[actual_start - 1 : actual_end]
        if len(selected_lines) > max_lines_per_range:
            head = selected_lines[:50]
            tail = selected_lines[-20:]
            formatted_parts: list[str] = []
            for i, line in enumerate(head, start=actual_start):
                formatted_parts.append(f"{i:4d} | {line}")
            omitted_count = len(selected_lines) - 70
            formatted_parts.append(f" ... | ... (中间省略 {omitted_count} 行) ...")
            tail_start = actual_end - len(tail) + 1
            for i, line in enumerate(tail, start=tail_start):
                formatted_parts.append(f"{i:4d} | {line}")
            code_block_text = "\n".join(formatted_parts)
        else:
            code_block_text = "\n".join(
                f"{i:4d} | {line}" for i, line in enumerate(selected_lines, start=actual_start)
            )

        snippets.append(f"```{lang}\n// [源文件: {path} (行号: {actual_start}-{actual_end})]\n{code_block_text}\n```")

    return "\n\n".join(snippets)


def _format_command_execution(command_id: str | None, command_lookup: dict[str, str]) -> str:
    """格式化命令行执行 ID 与真实命令行。"""
    if not command_id:
        return ""
    cmd_line = command_lookup.get(command_id)
    if cmd_line:
        return f"> 💻 **命令行执行 ID**: `{command_id}` ➔ **执行命令**: `{cmd_line}`"
    return f"> 💻 **命令行执行 ID**: `{command_id}`"



def fold_artifacts_net_changes(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 path 分组并按时间排序折叠物理产物净变化。

    折叠规则：
    1. 同一路径多次 modify 只输出一条净变化；
    2. 本会期内 create 后 delete 的路径不输出；
    3. move 以最终路径为键并注明原路径，后续对新路径的 modify 归入该路径；
    4. 非文件产物（type='other'）保持原样输出。
    """
    file_ops = ("file_create", "create", "file_modify", "modify", "file_delete", "delete", "file_move", "move")
    others: list[dict[str, Any]] = []
    file_records: list[dict[str, Any]] = []
    for a in artifacts:
        t = a.get("type")
        if t in file_ops:
            file_records.append(dict(a))
        else:
            others.append(dict(a))

    file_records.sort(key=lambda x: str(x.get("created_at", "")))

    class Lifecycle:
        def __init__(self, path: str, first_rec: dict[str, Any]) -> None:
            self.original_path = path
            self.current_path = path
            self.was_created = first_rec.get("type") in ("file_create", "create")
            self.is_deleted = first_rec.get("type") in ("file_delete", "delete")
            self.moves: list[tuple[str, str]] = []
            self.records: list[dict[str, Any]] = [first_rec]

    lifecycles: dict[str, Lifecycle] = {}

    for rec in file_records:
        t = rec.get("type")
        if t in ("file_create", "create"):
            p = rec.get("file_path") or rec.get("path") or ""
            lc = Lifecycle(p, rec)
            lifecycles[p] = lc
        elif t in ("file_modify", "modify"):
            p = rec.get("file_path") or rec.get("path") or ""
            if p in lifecycles:
                lc = lifecycles[p]
                lc.records.append(rec)
            else:
                lc = Lifecycle(p, rec)
                lifecycles[p] = lc
        elif t in ("file_move", "move"):
            src = rec.get("source_path") or rec.get("file_path") or rec.get("path") or ""
            dst = rec.get("target_path") or rec.get("path") or ""
            if src in lifecycles:
                lc = lifecycles.pop(src)
                lc.current_path = dst
                lc.moves.append((src, dst))
                lc.records.append(rec)
                lifecycles[dst] = lc
            else:
                lc = Lifecycle(src, rec)
                lc.current_path = dst
                lc.moves.append((src, dst))
                lc.records.append(rec)
                lifecycles[dst] = lc
        elif t in ("file_delete", "delete"):
            p = rec.get("file_path") or rec.get("path") or ""
            if p in lifecycles:
                lc = lifecycles[p]
                lc.is_deleted = True
                lc.records.append(rec)
            else:
                lc = Lifecycle(p, rec)
                lc.is_deleted = True
                lifecycles[p] = lc

    results: list[dict[str, Any]] = []
    for lc in lifecycles.values():
        if lc.is_deleted:
            if lc.was_created:
                # create + delete: 不输出
                continue
            del_rec = dict(lc.records[-1])
            del_rec["type"] = "file_delete"
            del_rec["file_path"] = lc.current_path
            results.append(del_rec)
        else:
            if lc.moves:
                last_rec = dict(lc.records[-1])
                last_rec["type"] = "file_move"
                last_rec["file_path"] = lc.current_path
                last_rec["source_path"] = lc.original_path
                last_rec["target_path"] = lc.current_path
                desc = last_rec.get("description") or ""
                note = f"原路径: {lc.original_path}"
                last_rec["description"] = f"{desc} ({note})" if desc else note
                results.append(last_rec)
            elif lc.was_created:
                last_rec = dict(lc.records[-1])
                last_rec["type"] = "file_create"
                last_rec["file_path"] = lc.current_path
                results.append(last_rec)
            else:
                last_rec = dict(lc.records[-1])
                last_rec["type"] = "file_modify"
                last_rec["file_path"] = lc.current_path
                results.append(last_rec)

    return others + results


def format_task_note_to_markdown(
    note_snapshot: dict[str, Any],
    *,
    command_lookup: dict[str, str] | None = None,
    current_working_directory: str = "",
) -> str:
    """将 Note 快照及反查数据全量格式化为高质量 Markdown 现场工作手册。"""
    cmd_lookup = dict(command_lookup or {})
    # 若 snapshot 中自带 commands 记录，自动注入到 lookup 字典中
    if "commands" in note_snapshot and isinstance(note_snapshot["commands"], list):
        for c in note_snapshot["commands"]:
            if isinstance(c, dict) and "command_id" in c and "command_line" in c:
                cmd_lookup.setdefault(c["command_id"], c["command_line"])

    sections: list[str] = ["# 任务现场工作手记 (Task Work Note)"]
    task_id = note_snapshot.get("task_id")
    agent_id = note_snapshot.get("agent_instance_id")
    if task_id or agent_id:
        tid_str = f"`{task_id}`" if task_id else "-"
        aid_str = f"`{agent_id}`" if agent_id else "-"
        sections.append(f"> 任务 ID: {tid_str} | Agent 实例: {aid_str}\n")

    sec_idx = 1

    # ── 1. 当前行动指针与最新进展 (Progress & Next Action) ─────────────
    progresses = note_snapshot.get("progresses") or []
    if not progresses and note_snapshot.get("progress"):
        p_val = note_snapshot["progress"]
        progresses = p_val if isinstance(p_val, list) else [p_val]
    if progresses:
        p_lines = [f"## {sec_idx}. 现场进度与下一步行动指针 (Progress & Next Action)"]
        sec_idx += 1
        has_any = False
        for p in progresses:
            if not isinstance(p, dict):
                continue
            has_fields = any(
                p.get(k)
                for k in (
                    "next_step",
                    "completed_summary",
                    "current_work",
                    "current_blockers",
                    "recent_validation",
                    "handoff_notes",
                )
            )
            if not has_fields:
                continue
            has_any = True
            mid = p.get("mission_id")
            if mid and len(progresses) > 1:
                p_lines.append(f"### 任务 `{mid}` 进度")
            next_step = p.get("next_step") or "(未设定明确下一步)"
            p_lines.append(f"> 🎯 **下一步明确行动 (Next Step)**:\n> **{next_step}**\n")

            comp = p.get("completed_summary")
            if comp:
                p_lines.append(f"- **已完成工作摘要**: {comp}")
            cur_w = p.get("current_work")
            if cur_w:
                p_lines.append(f"- **当前正在进行**: {cur_w}")
            blockers = p.get("current_blockers")
            if blockers:
                p_lines.append(f"- **当前阻塞与风险**: {blockers}")
            rec_v = p.get("recent_validation")
            if rec_v:
                p_lines.append(f"- **最近验证结论**: {rec_v}")
            handoff = p.get("handoff_notes")
            if handoff:
                p_lines.append(f"- **交接备忘**: {handoff}")
        if has_any:
            sections.append("\n".join(p_lines))

    # ── 2. 待办任务清单 (Todos) ───────────────────────────────────────
    todos = note_snapshot.get("todos") or []
    if todos:
        t_lines = [f"## {sec_idx}. 任务待办清单 (Todos)"]
        sec_idx += 1
        sorted_todos = sorted(todos, key=lambda x: (x.get("priority", 999), x.get("created_at", "")))
        todo_lines: list[str] = []
        for t in sorted_todos:
            p = t.get("priority", 1)
            content = t.get("todo_content", "")
            status = t.get("status", "pending")
            tid = t.get("todo_id", "")
            if status in ("completed", "done", "resolved"):
                box = "- [x]"
                label = f"[P{p}] {content}"
            elif status in ("in_progress", "active"):
                box = "- [/]"
                label = f"[P{p}] {content} ⏳ *(进行中)*"
            elif status in ("cancelled", "canceled", "superseded"):
                box = "- [ ]"
                label = f"~~[P{p}] {content} (已取消)~~"
            else:
                box = "- [ ]"
                label = f"[P{p}] {content}"
            todo_lines.append(f"{box} {label} `({tid})`")
        t_lines.append("\n".join(todo_lines))
        sections.append("\n".join(t_lines))

    # ── 3. 用户任务目标与意图 (Missions) ───────────────────────────────
    missions = note_snapshot.get("missions") or []
    if missions:
        m_lines = [f"## {sec_idx}. 意图与目标需求 (Missions)"]
        sec_idx += 1
        for idx, m in enumerate(missions, start=1):
            mid = m.get("mission_id", f"m_{idx}")
            title = m.get("title", "(无标题任务)")
            st = m.get("status", "active")
            pruned_at = m.get("pruned_at")
            outcome_summary = m.get("outcome_summary")
            if pruned_at:
                summary_text = outcome_summary or "已闭环"
                m_lines.append(f"### 📦 [交付结果卡片] {title} (ID: `{mid}`, 状态: `{st}`)\n- **交付结论与总结**: {summary_text}")
                continue

            refined = m.get("refined_requirement", "")
            raw_prompts = m.get("raw_prompts") or []
            m_lines.append(f"### Mission: {title} (ID: `{mid}`, 状态: `{st}`)")
            if refined:
                m_lines.append(f"- **精炼业务需求**:\n  {refined}")
            if raw_prompts:
                if isinstance(raw_prompts, list):
                    prompt_strs = []
                    for p in raw_prompts:
                        if isinstance(p, dict):
                            txt = p.get("prompt") or ""
                            if txt:
                                prompt_strs.append(f"  - \"{txt}\"")
                        elif isinstance(p, str) and p:
                            prompt_strs.append(f"  - \"{p}\"")
                    if prompt_strs:
                        m_lines.append("- **用户与委派指令记录**:\n" + "\n".join(prompt_strs))
                elif isinstance(raw_prompts, str):
                    m_lines.append(f"- **用户与委派指令记录**: {raw_prompts}")
        sections.append("\n".join(m_lines))

    # ── 4. 当前方案设计与架构方案 (Active Solution) ─────────────────────
    solutions = note_snapshot.get("solutions") or []
    if not solutions and note_snapshot.get("solution"):
        solutions = [note_snapshot["solution"]]
    if solutions:
        s_lines = [f"## {sec_idx}. 当前执行方案与架构设计 (Active Solution)"]
        sec_idx += 1
        has_any_sol = False
        for s in solutions:
            if not isinstance(s, dict) or not s.get("solution_content"):
                continue
            has_any_sol = True
            mid = s.get("mission_id")
            if mid and len(solutions) > 1:
                s_lines.append(f"### [方案] 任务: `{mid}`")
            s_lines.append(s["solution_content"])
        if has_any_sol:
            sections.append("\n\n".join(s_lines))

    # ── 5. 关键技术决策与约定 (Technical Decisions) ───────────────────
    decisions = note_snapshot.get("decisions") or []
    if decisions:
        d_lines = [f"## {sec_idx}. 关键技术决策与约定 (Technical Decisions)"]
        sec_idx += 1
        for d in decisions:
            did = d.get("decision_id", "")
            status = d.get("decision_status", "active")
            content = d.get("decision_content", "")
            rat = d.get("rationale")
            alt = d.get("alternatives")
            imp = d.get("impact")
            sup_reason = d.get("superseded_reason")
            d_lines.append(f"- **决策**: {content} (ID: `{did}`, 状态: `{status}`)")
            if rat:
                d_lines.append(f"  - 决策理由: {rat}")
            if alt:
                d_lines.append(f"  - 备选考量: {alt}")
            if imp:
                d_lines.append(f"  - 影响范围: {imp}")
            if sup_reason:
                d_lines.append(f"  - 废弃理由: {sup_reason}")
        sections.append("\n".join(d_lines))

    # ── 6. 工作区重点代码与上下文 (Workspace Context) ─────────────────
    ctx_list = note_snapshot.get("workspace_context") or []
    if ctx_list:
        w_lines = [f"## {sec_idx}. 工作区关键上下文与重点源码 (Workspace Context)"]
        sec_idx += 1
        for c in ctx_list:
            cid = c.get("context_id", "")
            stype = c.get("source_type", "file")
            desc = c.get("description", "")
            path = c.get("path")
            line_ranges = c.get("line_ranges")
            cmd_id = c.get("command_id")
            important = c.get("important_content") or c.get("core_content") or c.get("content")
            search_c = c.get("search_content")
            core_res = c.get("core_result_content")
            d_struct = c.get("directory_structure")
            urls = c.get("url_list") or c.get("reference_links")

            w_lines.append(f"### 上下文: {desc or stype} (`{cid}`)")
            if cmd_id:
                cmd_str = _format_command_execution(cmd_id, cmd_lookup)
                if cmd_str:
                    w_lines.append(cmd_str)

            # 源码反查
            if path and line_ranges:
                code_snippet = reverse_lookup_source_code(
                    path, line_ranges, current_working_directory=current_working_directory
                )
                if code_snippet:
                    w_lines.append(f"**关联源码反查**:\n{code_snippet}")
            elif path:
                w_lines.append(f"- **文件路径**: `{path}`")

            if important:
                w_lines.append(f"- **核心内容**: {important}")
            if search_c:
                w_lines.append(f"- **检索词**: `{search_c}`")
            if core_res:
                w_lines.append(f"- **检索结果摘要**: {core_res}")
            if d_struct:
                w_lines.append(f"- **目录结构**:\n```text\n{d_struct}\n```")
            if urls:
                if isinstance(urls, list):
                    w_lines.append("- **参考链接**: " + ", ".join(f"`{u}`" for u in urls))
                else:
                    w_lines.append(f"- **参考链接**: `{urls}`")
        sections.append("\n".join(w_lines))

    # ── 7. 代码探索与关键分析发现 (Discoveries) ────────────────────────
    discoveries = note_snapshot.get("discoveries") or []
    if discoveries:
        disc_lines = [f"## {sec_idx}. 探索与关键分析发现 (Discoveries)"]
        sec_idx += 1
        for d in discoveries:
            did = d.get("discovery_id", "")
            content = d.get("discovery_content", "")
            impact = d.get("impact")
            source_path = d.get("source_path")
            line_ranges = d.get("line_ranges")
            cmd_id = d.get("command_id")
            ref_url = d.get("reference_url")

            disc_lines.append(f"- **发现 (`{did}`)**: {content}")
            if impact:
                disc_lines.append(f"  - 影响面: {impact}")
            if cmd_id:
                cmd_str = _format_command_execution(cmd_id, cmd_lookup)
                if cmd_str:
                    disc_lines.append(f"  {cmd_str}")
            if source_path and line_ranges:
                code_snippet = reverse_lookup_source_code(
                    source_path, line_ranges, current_working_directory=current_working_directory
                )
                if code_snippet:
                    disc_lines.append(f"  **关联源码**:\n{code_snippet}")
            elif source_path:
                disc_lines.append(f"  - 涉及路径: `{source_path}`")
            if ref_url:
                disc_lines.append(f"  - 参考来源: `{ref_url}`")
        sections.append("\n".join(disc_lines))

    # ── 8. 问题排查与阻碍记录 (Issues) ────────────────────────────────
    issues = note_snapshot.get("issues") or []
    if issues:
        iss_lines = [f"## {sec_idx}. 问题排查与阻碍记录 (Issues)"]
        sec_idx += 1
        for iss in issues:
            iid = iss.get("issue_id", "")
            status = iss.get("status", "open")
            content = iss.get("issue_content", "")
            impact = iss.get("impact")
            res = iss.get("resolution")
            source_path = iss.get("source_path")
            line_ranges = iss.get("line_ranges")
            cmd_id = iss.get("command_id")

            st_icon = "🟢 [已解决]" if status == "resolved" else "🔴 [未解决]"
            iss_lines.append(f"### {st_icon} {content} (`{iid}`)")
            if impact:
                iss_lines.append(f"- **影响表现**: {impact}")
            if res:
                iss_lines.append(f"- **解决方案/根因**: {res}")
            if cmd_id:
                cmd_str = _format_command_execution(cmd_id, cmd_lookup)
                if cmd_str:
                    iss_lines.append(cmd_str)
            if source_path and line_ranges:
                code_snippet = reverse_lookup_source_code(
                    source_path, line_ranges, current_working_directory=current_working_directory
                )
                if code_snippet:
                    iss_lines.append(f"**相关源码**:\n{code_snippet}")
        sections.append("\n".join(iss_lines))

    # ── 9. 验证与测试结论 (Validations) ───────────────────────────────
    validations = note_snapshot.get("validations") or []
    if validations:
        v_lines = [f"## {sec_idx}. 验证与测试结论 (Validations)"]
        sec_idx += 1
        for v in validations:
            vid = v.get("validation_id", "")
            scope = v.get("validation_scope", "")
            conclusion = v.get("conclusion", "passed")
            content = v.get("validation_content", "")
            uncovered = v.get("uncovered_scope")
            cmd_id = v.get("command_id")
            source_path = v.get("source_path")
            line_ranges = v.get("line_ranges")

            conc_icon = "✅ 通过" if conclusion == "passed" else ("❌ 失败" if conclusion == "failed" else f"⚠️ {conclusion}")
            v_lines.append(f"- **验证范围**: {scope} ➔ **结论**: **{conc_icon}** (`{vid}`)")
            v_lines.append(f"  - 验证细节: {content}")
            if cmd_id:
                cmd_str = _format_command_execution(cmd_id, cmd_lookup)
                if cmd_str:
                    v_lines.append(f"  {cmd_str}")
            if uncovered:
                v_lines.append(f"  - 未覆盖范围: {uncovered}")
            if source_path and line_ranges:
                code_snippet = reverse_lookup_source_code(
                    source_path, line_ranges, current_working_directory=current_working_directory
                )
                if code_snippet:
                    v_lines.append(f"  **测试用例代码**:\n{code_snippet}")
        sections.append("\n".join(v_lines))

    # ── 10. 关键物理产物 (Artifacts) ──────────────────────────────────
    raw_artifacts = note_snapshot.get("artifacts") or []
    artifacts = fold_artifacts_net_changes(raw_artifacts)
    if artifacts:
        art_lines = [f"## {sec_idx}. 关键产物与文件 (Artifacts)"]
        sec_idx += 1
        for a in artifacts:
            aid = a.get("artifact_id", "")
            atype = a.get("type", "file")
            file_path = a.get("file_path") or a.get("path") or ""
            desc = a.get("description", "")
            edits = a.get("edits")
            content_lines = a.get("content_lines")
            content = a.get("content")
            source_path = a.get("source_path")
            target_path = a.get("target_path")

            if atype in ("file_modify", "modify"):
                art_lines.append(f"- 📝 **[文件修改 (file_modify)]**: `{file_path}` (`{aid}`)")
                parsed_edits: list[dict[str, Any]] = []
                if isinstance(edits, str):
                    try:
                        parsed_edits = json.loads(edits)
                    except Exception:
                        parsed_edits = []
                elif isinstance(edits, list):
                    parsed_edits = edits

                if parsed_edits:
                    for edit in parsed_edits:
                        if isinstance(edit, dict):
                            f_desc = edit.get("feature_desc")
                            s_line = edit.get("start_line")
                            e_line = edit.get("end_line")
                            replace_lines = edit.get("replace_lines")

                            loc_str = f" (行区间: `L{s_line}-L{e_line}`)" if (s_line is not None and e_line is not None) else ""
                            if f_desc:
                                art_lines.append(f"  - **功能说明{loc_str}**: {f_desc}")
                            elif loc_str:
                                art_lines.append(f"  - **修改位置**: {loc_str}")

                            if replace_lines:
                                r_list = replace_lines if isinstance(replace_lines, list) else str(replace_lines).splitlines()
                                lang = _detect_lang(file_path)
                                r_text = "\n".join(r_list)
                                art_lines.append(f"  - **替换写入代码 (replace_lines)**:\n    ```{lang}\n{r_text}\n    ```")
                elif desc:
                    art_lines.append(f"  - 说明: {desc}")

            elif atype in ("file_create", "create"):
                art_lines.append(f"- 📄 **[文件新建 (file_create)]**: `{file_path}` (`{aid}`)")
                if content_lines:
                    c_list = content_lines if isinstance(content_lines, list) else str(content_lines).splitlines()
                    lang = _detect_lang(file_path)
                    c_text = "\n".join(c_list[:30])
                    art_lines.append(f"  - **新建代码内容 ({len(c_list)} 行)**:\n    ```{lang}\n{c_text}\n    ```")
                elif content:
                    art_lines.append(f"  - 规模: 新建共 {len(str(content).splitlines())} 行")
                if desc:
                    art_lines.append(f"  - 说明: {desc}")

            elif atype in ("file_move", "move"):
                art_lines.append(f"- 🚚 **[文件移动/重命名 (file_move)]**: `{source_path or file_path}` ➔ `{target_path or file_path}` (`{aid}`)")
                if desc:
                    art_lines.append(f"  - 说明: {desc}")

            elif atype in ("file_delete", "delete"):
                art_lines.append(f"- 🗑️ **[文件删除 (file_delete)]**: `{file_path}` (`{aid}`)")
                if desc:
                    art_lines.append(f"  - 说明: {desc}")

            elif atype == "other":
                art_lines.append(f"- 📦 **[成果交付 (other)]**: {content or desc or '通用交付产物'} (`{aid}`)")
            else:
                art_lines.append(f"- **产物 [{atype}]**: `{file_path}` (`{aid}`)")
                if desc:
                    art_lines.append(f"  - 描述: {desc}")
        sections.append("\n".join(art_lines))

    # ── 11. 外部环境与持久状态变更 (External States) ───────────────────
    ext_states = note_snapshot.get("external_states") or []
    if ext_states:
        es_lines = [f"## {sec_idx}. 外部环境与服务状态 (External States)"]
        sec_idx += 1
        for es in ext_states:
            eid = es.get("external_state_id", "")
            ttype = es.get("target_type", "")
            tident = es.get("target_identifier", "")
            cchange = es.get("change_content", "")
            cstate = es.get("current_state", "")
            rev = es.get("reversibility", "reversible")
            rb = es.get("rollback_info")
            cmd_id = es.get("command_id")

            es_lines.append(f"- **[{ttype}] `{tident}`** (`{eid}`)")
            es_lines.append(f"  - 变更内容: {cchange}")
            es_lines.append(f"  - 当前状态: {cstate} (可逆性: `{rev}`)")
            if cmd_id:
                cmd_str = _format_command_execution(cmd_id, cmd_lookup)
                if cmd_str:
                    es_lines.append(f"  {cmd_str}")
            if rb:
                es_lines.append(f"  - 回滚指南: {rb}")
        sections.append("\n".join(es_lines))

    # ── 12. 多 Agent 协作拓扑与通信记录 (Multi-Agent Dialogs) ─────────
    agent_rels = note_snapshot.get("agent_relationships") or []
    agent_msgs = note_snapshot.get("agent_messages") or []
    if agent_rels or agent_msgs:
        dlg_lines = [f"## {sec_idx}. 多 Agent 协作拓扑与通信记录 (Multi-Agent Collaboration & Dialogs)"]
        sec_idx += 1
        rel_map = {r.get("target_agent_instance_id"): r for r in agent_rels}

        # 区分父 Agent 与子 Agent
        parent_ids = list(dict.fromkeys(
            [r.get("target_agent_instance_id") for r in agent_rels if r.get("target_agent_type") == "parent" and r.get("target_agent_instance_id")]
            + [m.get("target_agent_instance_id") for m in agent_msgs if m.get("target_agent_type") == "parent" and m.get("target_agent_instance_id")]
        ))
        child_ids = list(dict.fromkeys(
            [r.get("target_agent_instance_id") for r in agent_rels if r.get("target_agent_type") != "parent" and r.get("target_agent_instance_id")]
            + [m.get("target_agent_instance_id") for m in agent_msgs if m.get("target_agent_type") != "parent" and m.get("target_agent_instance_id")]
        ))

        # 1. 上级父 Agent 协作专线
        for pid in parent_ids:
            rel_info = rel_map.get(pid, {})
            st = rel_info.get("status", "unreleased")
            dlg_lines.append(f"### 🔼 上级父 Agent: `{pid}` (角色: `parent`, 状态: `{st}`)")

            p_msgs = [m for m in agent_msgs if m.get("target_agent_instance_id") == pid]
            if p_msgs:
                for msg in p_msgs:
                    mtype = msg.get("message_type", "send")
                    mbody = msg.get("message", "")
                    ts = msg.get("created_at", "")
                    prefix = "📤 **[向父 Agent 汇报]**" if mtype == "send" else "📥 **[收到父 Agent 指令]**"
                    ts_label = f"`{ts}` " if ts else ""
                    dlg_lines.append(f"- {ts_label}{prefix}: {mbody}")
            else:
                dlg_lines.append("- *(暂无通信消息流水)*")

        # 2. 下级协作子 Agent 专线
        for cid in child_ids:
            rel_info = rel_map.get(cid, {})
            role = rel_info.get("target_agent_type", "child")
            st = rel_info.get("status", "unreleased")
            dlg_lines.append(f"### 协作子 Agent: `{cid}` (角色: `{role}`, 状态: `{st}`)")
            delegation = rel_info.get("delegation_content")
            if delegation:
                dlg_lines.append(f"- **委派任务**: {delegation}")

            child_msgs = [m for m in agent_msgs if m.get("target_agent_instance_id") == cid]
            if child_msgs:
                for msg in child_msgs:
                    mtype = msg.get("message_type", "send")
                    mbody = msg.get("message", "")
                    ts = msg.get("created_at", "")
                    prefix = "📤 **[发往子 Agent]**" if mtype == "send" else "📥 **[收到子 Agent 回复]**"
                    ts_label = f"`{ts}` " if ts else ""
                    dlg_lines.append(f"- {ts_label}{prefix}: {mbody}")
            else:
                dlg_lines.append("- *(暂无通信消息流水)*")
        sections.append("\n".join(dlg_lines))

    # ── 13. 外部远程对等 Agent 通信记录 (Remote Agent Messages) ────────
    remote_msgs = note_snapshot.get("remote_agent_messages") or []
    if remote_msgs:
        rem_lines = [f"## {sec_idx}. 外部远程 Agent 对等通信流水 (Remote Agent Messages)"]
        sec_idx += 1
        remote_groups: dict[str, list[dict[str, Any]]] = {}
        for rm in remote_msgs:
            rid = rm.get("remote_agent_instance_id") or "unknown_remote_agent"
            remote_groups.setdefault(rid, []).append(rm)

        for rid, msgs in remote_groups.items():
            rem_lines.append(f"### 远程对等 Agent 实例 ID: `{rid}`")
            for rm in msgs:
                mtype = rm.get("message_type", "send")
                mbody = rm.get("message", "")
                ts = rm.get("created_at", "")
                prefix = "📤 **[发往远程 Agent]**" if mtype == "send" else "📥 **[接收自远程 Agent]**"
                ts_label = f"`{ts}` " if ts else ""
                rem_lines.append(f"- {ts_label}{prefix}: {mbody}")
        sections.append("\n".join(rem_lines))

    # ── 14. 补充现场信息 (Additional Context) ──────────────────────────
    known_keys = {
        "task_id", "agent_instance_id", "progress", "todos", "missions",
        "solution", "decisions", "workspace_context", "discoveries",
        "issues", "validations", "artifacts", "external_states",
        "agent_relationships", "agent_messages", "remote_agent_messages",
        "commands",
    }
    extra_keys = [k for k in note_snapshot.keys() if k not in known_keys and note_snapshot[k]]
    if extra_keys:
        ext_lines = [f"## {sec_idx}. 补充现场信息 (Additional Context)"]
        sec_idx += 1
        for k in extra_keys:
            val = note_snapshot[k]
            if isinstance(val, list):
                ext_lines.append(f"### {k}")
                for item in val:
                    if isinstance(item, dict):
                        desc_parts = [f"{ik}: {iv}" for ik, iv in item.items()]
                        ext_lines.append("- " + ", ".join(desc_parts))
                    else:
                        ext_lines.append(f"- {item}")
            elif isinstance(val, dict):
                ext_lines.append(f"### {k}")
                for ik, iv in val.items():
                    ext_lines.append(f"- **{ik}**: {iv}")
            else:
                ext_lines.append(f"- **{k}**: {val}")
        sections.append("\n".join(ext_lines))

    if sec_idx == 1:
        sections.append("*(当前暂无持久化任务手记记录)*")

    return "\n\n".join(sections)
