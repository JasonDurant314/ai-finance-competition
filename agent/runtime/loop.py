"""调度主循环 —— 两级结构。

**宏观：Plan DAG（确定性）**
    由 ``pipelines/<name>/pipeline.yaml`` 声明的阶段序列驱动。
    阶段之间通过**文件产物**衔接，不传内存对象。这保证了
    「同输入同输出」，也让每个阶段可以独立调试、独立重跑。

**微观：Agent Loop（有界）**
    仅在单个阶段内部使用，带最小 ReAct 语义与自我修复。
    由 :class:`StageAgent` 提供。

调度器职责：
    * 阶段编排、断点续跑、失败降级
    * 预算执行（超限转 partial 而非崩溃）
    * 全流程事件记录
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from agent.config import (
    CONFIG_DIR,
    PROJECT_ROOT,
    load_models_config,
    load_tools_config,
    load_yaml,
)
from agent.evidence.models import RunManifest, utcnow
from agent.evidence.store import EvidenceStore, FactLedger
from agent.model.cache import ResponseCache
from agent.model.gateway import ModelGateway, RoleConfig
from agent.model.providers.mock import MockProvider
from agent.model.providers.openai_compat import OpenAICompatProvider
from agent.prompts.registry import PromptRegistry
from agent.skills.registry import SkillRegistry
from agent.tools.fsguard import FileGuard
from agent.runtime.budget import Budget
from agent.runtime.errors import (
    BudgetExceededError,
    ConfigError,
    HarnessError,
    StageSkipped,
)
from agent.trace.tracer import Tracer, file_sha256, payload_hash
from agent.validate.schema import has_jsonschema

StageFn = Callable[["RunContext"], dict[str, Any] | None]


# ==========================================================================
# 阶段与计划
# ==========================================================================


@dataclass
class Stage:
    """一个流水线阶段。``fn_ref`` 形如 ``pipelines.r1_extract.ingest:run``。"""

    name: str
    fn_ref: str
    description: str = ""
    optional: bool = False
    """True 表示该阶段失败不阻断后续（降级路径）。"""
    timeout_s: float | None = None
    produces: list[str] = field(default_factory=list)
    """该阶段产出的相对路径，用于复现校验。"""

    def resolve(self) -> StageFn:
        if ":" not in self.fn_ref:
            raise ConfigError(f"阶段 {self.name} 的 fn_ref 格式应为 'module:function'", got=self.fn_ref)
        mod_name, fn_name = self.fn_ref.split(":", 1)
        try:
            module = importlib.import_module(mod_name)
        except ImportError as exc:
            raise ConfigError(
                f"阶段 {self.name} 无法导入模块 {mod_name}", error=str(exc)
            ) from exc
        fn = getattr(module, fn_name, None)
        if fn is None:
            raise ConfigError(f"模块 {mod_name} 中不存在函数 {fn_name}")
        if not callable(fn):
            raise ConfigError(f"{self.fn_ref} 不是可调用对象")
        return fn


@dataclass
class Plan:
    """阶段序列。"""

    name: str
    stages: list[Stage]
    description: str = ""

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Plan":
        cfg = load_yaml(path)
        name = cfg.get("name") or Path(path).parent.name
        stages = [
            Stage(
                name=s["name"],
                fn_ref=s["run"],
                description=s.get("description", ""),
                optional=bool(s.get("optional", False)),
                timeout_s=s.get("timeout_s"),
                produces=list(s.get("produces", [])),
            )
            for s in cfg.get("stages", [])
        ]
        if not stages:
            raise ConfigError("pipeline.yaml 未定义任何阶段", path=str(path))
        return cls(name=name, stages=stages, description=cfg.get("description", ""))

    @classmethod
    def for_pipeline(cls, pipeline: str, *, root: str | Path | None = None) -> "Plan":
        base = Path(root) if root else PROJECT_ROOT / "pipelines"
        return cls.from_yaml(base / pipeline / "pipeline.yaml")


# ==========================================================================
# 运行上下文
# ==========================================================================


@dataclass
class RunContext:
    """一次运行的全部状态。传给每个阶段函数。"""

    run_id: str
    run_dir: Path
    pipeline: str
    dry_run: bool = False

    upstream_facts_run: Path | None = None
    """上游 R1 运行目录。R4 / R5 通过它消费事实库，
    而不是重新解析文档 —— 这是三处数字一致性的结构保证。"""

    budget: Budget = field(default_factory=Budget)
    tracer: Tracer | None = None
    gateway: ModelGateway | None = None
    prompts: PromptRegistry = field(default_factory=PromptRegistry)
    skills: SkillRegistry = field(default_factory=SkillRegistry)
    """领域技能注册表。赛题要求提交的核心模块之一（Skill）；
    与 ``prompts`` 对称 —— 提示词管「对模型说什么」，技能管「这件事该怎么做」。
    指纹写入 manifest，因此改一条技能声明同样会体现在复现比对里。"""
    store: EvidenceStore = field(default_factory=EvidenceStore)
    ledger: FactLedger | None = None
    manifest: RunManifest | None = None
    configs: dict[str, Any] = field(default_factory=dict)
    fs: FileGuard | None = None
    """本 run 的文件访问器。**每一个**读写点位都经它走，于是
    `trace.jsonl` 里逐条留下 `file_access`（路径 + 模式 + sha256 + 阶段）。

    留 None 是为了兼容测试里手搭的裸上下文：`guard()` 会按目录约定当场补一个，
    所以没有哪条代码路径能绕开它 —— 这正是「完整记录文件访问」的落点。
    由 `build_context` 显式装配的那个才是正式的那份（读根含材料目录与上游 run，
    写根只有本 run 目录）。"""

    # 阶段间通过目录传递产物
    @property
    def facts_dir(self) -> Path:
        return self.run_dir / "facts"

    @property
    def valuation_dir(self) -> Path:
        return self.run_dir / "valuation"

    @property
    def audit_dir(self) -> Path:
        return self.run_dir / "audit"

    @property
    def pages_dir(self) -> Path:
        return self.run_dir / "pages"

    @property
    def cache_dir(self) -> Path:
        return self.run_dir / "cache"

    @property
    def report_dir(self) -> Path:
        return self.run_dir / "report"

    @property
    def web_dir(self) -> Path:
        return self.run_dir / "web"

    def sub(self, name: str) -> Path:
        p = self.run_dir / name
        p.mkdir(parents=True, exist_ok=True)
        return p

    # ------------------------------------------------------------ 文件访问

    def guard(self) -> FileGuard:
        """取本 run 的文件访问器；没有就按目录约定当场补一个。

        **为什么是方法而不是直接用 `ctx.fs`**：`ctx.fs` 是可选的（测试手搭的
        裸上下文不给），而流水线里每一个读写点都必须留痕。写成
        `ctx.fs.write_json(...)` 就要求每处先判空，漏判一处就是一条静默的
        无痕访问 —— 而「静默」正是这一轮要根治的毛病。这里一步兜住：
        没装配就现建一个，之后一直复用。
        """
        if self.fs is None:
            self.fs = build_file_guard(self)
        return self.fs

    def read_roots(self) -> list[Path]:
        """允许读的根。**材料目录 + 配置目录 + 本 run 与上游 run 目录**，别无其他。

        「受控数据环境」落到代码上就是这一句话：流水线能读什么由这里穷举，
        名单外的路径会抛 `SandboxViolation` 而不是静默放行 —— 实测已经靠它
        抓出两次漏配（web 自测的 `inbox/raw/` 与 `inbox/market/`）。

        ★ 这里**不写 `PROJECT_ROOT`**。写上它等于什么都能读，这道闸门就白设了。

        名单来源是 `configs["paths"]`：CLI 会把 `--data <目录>` 摊成
        `{"data": <目录>, "raw": <目录>/raw, "golden": …, "drafts": …, "samples": …}`
        （`agent/cli.py`）。**整份摊开都要收**，不能只收 `raw` ——
        市场利率缓存在 `<目录>/market/`、可比公司快照在 `<目录>/comps/`，
        它们既不在 `raw/` 下，也不在项目自带的 `data/` 里。少收一个子目录
        就是一次「闸门放行、读取失败」的对偶错误：直接判越权、跑不完。

        相对路径不在这里做二次解释：`FileGuard` 与流水线都按 CWD 解析
        （`raw_dir()` 返回的就是 `Path("data")/"raw"`），两边口径一致才不会
        出现自相矛盾的组合。
        """
        roots = [self.run_dir, CONFIG_DIR]
        if self.upstream_facts_run is not None:
            roots.insert(0, Path(self.upstream_facts_run))
        declared = [
            v for k, v in (self.configs.get("paths") or {}).items()
            if k in ("data", "raw", "golden", "drafts", "samples") and v
        ]
        if not declared:
            declared = [self.configs.get("data_dir") or (PROJECT_ROOT / "data")]
        for d in declared:
            p = Path(d)
            roots.append(p if p.is_absolute() else PROJECT_ROOT / p)
        return roots

    # ------------------------------------------------------------ 上游产物

    def build_stamp(self) -> datetime:
        """本次运行的**统一构建时间戳**。

        为什么必须统一：产物里若各自调用 ``utcnow()``，同一输入两次运行会
        产生不同的时间戳，产物哈希永远无法一致，「可复现」就成了空话。
        因此所有产物时间戳一律取 ``manifest.started_at``。
        """
        if self.manifest is not None:
            return self.manifest.started_at
        return utcnow()

    def facts_run_dir(self) -> Path:
        """事实库所在的运行目录。

        优先用 ``upstream_facts_run``（由 ``agent run --facts-run`` 指定）；
        否则回退到本 run 目录（链式流水线中 R1 与 R4 共用同一 run 时）。
        """
        return self.upstream_facts_run or self.run_dir

    def facts_path(self) -> Path:
        """定位 ``facts.json``。

        容错：既接受被指向 run 目录根，也接受被直接指向 ``facts/`` 子目录，
        还接受直接指向 json 文件本身。用户不必记住精确层级。
        """
        base = self.facts_run_dir()
        candidates = [
            base / "facts" / "facts.json",
            base / "facts.json",
            base,
        ]
        for c in candidates:
            if c.is_file() and c.suffix == ".json":
                return c
        # 都没找到：返回标准位置，交由调用方给出清晰的报错信息
        return base / "facts" / "facts.json"

    def require_facts(self) -> Path:
        """取事实库路径；不存在时抛出带操作指引的错误。"""
        p = self.facts_path()
        if not p.is_file():
            raise FileNotFoundError(
                f"找不到事实库：{p}\n"
                "R4 / R5 只消费 R1 的事实库产物（不重新解析文档，以保证数字一致）。\n"
                "请先运行 `agent run r1_extract`，然后：\n"
                "  · 链式运行全流程：   agent run full\n"
                "  · 指定上游 run：     agent run r4_valuation --facts-run <r1_run_id>"
            )
        return p

    def ensure_dirs(self) -> None:
        for p in (
            self.facts_dir,
            self.valuation_dir,
            self.audit_dir,
            self.pages_dir,
            self.cache_dir,
            self.report_dir,
            self.web_dir,
        ):
            p.mkdir(parents=True, exist_ok=True)


# ==========================================================================
# 调度器
# ==========================================================================


class Loop:
    """宏观调度器：按 Plan 顺序执行阶段，带断点续跑与降级。"""

    def __init__(self, ctx: RunContext, plan: Plan, *, resume: bool = False) -> None:
        self.ctx = ctx
        self.plan = plan
        self.resume = resume
        self.completed: list[str] = []
        self.failed: list[str] = []
        self.skipped: list[str] = []
        self._checkpoint = ctx.run_dir / "checkpoint.json"
        # 上一次落在同一 run 目录里的运行状态。续跑收尾时用它防止「把状态洗好」，
        # 见 `run()` 末尾的长注释。
        self._prev_status: str | None = self._read_prev_status()
        # 开跑前就已经存在的文件。见 `_finalize_manifest`：只有**本次新增**的
        # 文件才补进产物清单。
        self._preexisting: set[Path] = set()

    def _read_prev_status(self) -> str | None:
        mf = self.ctx.run_dir / "manifest.json"
        if not mf.is_file():
            return None
        try:
            return json.loads(mf.read_text(encoding="utf-8")).get("status")
        except (OSError, json.JSONDecodeError):
            return None

    # ---------------------------------------------------------------- 主流程

    def run(self) -> RunManifest:
        ctx = self.ctx
        ctx.ensure_dirs()
        self._preexisting = {
            p for p in ctx.run_dir.rglob("*") if p.is_file()
        }
        self._claim_run_dir()
        manifest = ctx.manifest or RunManifest(
            run_id=ctx.run_id,
            pipeline=ctx.pipeline,
            python_version=sys.version.split()[0],
            offline=bool(ctx.gateway and ctx.gateway.offline),
        )
        ctx.manifest = manifest

        done_before = self._load_checkpoint() if self.resume else []
        if done_before:
            self._log(f"断点续跑：已完成 {len(done_before)} 个阶段 {done_before}")

        status: str = "success"
        for stage in self.plan.stages:
            if stage.name in done_before:
                self.completed.append(stage.name)
                continue

            self._log(f"▶ 阶段 [{stage.name}] {stage.description}")
            ctx.budget.enter_stage(stage.name)
            if ctx.tracer:
                ctx.tracer.stage_event(stage=stage.name, status="start")

            try:
                fn = stage.resolve()
                result = fn(ctx)
                self.completed.append(stage.name)
                self._record_produces(stage)
                if ctx.tracer:
                    ctx.tracer.stage_event(
                        stage=stage.name,
                        status="ok",
                        detail=json.dumps(result or {}, ensure_ascii=False, default=str)[:300],
                    )
                self._log(f"  ✅ {stage.name} 完成（{ctx.budget.stage_elapsed(stage.name):.1f}s）")

                # ★ 阶段"完成"但**有部分失败**时，整次运行必须标为 partial。
                #
                #   实测事故：一步表格裁决的 VLM 调用失败 1 次，阶段照样报 success、
                #   产物照常落盘，运行状态也是 success。后果是
                #   **`agent replay --strict` 报『不可复现』却给不出原因** ——
                #   那一次失败的调用没有响应缓存，离线回放只能产出不同的结果。
                #
                #   失败已经被计数（`table_cells_failed`），问题在于**没有传导到运行状态**：
                #   计数只进了产物与 trace，没人看。这里把它提升到运行级信号，
                #   让"这次运行不完整"成为一件必须被看见的事。
                partial = _partial_marker(result)
                if partial:
                    status = "partial"
                    self._log(
                        f"  ⚠️ {stage.name} 有部分失败，运行标记为 partial："
                        f"{partial}（产物已落盘，但本次结果不完整、可能不可复现）"
                    )
                    if ctx.tracer:
                        ctx.tracer.stage_event(
                            stage=stage.name, status="warn",
                            detail=f"部分失败：{partial}",
                        )

            except BudgetExceededError as exc:
                self.skipped.append(stage.name)
                status = "partial"
                self._log(f"  ⚠️ {stage.name} 因预算超限跳过：{exc}")
                if ctx.tracer:
                    ctx.tracer.error(where=stage.name, message=f"预算超限：{exc}", stage=stage.name)

            except StageSkipped as exc:
                self.skipped.append(stage.name)
                self._log(f"  ⏭️ {stage.name} 主动跳过：{exc}")

            except HarnessError as exc:
                if stage.optional:
                    self.skipped.append(stage.name)
                    status = "partial"
                    self._log(f"  ⚠️ 可选阶段 {stage.name} 失败，降级继续：{exc}")
                    if ctx.tracer:
                        ctx.tracer.error(where=stage.name, message=str(exc), stage=stage.name)
                else:
                    self.failed.append(stage.name)
                    status = "failed"
                    self._log(f"  ❌ {stage.name} 失败：{exc}")
                    if ctx.tracer:
                        ctx.tracer.error(where=stage.name, message=str(exc), stage=stage.name)
                    break

            except Exception as exc:  # 未预期异常：记录完整堆栈信息但继续判定为失败
                self.failed.append(stage.name)
                status = "failed"
                self._log(f"  ❌ {stage.name} 未预期异常：{type(exc).__name__}: {exc}")
                if ctx.tracer:
                    ctx.tracer.error(
                        where=stage.name,
                        message=f"{type(exc).__name__}: {exc}",
                        stage=stage.name,
                    )
                break

            finally:
                self._save_checkpoint()

        # —— 收尾 ——
        # ★★ 续跑**不得把运行状态洗好**（2026-09 第十八轮实测）。
        #
        #   实测教训：`annual41-seg` 原本是 `partial`（一次模型调用失败），
        #   我为了给演示流程计时，在它上面跑了一次 `--resume`（全部阶段被跳过）。
        #   那一轮没有任何阶段执行、`failed=[]`，于是收尾把 status 重算成 `success`
        #   并覆盖写回 —— **一次空转的续跑把「运行不完整」这件事洗掉了**。
        #
        #   后果不是抽象的：`replay` 靠 manifest.status 决定要不要打印
        #   「本次不一致是预期结果（原始运行 partial）」这段说明。
        #   洗成 success 之后说明不再打印，读者只看到一个孤零零的
        #   「❌ 不可复现」，而这正是本文件反复警告的
        #   「假警报会训练人忽略复现报告」。
        #
        #   判据：续跑时若**有阶段被跳过**（说明这不是一次完整重跑），
        #   状态只能取「更差」的那个 —— 半途续跑没有能力修复别处的失败。
        if self.resume and done_before:
            worse = _worse_status(self._prev_status, status)
            if worse != status:
                self._log(
                    f"⚠️ 本次是续跑（跳过 {len(done_before)} 个已完成阶段），"
                    f"运行状态保留上次的 `{worse}` —— 不做成 `{status}`。\n"
                    "   续跑没有被跳过的那些阶段并没有重跑，"
                    "把它们的历史问题记成「已解决」会让复现报告失去解释。"
                )
            status = worse

        manifest.status = status  # type: ignore[assignment]
        manifest.total_steps = ctx.budget.steps
        manifest.total_tokens = ctx.budget.tokens
        manifest.total_cost_cny = round(ctx.budget.cost_cny, 6)
        if ctx.gateway is not None:
            stats = ctx.gateway.stats
            manifest.cache_hit_rate = stats.get("cache_hit_rate")

        self._finalize_manifest()

        if ctx.tracer:
            ctx.tracer.stage_event(
                stage="__loop__",
                status=status,
                detail=f"completed={self.completed} skipped={self.skipped} failed={self.failed}",
            )

        self._log(
            f"■ 运行结束：status={status} 完成 {len(self.completed)}/{len(self.plan.stages)} "
            f"阶段，{ctx.budget.steps} 步，{ctx.budget.tokens:,} token，¥{ctx.budget.cost_cny:.4f}"
        )
        return manifest

    # ---------------------------------------------------------------- 内部

    def _claim_run_dir(self) -> None:
        """给 run 目录上一个「单写者」的标记，发现别人在写就**当场吼一声**。

        为什么需要：实测踩过一次 —— 两个 warm 进程同时写同一个 run 名，
        缓存文件被两个写者穿插追加（569 行里 168 个键被写了两次，行距 26–58），
        于是回放取到的不是产物当时用的那个答案，一次**本可复现**的运行被判
        「❌ 不可复现」。那种假警报比不做校验更糟：它会训练人忽略复现报告。

        只警告、不阻断：评审可能刻意并行跑不同 run；
        而且这里的判据是「pid 还活着吗」，误判的代价不该是跑不起来。
        """
        lock = self.ctx.run_dir / ".lock"
        try:
            if lock.is_file():
                prev = lock.read_text(encoding="utf-8").strip()
                pid_txt, _, when = prev.partition("@")
                if pid_txt.isdigit() and _pid_alive(int(pid_txt)):
                    self._log(
                        f"⚠️ runs/{self.ctx.run_id} 正被 pid {pid_txt}（{when}）写入。\n"
                        "   **同一个 run 目录同时只能有一个写者** —— 两个进程一起写会把\n"
                        "   缓存与 trace 写脏，回放会报「不可复现」，而那只是假警报。\n"
                        "   请换一个 run 名（--run-id），或等对方跑完。"
                    )
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            lock.write_text(f"{os.getpid()}@{stamp}\n", encoding="utf-8", newline="\n")
        except OSError:  # pragma: no cover - 只读介质等
            pass

    def _record_produces(self, stage: Stage) -> None:
        """记录阶段产物哈希到 manifest —— 复现比对的依据。"""
        if self.ctx.manifest is None:
            return
        for rel in stage.produces:
            target = self.ctx.run_dir / rel
            if target.exists() and target.is_file():
                self.ctx.manifest.outputs[rel] = file_sha256(target)

    def _finalize_manifest(self) -> None:
        ctx = self.ctx
        manifest = ctx.manifest
        if manifest is None:
            return
        from agent.evidence.models import utcnow

        manifest.finished_at = utcnow()
        # 扫描 run 目录下所有 json/md/xlsx/png 产物，补充哈希
        #
        # ⚠️ 只补**本次运行新增**的文件。为什么必须加这个限制：
        #   `audit.md` 由 CLI 在 loop 结束**之后**才写，不属任何阶段。
        #   续跑（--resume）时它已经在盘上了，于是被 rglob 扫进 outputs ——
        #   而回放只跑阶段，永远产不出它，一次本可复现的运行被判
        #   「❌ 不可复现：回放未产出 audit.md」（实测踩过）。
        #   同理，上一轮留下的 replay_report.md 等散件也会被误收。
        for pattern in ("*.json", "*.md", "*.xlsx", "*.csv", "*.png"):
            for f in ctx.run_dir.rglob(pattern):
                if "_replay" in f.parts or f.name in {"manifest.json", "checkpoint.json"}:
                    continue
                if f in self._preexisting:
                    continue
                rel = str(f.relative_to(ctx.run_dir)).replace("\\", "/")
                manifest.outputs.setdefault(rel, file_sha256(f))
        (ctx.run_dir / "manifest.json").write_text(
            manifest.model_dump_json(indent=2) + "\n", encoding="utf-8", newline="\n"
        )

    def _save_checkpoint(self) -> None:
        data = {
            "pipeline": self.plan.name,
            # provider 也落一份。manifest.json 要**跑完**才写，而「这个 run 是不是
            # mock」在**跑的过程中**就得能读到 —— 控制台右栏正在展示的恰是这段时间
            # 的原始对话。缺了它，`--mock` 的 run 在整段运行里与真实 run 无从分辨，
            # 于是罐头回答会被当成模型的真话（而右栏正是给人看模型说了什么的地方）。
            "provider": (self.ctx.manifest.provider if self.ctx.manifest else ""),
            "completed": self.completed,
            "failed": self.failed,
            "skipped": self.skipped,
            "budget": self.ctx.budget.snapshot(),
        }
        self._checkpoint.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
        )

    def _load_checkpoint(self) -> list[str]:
        if not self._checkpoint.exists():
            return []
        try:
            data = json.loads(self._checkpoint.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        if data.get("pipeline") != self.plan.name:
            self._log("检查点的 pipeline 与本次不一致，忽略断点")
            return []
        return list(data.get("completed", []))

    @staticmethod
    def _log(msg: str) -> None:
        print(msg, flush=True)


# ==========================================================================
# 运行时装配
# ==========================================================================


def build_gateway(
    ctx: RunContext,
    *,
    offline: bool,
    use_mock: bool = False,
    models_config: dict[str, Any] | None = None,
) -> ModelGateway:
    """按配置装配模型网关。

    ⚠️ **``offline`` 与 ``use_mock`` 是两件独立的事，不能混为一谈**：

        offline   —— 禁止出网，只允许命中缓存
        use_mock  —— 换用假模型（P2 框架测试 / CI）

    早期实现写成 ``if use_mock or offline: provider = MockProvider()``，
    结果是 **replay（offline=True）会换掉 provider**，
    而缓存键包含 provider 名 → 全部缓存未命中 →
    回放判定为「不可复现」（假警报），实测踩过。

    正确行为：offline 时**仍使用原 provider**，只是网络调用被禁止；
    缓存未命中时由 ModelGateway 抛错。
    """
    cfg = models_config or load_models_config()
    roles: dict[str, RoleConfig] = {}
    for role, spec in (cfg.get("roles") or {}).items():
        if not isinstance(spec, dict) or "model" not in spec:
            raise ConfigError(f"角色 {role} 的配置缺少 model 字段")
        roles[role] = RoleConfig(
            model=str(spec["model"]),
            temperature=float(spec.get("temperature", 0.0)),
            max_tokens=int(spec.get("max_tokens", 16384)),
            reasoning_effort=spec.get("reasoning_effort"),
            # 默认关闭思考：思考模式下 temperature 不生效，会破坏可复现性
            thinking=bool(spec.get("thinking", False)),
            top_p=float(spec.get("top_p", 1.0)),
            supports_vision=bool(spec.get("supports_vision", False)),
        )

    if use_mock:
        provider = MockProvider()
    else:
        pcfg = cfg.get("provider") or {}
        ptype = pcfg.get("type", "openai_compat")
        if ptype != "openai_compat":
            raise ConfigError(f"暂不支持的 provider 类型：{ptype}")
        provider = OpenAICompatProvider(
            base_url=str(pcfg.get("base_url", "")),
            api_key=pcfg.get("api_key"),
            extra_headers=pcfg.get("extra_headers"),
            default_params=pcfg.get("default_params"),
            provider_name=str(pcfg.get("name", "openai_compat")),
        )

    budget_cfg = cfg.get("budget") or {}
    b = ctx.budget
    if budget_cfg:
        b.max_steps = budget_cfg.get("max_steps", b.max_steps)
        b.max_tokens = budget_cfg.get("max_tokens", b.max_tokens)
        b.max_wall_seconds = budget_cfg.get("max_wall_seconds", b.max_wall_seconds)
        b.max_cost_cny = budget_cfg.get("max_cost_cny", b.max_cost_cny)

    cache = ResponseCache(ctx.cache_dir / "llm.jsonl", enabled=True)
    gw = ModelGateway(
        provider=provider,
        roles=roles,
        tracer=ctx.tracer,
        cache=cache,
        budget=b,
        offline=offline,
        max_retries=int(cfg.get("max_retries", 2)),
        timeout=float(cfg.get("timeout_s", 120.0)),
    )
    # 缓存复用：断点续跑时先加载已有缓存
    return gw


def build_file_guard(ctx: "RunContext") -> FileGuard:
    """按上下文装配文件访问器。读根见 `RunContext.read_roots`，写根只有 run 目录。

    **写根只有一处**是有意的：中间产物与交付产物全部落在 `runs/<run_id>/` 下，
    于是「这次运行改动了什么」= 这个目录的 diff，不需要另做说明。
    """
    return FileGuard(
        read_roots=ctx.read_roots(),
        write_roots=[ctx.run_dir],
        tracer=ctx.tracer,
    )


def build_context(
    *,
    run_id: str,
    runs_root: str | Path | None = None,
    run_dir: "Path | None" = None,
    pipeline: str = "r1_extract",
    offline: bool = False,
    use_mock: bool = False,
    seed: int = 0,
    dry_run: bool = False,
    facts_run: str | Path | None = None,
    started_at: "datetime | None" = None,
) -> RunContext:
    """装配一个可用的运行上下文。

    Args:
        run_dir: **产物写到哪里**。默认 ``runs_root/run_id``。replay 会显式
            传 ``<原始 run>/_replay`` —— 必须在这里就给对，不能等返回后再改
            ``ctx.run_dir``：``Tracer`` 与 ``ModelGateway`` 都是在**本函数内部**
            构造的，两者各持一份 tracer 引用（``build_gateway`` 里
            ``tracer=ctx.tracer``）。返回后只改 ``ctx`` 上的字段，
            gateway 手里那份纹丝不动，于是模型调用事件仍然写进原始 run 的
            ``trace.jsonl``（实测：一次 replay 泄漏 397 条 ``llm_call``）。
        facts_run: 上游 R1 运行目录（R4 / R5 用）。可以是 run_id、
            相对/绝对路径，或直接指向 ``facts/`` 目录。
        started_at: 显式指定运行起始时间。**replay 必须传入原始时间戳** ——
            产物中的时间字段取自它，若用当前时间，回放产物将与原始产物
            哈希不同，「可复现」无法通过校验。
    """
    root = Path(runs_root) if runs_root else PROJECT_ROOT / "runs"
    run_dir = Path(run_dir) if run_dir is not None else root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    upstream: Path | None = None
    if facts_run:
        cand = Path(facts_run)
        if not cand.exists():
            cand = root / str(facts_run)
        if not cand.exists():
            raise ConfigError(
                f"--facts-run 指向的路径不存在：{facts_run}",
                resolved=str(cand),
                hint="用 `agent list` 查看可用的 run",
            )
        upstream = cand

    tracer = Tracer(run_dir / "trace.jsonl")
    ctx = RunContext(
        run_id=run_id,
        run_dir=run_dir,
        pipeline=pipeline,
        dry_run=dry_run,
        upstream_facts_run=upstream,
        tracer=tracer,
        budget=Budget(),
    )
    ctx.ensure_dirs()
    ctx.ledger = FactLedger(ctx.store, run_id=run_id)

    # 加载提示词
    n = ctx.prompts.load_dir(PROJECT_ROOT / "domain" / "prompts")
    # 加载领域技能（Skill 是赛题要求提交的核心模块之一）
    n_skills = ctx.skills.load_dir(PROJECT_ROOT / "domain" / "skills")
    # 加载配置。
    # ⚠️ `tools.yaml` 里除了工具白名单，还承载**子系统参数**
    #    （如 `tables.*` 的表格裁决策略）。因此除了整份配置，
    #    还要把已解析的段直接摊到顶层 —— 阶段函数写
    #    `ctx.configs.get("tables")` 才拿得到（实测踩过：
    #    少了这一步，表格裁决静默不触发，而且不报任何错）。
    tools_cfg = load_tools_config()
    ctx.configs = {
        "models": load_models_config(),
        "tools": tools_cfg,
        "routing": _safe_config("routing"),
        **{k: v for k, v in tools_cfg.items() if isinstance(v, dict)},
    }
    ctx.gateway = build_gateway(ctx, offline=offline, use_mock=use_mock)
    # ★ 文件访问器**故意不在这里装配**，交给 `ctx.guard()` 首次用到时再建。
    #
    #   读根是从 `ctx.configs["paths"]` 推出来的，而这一份**要等本函数返回之后**
    #   才由 CLI 填上（`agent/cli.py`：`ctx.configs["paths"] = {"data": …,
    #   "raw": <材料目录绝对路径>, …}`）。在这里就建，读根里只有兜底的
    #   `PROJECT_ROOT/data`，于是 `--data <别的目录>` 的每一次源文档读取都会
    #   被判越权 —— 实测就是这么炸的（web 自测：inbox/raw/sample.pdf 越权）。
    #   懒建一次不亏：第一次读写就会触发，之后全程复用同一份。

    ctx.manifest = RunManifest(
        run_id=run_id,
        pipeline=pipeline,
        started_at=started_at or utcnow(),
        python_version=sys.version.split()[0],
        offline=offline,
        seed=seed,
        prompts=ctx.prompts.fingerprint(),
        skills=ctx.skills.fingerprint(),
        models={r: c.model for r, c in ctx.gateway.roles.items()},
        provider=ctx.gateway.provider.name,
        model_params={
            r: c.to_params() for r, c in ctx.gateway.roles.items()
        },
        agent_config_hash=_config_fingerprint(),
    )
    if n:
        tracer.stage_event(stage="__boot__", status="ok", detail=f"加载 {n} 个提示词")
    if n_skills:
        tracer.stage_event(
            stage="__boot__", status="ok", detail=f"加载 {n_skills} 个领域技能"
        )
    if not has_jsonschema():
        tracer.stage_event(
            stage="__boot__",
            status="warn",
            detail="未安装 jsonschema，Schema 校验降级为轻量模式",
        )
    return ctx


def _worse_status(a: str | None, b: str) -> str:
    """两个运行状态取**更差**的那个（续跑不得把 partial/failed 洗成 success）。"""
    rank = {"success": 0, "partial": 1, "failed": 2}
    if a not in rank:
        return b
    return a if rank[a] > rank.get(b, 0) else b


def _pid_alive(pid: int) -> bool:
    """这个 pid 还活着吗？**判不了就当活着**（宁可多警告，不可漏警告）。

    ⚠️ 实测踩过的坑：在受限环境（沙箱）里 ``tasklist`` 返回
    ``ERROR: Access denied``（返回码 1、stdout 为空）。早期实现只看
    stdout 里有没有 pid，于是"查不了"被当成了"已经死了" ——
    与上面这句注释**正好相反**：本该"多警告"，实际变成**永不警告**。

    这类缺陷特别危险：**一条永远不会触发的守卫，看起来和一条一直在工作的守卫一模一样**。
    所以现在把"查不了"显式归到"当活着"这一侧。
    """
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import subprocess

            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
            if out.returncode != 0:          # 查不了（沙箱拒绝 / tasklist 不可用）
                return True
            return str(pid) in (out.stdout or "")
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:              # 进程在，只是不归我们管
            return True
        return True
    except Exception:  # noqa: BLE001
        return True


def _partial_marker(result: Any) -> str:
    """阶段返回值里的"部分失败"标记 → 人类可读描述（没有则返回空串）。

    约定：阶段在返回值里放 ``partial_failures: {说明: 条数}``，
    调度器据此把整次运行标为 ``partial``。

    **为什么值得一条约定**：本项目已经有"异常必须计数"的纪律，
    但计数只落在产物里，没有传导到**运行状态**。于是出现过
    「阶段 success、产物已落盘、replay 却说不可复现，而没人知道为什么」。
    把计数升格为状态，是让这类问题在运行结束的那一行输出里就暴露。
    """
    if not isinstance(result, dict):
        return ""
    raw = result.get("partial_failures")
    if not raw:
        return ""
    if isinstance(raw, dict):
        return ", ".join(f"{k} {v}" for k, v in raw.items() if v)
    return str(raw)


def _safe_config(name: str) -> dict[str, Any]:
    try:
        return load_yaml(CONFIG_DIR / f"{name}.yaml")
    except ConfigError:
        return {}


def _config_fingerprint() -> str:
    """对 configs/ 下全部配置文件取指纹，写入 manifest 用于复现比对。"""
    blobs: dict[str, str] = {}
    if CONFIG_DIR.exists():
        for f in sorted(CONFIG_DIR.rglob("*.yaml")):
            blobs[str(f.relative_to(CONFIG_DIR)).replace("\\", "/")] = f.read_text(encoding="utf-8")
    return payload_hash(blobs)[:32]


def new_run_id(pipeline: str) -> str:
    """生成 run_id：时间戳 + 流水线名，可读且有序。"""
    from datetime import datetime

    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{pipeline}"


__all__ = [
    "Stage",
    "Plan",
    "RunContext",
    "Loop",
    "build_context",
    "build_gateway",
    "new_run_id",
]
