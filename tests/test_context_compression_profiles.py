from agent_runtime.context_compression.models import CompressionJob, SectionItem
from agent_runtime.context_compression.compression_service import _render_draft_markdown
from agent_runtime.context_compression.profiles import (
    DEFAULT_ACTIVE_PROFILE_ID,
    PROFILE_SECTION_PARALLEL_V1,
    PROFILE_TASK_RESUME_V2,
    get_profile,
    list_profiles,
    normalize_profile_id,
)
from agent_runtime.context_compression.default_prompts import get_default_worker_prompt


def test_builtin_profiles_registered():
    profiles = {profile.profile_id: profile for profile in list_profiles()}
    assert PROFILE_SECTION_PARALLEL_V1 in profiles
    assert PROFILE_TASK_RESUME_V2 in profiles
    assert normalize_profile_id(None) == DEFAULT_ACTIVE_PROFILE_ID


def test_profile_b_stage_order():
    profile = get_profile(PROFILE_TASK_RESUME_V2)
    assert [stage.stage_id for stage in profile.stages] == ["scope_extract", "dependent_extract"]
    assert profile.stages[0].mode == "serial"
    assert profile.stages[1].mode == "parallel"
    assert len(profile.stages[0].worker_names) == 1
    assert len(profile.stages[1].worker_names) == 5


def test_profile_b_default_prompts_load():
    text = get_default_worker_prompt(
        profile_id=PROFILE_TASK_RESUME_V2,
        worker_name="TaskRequirementsRemainingGoalsWorker",
    )
    assert "任务需求与剩余目标" in text or "任务" in text
    assert len(text) > 1000


def test_ordered_section_concat_assembly():
    profile = get_profile(PROFILE_TASK_RESUME_V2)
    job = CompressionJob(
        job_id="j1",
        task_id="t1",
        thread_id="th1",
        agent_id="a1",
        agent_instance_id="i1",
        root_conversation_id="r1",
        source_conversation_id="s1",
        compression_index=1,
        profile_id=profile.profile_id,
        profile_version=profile.version,
        assembly_strategy=profile.assembly.strategy,
        section_items=[
            SectionItem(section="existing_work_artifacts", content="artifact-body"),
            SectionItem(section="task_requirements_remaining_goals", content="goal-body"),
            SectionItem(section="task_state_execution_plan", content="plan-body"),
        ],
    )
    markdown = _render_draft_markdown(job, profile=profile)
    assert markdown.index("用户任务、要求与剩余目标") < markdown.index("任务状态与执行计划")
    assert markdown.index("任务状态与执行计划") < markdown.index("已有工作成果与产物")
    assert "goal-body" in markdown
    assert "plan-body" in markdown
    assert "artifact-body" in markdown
