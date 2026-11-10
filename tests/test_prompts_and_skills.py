"""提示词与技能注册表测试。

提示词是**带版本的资产**：改一个字就会改变 SHA256，进而让缓存失效、
让结果变化可归因。因此这些文件必须结构合法、id 唯一、变量可渲染。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

from agent.config import PROJECT_ROOT
from agent.prompts.registry import PromptRegistry
from agent.runtime.errors import ConfigError

if TYPE_CHECKING:  # 仅用于类型注解，避免运行期循环导入
    from agent.skills.registry import SkillRegistry

PROMPTS_DIR = PROJECT_ROOT / "domain" / "prompts"
SKILLS_DIR = PROJECT_ROOT / "domain" / "skills"


@pytest.fixture(scope="module")
def registry() -> PromptRegistry:
    reg = PromptRegistry()
    reg.load_dir(PROMPTS_DIR)
    return reg


# ==========================================================================
# 提示词
# ==========================================================================


def test_prompts_directory_exists() -> None:
    assert PROMPTS_DIR.is_dir(), f"缺少提示词目录：{PROMPTS_DIR}"


def test_all_prompts_load(registry: PromptRegistry) -> None:
    """每个 .md 都必须能被解析为合法 Prompt（含 front-matter）。"""
    md_files = sorted(PROMPTS_DIR.glob("*.md"))
    assert md_files, "domain/prompts/ 下没有任何提示词文件"
    assert len(registry) == len(md_files), (
        f"加载数量 {len(registry)} 与文件数 {len(md_files)} 不符，"
        "可能有文件缺少 front-matter 或 id 重复"
    )


def test_prompt_ids_are_unique(registry: PromptRegistry) -> None:
    ids = [p.id for p in (registry.get(i) for i in registry.ids)]
    assert len(ids) == len(set(ids))


def test_every_prompt_has_version_and_role(registry: PromptRegistry) -> None:
    for pid in registry.ids:
        p = registry.get(pid)
        assert p.version, f"{pid} 缺少 version"
        assert p.role, f"{pid} 缺少 role"
        assert p.template.strip(), f"{pid} 正文为空"


def test_prompt_roles_are_known(registry: PromptRegistry, project_root: Path) -> None:
    """提示词声明的 role 必须是 models.yaml 中已配置的角色。"""
    from agent.config import load_config

    cfg = load_config("models")
    known = set((cfg.get("roles") or {}).keys())
    for pid in registry.ids:
        role = registry.get(pid).role
        assert role in known, f"{pid} 的 role='{role}' 不在 models.yaml 的 roles 中（可用：{sorted(known)}）"


def test_fingerprint_is_stable(registry: PromptRegistry) -> None:
    """同一份提示词两次取指纹必须相同 —— 复现比对依赖这一点。"""
    a = registry.fingerprint()
    b = registry.fingerprint()
    assert a == b
    assert all(len(v) == 64 for v in a.values())


def test_changing_prompt_changes_hash() -> None:
    """改一个字就必须改变 SHA256，否则缓存不会失效、变更无法归因。"""
    reg1 = PromptRegistry()
    reg1.load_dir(PROMPTS_DIR)
    original = reg1.get("r1.extract_financial_facts")
    h1 = original.sha256

    from agent.prompts.registry import Prompt

    modified = Prompt(
        id=original.id,
        version=original.version,
        role=original.role,
        description=original.description,
        template=original.template + "\n（追加一行）",
    )
    assert modified.sha256 != h1


def test_version_bump_changes_hash() -> None:
    from agent.prompts.registry import Prompt

    a = Prompt(id="x", version="1.0.0", role="default", description="", template="T")
    b = Prompt(id="x", version="1.0.1", role="default", description="", template="T")
    assert a.sha256 != b.sha256


def test_render_substitutes_variables(registry: PromptRegistry) -> None:
    p = registry.get("r1.extract_financial_facts")
    out = p.render(page=3, page_text="营业收入 100 万元", target_metrics="revenue")
    assert "第 3 页" in out
    assert "营业收入 100 万元" in out
    assert "{page}" not in out


def test_render_rejects_missing_variable(registry: PromptRegistry) -> None:
    """缺变量必须报错，不能静默留下 {var} 占位符给模型看。"""
    p = registry.get("r1.extract_financial_facts")
    with pytest.raises(ConfigError, match="缺少变量"):
        p.render(page=1)


def test_load_file_rejects_missing_front_matter(tmp_path: Path) -> None:
    bad = tmp_path / "bad.md"
    bad.write_text("没有 front-matter 的正文", encoding="utf-8")
    with pytest.raises(ConfigError, match="front-matter"):
        PromptRegistry().load_file(bad)


def test_load_file_rejects_missing_id(tmp_path: Path) -> None:
    bad = tmp_path / "noid.md"
    bad.write_text("---\nversion: 1.0.0\n---\n正文", encoding="utf-8")
    with pytest.raises(ConfigError, match="缺少字段 id"):
        PromptRegistry().load_file(bad)


def test_get_unknown_prompt_raises(registry: PromptRegistry) -> None:
    with pytest.raises(ConfigError, match="未注册"):
        registry.get("no.such.prompt")


def test_prompt_contains_anti_hallucination_rules(registry: PromptRegistry) -> None:
    """★ 提示词层面的防幻觉约束必须存在。

    代码闸门（quote 回读）是硬保障，但提示词也要明确告知模型规则 ——
    两者是纵深防御，缺一不可。
    """
    for pid in ("r1.extract_financial_facts", "r1.vlm_judge_field"):
        text = registry.get(pid).template
        assert "逐字" in text or "原样" in text, f"{pid} 未要求逐字照抄"
        assert "禁止" in text or "不得" in text, f"{pid} 未包含禁止性约束"
        assert "quote" in text, f"{pid} 未要求提供 quote"


# ==========================================================================
# 技能
# ==========================================================================


def test_skills_exist_for_all_three_requirements() -> None:
    for name in ("r1_extraction", "r4_valuation", "r5_audit"):
        f = SKILLS_DIR / name / "SKILL.md"
        assert f.is_file(), f"缺少技能定义：{f}"


def test_skill_files_have_required_sections() -> None:
    required = ("考察点", "输出契约")
    for name in ("r1_extraction", "r4_valuation", "r5_audit"):
        text = (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
        for section in required:
            assert section in text, f"{name}/SKILL.md 缺少『{section}』段落"


def test_r5_skill_documents_false_positive_controls() -> None:
    """R5 技能必须写明误报控制措施 —— 这是该需求的评分关键。"""
    text = (SKILLS_DIR / "r5_audit" / "SKILL.md").read_text(encoding="utf-8")
    assert "误报" in text
    assert "派生指标" in text
    assert "陷阱" in text or "陷阱项" in text


# ==========================================================================
# 技能注册表（Skill 是赛题要求提交的核心模块之一）
# ==========================================================================


@pytest.fixture(scope="module")
def skill_registry() -> "SkillRegistry":
    from agent.skills.registry import SkillRegistry

    reg = SkillRegistry()
    reg.load_dir(SKILLS_DIR)
    return reg


def test_skill_registry_loads_all_skills(skill_registry: "SkillRegistry") -> None:
    md_files = sorted(SKILLS_DIR.rglob("SKILL.md"))
    assert md_files, "domain/skills/ 下没有任何 SKILL.md"
    assert len(skill_registry) == len(md_files), (
        f"加载数量 {len(skill_registry)} 与文件数 {len(md_files)} 不符，"
        "可能有文件缺少 skill_id 或 id 重复"
    )


def test_every_skill_has_id_version_and_contract(skill_registry: "SkillRegistry") -> None:
    for sid in skill_registry.ids:
        s = skill_registry.get(sid)
        assert s.version, f"{sid} 缺少版本号"
        assert s.contract, f"{sid} 未声明输出契约"
        assert s.requirement, f"{sid} 未声明对应赛题"
        assert s.doc.strip(), f"{sid} 正文为空"


def test_skill_fingerprint_is_stable_and_full_length(
    skill_registry: "SkillRegistry",
) -> None:
    """技能指纹与提示词指纹同样要写入 manifest，因此必须稳定且为完整 SHA256。"""
    a = skill_registry.fingerprint()
    b = skill_registry.fingerprint()
    assert a == b
    assert all(len(v) == 64 for v in a.values())
    assert a, "技能指纹为空"


def test_changing_skill_changes_hash() -> None:
    from agent.skills.registry import Skill, SkillRegistry

    reg = SkillRegistry()
    reg.load_dir(SKILLS_DIR)
    original = reg.get("r1.structured_extraction")
    h1 = original.sha256
    modified = Skill(
        id=original.id,
        version=original.version,
        title=original.title,
        requirement=original.requirement,
        tools=list(original.tools),
        prompts=list(original.prompts),
        contract=original.contract,
        doc=original.doc + "\n（追加一行）",
    )
    assert modified.sha256 != h1


def test_skill_bindings_resolve_to_real_tools_and_prompts(
    skill_registry: "SkillRegistry",
) -> None:
    """★ 技能声明的工具与提示词必须真实存在。

    这是「项目说明与实际实现一致」的机器校验。反例：技能文档写
    「绑定工具 ``table.check_arithmetic``」，而代码里这个工具被改名或删除 ——
    运行期不会有任何报错，文档会继续描述一个不存在的系统。
    评审只要点开 `agent tools` 对一眼就会发现。所以这里必须是硬断言。
    """
    from agent.cli import _load_tool_modules
    from agent.prompts.registry import PromptRegistry
    from agent.tools.registry import build_registry

    prompts = PromptRegistry()
    prompts.load_dir(PROMPTS_DIR)
    tools = build_registry(*_load_tool_modules())

    problems = skill_registry.validate_bindings(tools=tools.names, prompts=prompts.ids)
    assert not problems, "技能绑定与实现脱节：" + "; ".join(
        f"{p['skill']}.{p['kind']}={p['ref']} 不存在" for p in problems
    )


def test_binding_check_really_detects_drift(tmp_path: Path) -> None:
    """★ 证明上面那条校验**真的会失败** —— 否则它只是一句安慰。

    注入一个不存在的工具名，校验必须报出它；
    否则说明 ``_unmatched`` / 通配符处理写错了（例如把 ``doc.*`` 当成精确匹配）。
    """
    from agent.skills.registry import SkillRegistry

    d = tmp_path / "fake"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "# Skill: 假的\n\n"
        "| 项 | 值 |\n|---|---|\n"
        "| skill_id | `fake.skill` |\n"
        "| 版本 | `1.0.0` |\n"
        "| 绑定工具 | `ghost.tool`、`doc.*` |\n"
        "| 绑定提示词 | `ghost.prompt` |\n",
        encoding="utf-8",
    )
    reg = SkillRegistry()
    reg.load_dir(tmp_path)
    assert len(reg) == 1

    # 通配符 `doc.*` 应当被展开匹配，精确名 `ghost.tool` 应当报缺失
    problems = reg.validate_bindings(tools=["doc.register", "doc.page_text"], prompts=[])
    kinds = {(p["kind"], p["ref"]) for p in problems}
    assert ("tool", "ghost.tool") in kinds, "未检出缺失工具 —— 校验形同虚设"
    assert ("prompt", "ghost.prompt") in kinds, "未检出缺失提示词"
    assert ("tool", "doc.*") not in kinds, "通配符被误判为缺失"


def test_meta_table_parsing_ignores_body_tables(tmp_path: Path) -> None:
    """正文里的两列表格不能被误当成元信息。"""
    from agent.skills.registry import SkillRegistry

    d = tmp_path / "s"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "# Skill: 标题\n\n"
        "| 项 | 值 |\n|---|---|\n"
        "| skill_id | `x.y` |\n"
        "| 版本 | `2.1.0` |\n"
        "| 绑定工具 | `doc.register` |\n"
        "| 输出契约 | `FactSet` |\n\n"
        "## 考察点与对策\n\n"
        "| 官方考察点 | 我们的对策 |\n|---|---|\n"
        "| 完整性 | 别名表 |\n",
        encoding="utf-8",
    )
    reg = SkillRegistry()
    s = reg.load_file(d / "SKILL.md")
    assert s.id == "x.y"
    assert s.version == "2.1.0"
    assert s.tools == ["doc.register"]
    assert s.contract == "FactSet"
    # 正文保留，且元信息表不重复出现在正文里
    assert "考察点与对策" in s.doc
    assert "skill_id" not in s.doc


def test_skill_registry_rejects_duplicate_id(tmp_path: Path) -> None:
    from agent.skills.registry import SkillRegistry

    body = (
        "# Skill: 重复\n\n| 项 | 值 |\n|---|---|\n| skill_id | `dup.skill` |\n"
        "| 版本 | `1.0.0` |\n| 输出契约 | `X` |\n"
    )
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "SKILL.md").write_text(body, encoding="utf-8")
    (tmp_path / "b" / "SKILL.md").write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError, match="技能 id 重复"):
        SkillRegistry().load_dir(tmp_path)


def test_skill_missing_id_is_rejected(tmp_path: Path) -> None:
    from agent.skills.registry import SkillRegistry

    (tmp_path / "SKILL.md").write_text("# Skill: 没有 id\n\n正文\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="缺少 skill_id"):
        SkillRegistry().load_file(tmp_path / "SKILL.md")


def test_run_context_carries_skills_registry() -> None:
    """运行上下文必须带上技能注册表，否则 manifest 里的 skills 指纹永远是空的。"""
    from agent.runtime.loop import RunContext

    ctx = RunContext(run_id="t", run_dir=Path("."), pipeline="r1_extract")
    assert ctx.skills is not None
    assert ctx.skills.fingerprint() == {}


# ==========================================================================
# 配置一致性
# ==========================================================================


def test_metrics_config_is_loadable_and_well_formed() -> None:
    """指标表是 R1/R4/R5 三处共用的定义，必须结构完整。"""
    from agent.config import load_config

    cfg = load_config("metrics")
    metrics = cfg.get("metrics") or []
    assert len(metrics) >= 30, f"指标数量偏少（{len(metrics)}），可能配置损坏"

    canonicals = [m["canonical"] for m in metrics]
    assert len(canonicals) == len(set(canonicals)), "存在重复的 canonical 名"

    valid_units = {"元", "股", "%", "元每股", "倍"}
    for m in metrics:
        assert m.get("unit") in valid_units, f"{m['canonical']} 的单位 {m.get('unit')!r} 不在允许集合内"
        assert m.get("aliases"), f"{m['canonical']} 没有任何别名"
        assert m.get("label"), f"{m['canonical']} 缺少 label"

    assert cfg.get("calibers"), "缺少口径标签体系定义"


def test_assertions_config_matches_code() -> None:
    """configs/assertions.yaml 声明的断言必须有对应实现 —— 避免声明与实现脱节。"""
    from agent.config import load_config
    from agent.validate import assertions as A

    cfg = load_config("assertions")
    declared = {a["code"] for a in cfg.get("assertions", [])}
    implemented = {
        "A1": A.assert_balance_sheet,
        "A2": A.assert_cash_flow,
        "A3": A.assert_retained_earnings,
        "A4": A.assert_fcff_recompute,
        "A5": A.assert_income_statement,
        "A6": A.assert_valuation_sanity,
    }
    assert declared == set(implemented), (
        f"声明与实现不一致：声明 {sorted(declared)}，实现 {sorted(implemented)}"
    )
    assert all(callable(fn) for fn in implemented.values())


def test_routing_config_thresholds_are_sane() -> None:
    """路由阈值必须与排版常识一致（text_coverage 天然偏低）。"""
    from agent.config import load_config

    cfg = load_config("routing")
    tc = cfg.get("text_coverage_threshold", 0)
    assert 0 < tc <= 0.3, (
        f"text_coverage_threshold={tc} 不合理：正常 A4 正文页的文本覆盖率"
        "只有 0.2–0.4，阈值过高会导致所有页面被误判为非文本页"
    )
    assert cfg.get("min_chars_for_text_only", 0) >= 100, "文本页的字符数阈值过低会引入噪声"
    assert 0 < cfg.get("quote_fuzzy_threshold", 0) < 1


def test_models_config_has_required_roles() -> None:
    from agent.config import load_config

    cfg = load_config("models")
    roles = cfg.get("roles") or {}
    for role in ("default", "extractor", "critic", "vision"):
        assert role in roles, f"models.yaml 缺少角色 {role}"
    assert roles["vision"].get("supports_vision") is True, (
        "vision 角色必须声明 supports_vision —— 三层解析引擎的第三层依赖它"
    )
    assert roles["default"].get("temperature") == 0.0, (
        "默认温度必须为 0，否则结果不可复现"
    )


def test_thinking_disabled_for_reproducible_roles() -> None:
    """★ 可复现性关键约束：产出数字的角色必须关闭思考模式。

    为什么这条测试很重要：DeepSeek 官方文档明确说明
    **思考模式下 temperature 不生效**，而思维链本身是随机的。
    若对 extractor / vision 开启思考，同一输入两次运行可能给出不同答案，
    「运行结果可复现」这一评分项直接失守 —— 而且不会有任何报错提示。

    因此只有 critic / planner 允许开启思考：它们产出的是判断与计划，
    不直接作为数字写入产物，且结论会经过确定性校验兜底。
    """
    from agent.config import load_config

    roles = load_config("models").get("roles") or {}
    for role in ("default", "extractor", "vision", "writer"):
        assert roles[role].get("thinking", False) is False, (
            f"角色 {role} 开启了思考模式 —— 思考模式下 temperature 不生效，"
            "会破坏结果可复现性。请显式设置 thinking: false"
        )
    # critic 需要最强推理，允许开启（其结论不直接成为数字）
    assert roles["critic"].get("thinking") is True


def test_max_tokens_leave_room_for_reasoning() -> None:
    """开启思考的角色必须给足 max_tokens。

    实测教训：思考消耗计入 completion_tokens。开着思考却只给 512 token 时，
    思维链会把配额耗尽，``content`` 返回空字符串而 ``finish_reason`` 仍是
    ``stop`` —— 表现为「模型没回答但不报错」。网关已加显式检测，
    但配置上也必须留出余量。
    """
    from agent.config import load_config

    roles = load_config("models").get("roles") or {}
    for name, spec in roles.items():
        if spec.get("thinking"):
            assert spec.get("max_tokens", 0) >= 16384, (
                f"角色 {name} 开启了思考但 max_tokens={spec.get('max_tokens')} 偏小，"
                "可能被思维链耗尽导致空响应"
            )


def test_image_token_estimate_is_measured() -> None:
    """图片 token 折算值必须已实测回填（P0 的交付物之一）。

    这个数字决定了「是否敢对全页做 VLM 裁决」的成本判断，
    留 null 会让成本估算退化为拍脑袋。
    """
    from agent.config import load_config

    cfg = load_config("models").get("image_tokens") or {}
    val = cfg.get("per_page_a4_200dpi")
    assert isinstance(val, int) and val > 0, (
        "image_tokens.per_page_a4_200dpi 未实测回填。"
        "请运行：python tools/p0_probe.py --tokens --image <页面图>"
    )


def test_config_files_contain_no_secrets() -> None:
    """★ 合规红线：配置文件里绝不能出现真实 API Key。"""
    import re

    from agent.config import CONFIG_DIR

    suspicious = re.compile(r"sk-[A-Za-z0-9]{16,}")
    for f in CONFIG_DIR.rglob("*.yaml"):
        text = f.read_text(encoding="utf-8")
        assert not suspicious.search(text), f"{f.name} 中疑似包含真实 API Key"
        # 只允许 ${VAR} 或 ${VAR:-default} 形式
        for m in re.finditer(r"api_key:\s*(\S+)", text):
            assert m.group(1).startswith("${"), (
                f"{f.name} 的 api_key 必须用环境变量占位，实际为 {m.group(1)!r}"
            )


def test_yaml_configs_parse() -> None:
    """所有 YAML 必须能被解析（含 % 等特殊字符的引号问题）。"""
    from agent.config import CONFIG_DIR

    for f in CONFIG_DIR.rglob("*.yaml"):
        try:
            yaml.safe_load(f.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            pytest.fail(f"{f.name} YAML 解析失败：{exc}")
