"""追踪器 —— 记录工具调用、模型调用、计算过程与结果生成。

赛题明文要求：『完整记录文件访问、工具调用、计算过程和结果生成情况，
确保数据来源可核验、执行过程可追溯、运行结果可复现。』

**四类都已在编排层接入**（``file_access`` 于 2026-10-17 接入）。
文件访问这一路：每个读写点位都经 ``RunContext.guard()`` 走，
出口是本模块的 ``Tracer.file_access()``，于是 ``trace.jsonl`` 里逐条留下
路径 + 模式（读 / 写 / 列目录）+ sha256 + 阶段。

两条必须守住的规矩（违反其一，这条记录就不可核验，甚至有害）：

* **路径记相对路径。** 绝对路径会把本机用户名写进 ``trace.jsonl`` ——
  那是要交给评审的交付物，同一条规矩见 ``FileGuard.relpath()``。
* **阶段不能是空的。** ``file_access`` 与 ``tool_call`` 一样，发生在业务阶段
  内部却没有阶段可归，读者就无法把一次访问挂到某一步上。所以本模块借
  ``_in_stage()`` 从编排层当前阶段取值 —— 这正是 ``llm_call`` 曾经
  400 条 ``stage: null`` 换来的教训。

另有可独立复核的一道：``runs/<run_id>/manifest.json`` 的 ``input_files``
（输入文件 sha256）与每个产物的 sha256，评审可以逐字节复核。

本模块产出 ``runs/<run_id>/trace.jsonl``，每行一个 ``TraceEvent``。
设计约束：**追踪是框架能力，不是业务代码职责** —— 业务模块通过
``@traced`` 装饰器或 ``Tracer`` 实例获得记录，不得自行写日志文件。
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

from agent.evidence.models import TraceEvent, TraceKind, utcnow


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    raise TypeError(f"无法序列化类型 {type(obj)!r}")


def payload_hash(obj: Any) -> str:
    """对任意载荷取稳定哈希，用于结果比对与缓存键。"""
    blob = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=_json_default)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ==========================================================================
# 逐步记录（``agent_log.jsonl``）—— 记**原文**，与 trace.jsonl 并列
# ==========================================================================
#
# trace.jsonl 记的是「发生了什么」：工具名、耗时、**入参与结果的哈希**。
# 哈希足以证明「两次运行的这一步是同一个载荷」，但读不出「它到底传了什么、
# 回了什么」。所以控制台右栏（想让它长得像 coding agent 的对话流）没有料
# —— 实测中把 trace 摊开，``tool_call`` 一行只有 ``args_hash: a1b2c3…``。
#
# 因此另开一路：``Tracer.journal()`` 追加 ``runs/<id>/agent_log.jsonl``，
# 每行一条 ``{seq, ts, kind, …}``，模型调用带完整 ``messages`` 与回答正文，
# 工具调用带完整 ``args`` 与返回值。**阶段开始的边界也记一行**（``kind="stage"``）
# —— 右栏是一列模型/工具行，没有边界时读者分不出「这一段是哪个阶段说的」，
# 更看不出「R4/R5 那段一个模型都没调」（它们本来就只发 stage 事件）。
#
# 为什么**不**把这些塞进 trace.jsonl：
#   1. ``TraceEvent`` 是定死的契约（字段、pydantic 校验、`summarize_trace`
#      的按 kind 统计都吃它）。塞进几 KB 的提示词会让每条事件变重，
#      而审计路径只需要哈希。
#   2. 原文里有受控材料的正文。``runs/`` 不随源码包提交，但 trace 是
#      审计入口 —— 不该顺手把它变成材料的副本。
#   3. 回放逐字节比对的是 ``manifest.outputs``，而 ``.jsonl`` **不在**
#      ``_finalize_manifest`` 收集的 ``*.json`` / ``*.md`` / ``*.xlsx`` /
#      ``*.csv`` / ``*.png`` 里（``cache/llm.jsonl`` 同理）。所以这一路
#      永远进不了产物清单，不参与、也影响不了「可复现」的判定。

#: 单个字符串的上限。超了按 head + tail 截，**中略**而不是只留开头。
JOURNAL_MAX_STRING = 20_000

#: 单条记录的上限。工具的返回值可以很大（一次表格裁决的单元格全量），
#: 而右栏是每 2 秒轮询一次的 —— 不封顶就是把浏览器当垃圾桶。
JOURNAL_MAX_RECORD = 200_000

#: 整条超限时按这个顺序丢弃载荷字段（先丢最大的那个）。
_JOURNAL_PAYLOAD_KEYS = ("messages", "text", "args", "result")


def _clip_str(s: str, limit: int) -> tuple[str, dict[str, Any] | None]:
    """截断过长字符串：**留头也留尾**。

    只留开头会把要看的那句裁掉 —— 模型提示词的结构是「系统提示 + 长材料在前，
    真正的问题在最后」，工具返回值同理（结论常在后半段）。

    ★ 图片载荷（``data:image/…``）**整条换掉**，不留头尾：VLM 传的是 300 dpi
    整页图，base64 一页上百万字符（另一条并行的记录器实测：226 次调用里
    每次都要裁掉一条 20 万字符的 ``image_url``）。记它的前 15000 个字符
    既读不出图、又占满磁盘 —— 那 15000 个字符是「iVBORw0KGgo…」。
    原图本来就在产物里（``pages/``、单元格裁图），这里只留一条指认。
    """
    if s.startswith("data:image/"):
        return f"<图片已省略：{len(s)} 字符 base64>", {"kind": "image", "total": len(s)}
    if len(s) <= limit:
        return s, None
    head = limit * 3 // 4
    tail = limit - head
    note = f"\n…[中略 {len(s) - limit} 字符]…\n"
    return s[:head] + note + s[-tail:], {"kept": limit, "total": len(s)}


def _clip_strings(obj: Any, limit: int, path: str = "",
                  cut: dict[str, Any] | None = None) -> Any:
    """递归截断结构里所有过长的字符串，并把被截的字段记进 ``cut``。

    ``cut`` 是**这件事必须被看见**的落地：截断后的正文读起来是完整的，
    不说的话没人知道少了多少（``cut`` 里给出保留数与原长）。
    """
    if cut is None:
        cut = {}
    if isinstance(obj, str):
        clipped, meta = _clip_str(obj, limit)
        if meta:
            cut[path or "value"] = meta
        return clipped
    if isinstance(obj, dict):
        return {str(k): _clip_strings(v, limit, f"{path}.{k}" if path else str(k), cut)
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clip_strings(v, limit, f"{path}[{i}]", cut) for i, v in enumerate(obj)]
    return obj


def _fit_record(rec: dict[str, Any], cap: int) -> dict[str, Any]:
    """整条超限时，从最大的载荷字段开始换成省略说明，直到回到上限内。"""
    def size(r: dict[str, Any]) -> int:
        return len(json.dumps(r, ensure_ascii=False, default=_json_default))

    if size(rec) <= cap:
        return rec
    rec = dict(rec)
    omitted: dict[str, Any] = {}
    order = sorted(_JOURNAL_PAYLOAD_KEYS,
                   key=lambda k: -len(json.dumps(rec.get(k), ensure_ascii=False,
                                                 default=_json_default)))
    for k in order:
        if k not in rec:
            continue
        chars = len(json.dumps(rec[k], ensure_ascii=False, default=_json_default))
        rec[k] = None
        omitted[k] = {"omitted": True, "chars": chars}
        if size(rec) <= cap:
            break
    rec["omitted"] = omitted
    return rec


def file_sha256(path: str | Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


#: 勾稽断言的三态 —— 与 ``agent.evidence.models.AssertionStatus`` 同名同义，
#: 字符串化后写进 ``trace.jsonl`` 的 ``extra.status``。
ASSERT_STATUSES = ("pass", "fail", "skipped")


def assertion_status(event: TraceEvent) -> str:
    """读回一条断言事件的三态。

    兼容**早期两态** trace.jsonl（没有 ``extra.status``，只有 ``ok``）：
    那时 ``skipped`` 被压成了 ``ok=False``，读回来只能当作 ``fail``；
    新写入的事件一律带 ``status``，不再有这种歧义。
    """
    status = (event.extra or {}).get("status")
    if status in ASSERT_STATUSES:
        return str(status)
    return "pass" if event.ok else "fail"


class Tracer:
    """JSONL 追踪器。线程安全靠 append 单行写入的原子性。

    写两路：
      * ``trace.jsonl`` —— 事件契约，面向审计（哈希、耗时、计数）。
      * ``agent_log.jsonl`` —— 原文，面向看过程（提示词、回答、工具参数与返回，
        以及每个阶段开始的那一行边界）。见模块中段「逐步记录」一节。
        ``journal=False`` 可只关这一路。

    两路放在同一个 run 目录、同一个 ``Tracer`` 上，是因为**只有一处**能同时
    拿到「这次调用属于哪个阶段」和「它的完整载荷」—— 分成两个对象，就得让
    业务代码自己配一次对，那正是「追踪是框架能力，不是业务代码职责」要避免的。
    """

    def __init__(self, path: str | Path, *, echo: bool = False,
                 journal: bool = True) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._seq = 0
        self._echo = echo
        self._fh = self.path.open("a", encoding="utf-8", newline="\n")
        self.journal_path = self.path.parent / "agent_log.jsonl"
        self._jseq = 0
        self._jfh = (self.journal_path.open("a", encoding="utf-8", newline="\n")
                     if journal else None)
        #: 编排层最近一个 `status="start"` 的阶段名 —— 见 `_in_stage`。
        self._stage: str | None = None

    def _in_stage(self, stage: str | None) -> str | None:
        """定这次调用记在哪个阶段名下：调用方说了算，没说就用**当前正在跑的阶段**。

        编排层每进一个阶段都发 ``stage_event(status="start")``，名字由它
        记在这里。于是「这个阶段叫什么」只有一处定义 —— 计划里那个名字。
        两个实测教训：

        1) **没传 `stage` 就是 null。** VLM 那三处 ``gateway.call(...)`` 都
           没传，226 条 ``llm_call``（中石油那份 run 是 400 条）的 ``stage``
           全是 null，右栏答不出「这句话是哪个阶段说的」。
        2) **各模块写死的阶段名是单跑时的名字。** ``r1_extract`` 单跑时
           R1-4 叫 ``judge``、R5 叫 ``check``，同一个阶段在 ``full`` 里叫
           ``r1_judge``、``r5_check``。实测 ``runs/web-20261012-205543``
           （full）：88 条 ``tool_call`` 挂在 ``judge``、376 条 ``routing``
           挂在 ``ingest``/``extract_ocr`` —— 三个在 full 计划里**不存在**
           的阶段名，这些事件在「哪个阶段干了什么」里无处落座。
           所以 2026-10-12 起流水线模块不再自报阶段名（42 处全部删掉），
           由这里按当前阶段补上：两种计划下都得到计划里的那个名字。

        仍然允许显式传入，且显式优先 —— 编排层自己的 ``__boot__`` /
        ``__loop__`` 不属于任何阶段，它们必须能说自己的名字。
        """
        return stage if stage is not None else self._stage

    # ---------------------------------------------------------------- 写入

    def emit(self, kind: TraceKind, **fields: Any) -> TraceEvent:
        event = TraceEvent(seq=self._seq, kind=kind, **fields)
        self._seq += 1
        self._fh.write(
            json.dumps(event.model_dump(mode="json"), ensure_ascii=False, default=_json_default)
            + "\n"
        )
        self._fh.flush()
        if self._echo:
            print(f"[trace {event.seq:04d}] {kind.value} {fields.get('tool') or fields.get('model') or ''}")
        return event

    def journal(self, kind: str, **fields: Any) -> dict[str, Any] | None:
        """追加一条**原文**记录到 ``agent_log.jsonl``。

        返回落盘的那条记录（被截断/省略过，所以与入参不逐字相同），
        未开启逐步记录时返回 ``None``。调用方一般不需要它 —— 它是给测试
        与「这一步到底记了什么」的诊断用的。
        """
        if self._jfh is None:
            return None
        cut: dict[str, Any] = {}
        rec: dict[str, Any] = {
            "seq": self._jseq,
            "ts": utcnow().isoformat(),
            "kind": kind,
        }
        rec.update({k: _clip_strings(v, JOURNAL_MAX_STRING, k, cut)
                    for k, v in fields.items()})
        if cut:
            rec["cut"] = cut
        self._jseq += 1
        rec = _fit_record(rec, JOURNAL_MAX_RECORD)
        self._jfh.write(
            json.dumps(rec, ensure_ascii=False, default=_json_default) + "\n"
        )
        self._jfh.flush()
        return rec

    # ------------------------------------------------------------ 语义化 API

    def llm(
        self,
        *,
        role: str,
        model: str,
        prompt_id: str,
        prompt_version: str,
        prompt_sha256: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: int,
        cache_hit: bool,
        cost_cny: float = 0.0,
        out_hash: str | None = None,
        stage: str | None = None,
        step: int | None = None,
        ok: bool = True,
        error: str | None = None,
        extra: dict[str, Any] | None = None,
        messages: list[dict[str, Any]] | None = None,
        text: str | None = None,
    ) -> TraceEvent:
        """记录一次模型调用。

        ``messages`` / ``text`` 是**原文**，只进 ``agent_log.jsonl``（见
        :meth:`journal`），不进 ``trace.jsonl`` —— 后者只留 ``out_hash``。
        调用方拿得到就传，拿不到（如只做汇总的路径）不传也不影响审计那一路。
        """
        stage = self._in_stage(stage)
        event = self.emit(
            TraceKind.LLM_CALL,
            role=role,
            model=model,
            prompt_id=prompt_id,
            prompt_version=prompt_version,
            prompt_sha256=prompt_sha256,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            cache_hit=cache_hit,
            cost_cny=cost_cny,
            payload_hash=out_hash,
            stage=stage,
            step=step,
            ok=ok,
            error=error,
            extra=extra or {},
        )
        if messages is not None or text is not None:
            self.journal(
                "llm", stage=stage, role=role, model=model,
                prompt_id=prompt_id, prompt_version=prompt_version,
                cache_hit=cache_hit, latency_ms=latency_ms,
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                cost_cny=cost_cny, ok=ok, error=error,
                messages=messages or [], text=text or "",
            )
        return event

    def tool(
        self,
        *,
        name: str,
        args: Any,
        ok: bool,
        latency_ms: int,
        out_hash: str | None = None,
        stage: str | None = None,
        error: str | None = None,
        result: Any = None,
    ) -> TraceEvent:
        """记录一次工具调用。

        ``args`` 一律进逐步记录（这一步白拿：trace 那路本来就要算它的哈希）；
        ``result`` 有就传 —— 直接调 ``tracer.tool()`` 的地方（OCR、裁决这类
        不经工具注册表的调用）拿不到返回值时，右栏就只显示参数与耗时，
        而不是显示一个空的「返回」。
        """
        stage = self._in_stage(stage)
        event = self.emit(
            TraceKind.TOOL_CALL,
            tool=name,
            args_hash=payload_hash(args),
            ok=ok,
            latency_ms=latency_ms,
            payload_hash=out_hash,
            stage=stage,
            error=error,
        )
        fields: dict[str, Any] = {
            "stage": stage, "tool": name, "ok": ok,
            "latency_ms": latency_ms, "args": args,
        }
        if error:
            fields["error"] = error
        if result is not None:
            fields["result"] = result
        self.journal("tool", **fields)
        return event

    def file_access(
        self, *, path: str, mode: str, sha: str | None = None, stage: str | None = None
    ) -> TraceEvent:
        """记录一次文件访问（读 / 写 / 列目录）。

        ``path`` 一律是**相对路径**：绝对路径会把本机用户名写进交付物里
        （``C:\\Users\\<名字>\\…``），而 ``trace.jsonl`` 是要交给评审的。
        转换在 ``FileGuard._log`` 那一处完成，调用方不必操心。

        ``stage`` 与 ``tool_call`` 同一套规矩：不传就取**当前正在跑的阶段**
        （见 :meth:`_in_stage`）。这一条曾让 400 条 ``llm_call`` 的阶段名全是
        null，右栏答不出「这句话是哪个阶段说的」—— 同类问题不该犯第二次。
        """
        return self.emit(
            TraceKind.FILE_ACCESS,
            path=path,
            mode=mode,
            path_sha256=sha,
            stage=self._in_stage(stage),
        )

    def assertion(
        self,
        *,
        code: str,
        name: str,
        detail: str,
        passed: bool | None = None,
        status: str | None = None,
        stage: str | None = None,
    ) -> TraceEvent:
        """记录一条勾稽断言。

        ⚠️ 这里刻意保留**三态**（``pass`` / ``fail`` / ``skipped``），
        而不是只记一个 ``passed: bool``：``skipped``（缺辅助数据、本次无法独立重算）
        与 ``fail``（校验过且不通过）是两件完全不同的事。

        实测踩过的坑：早期实现只收 bool，``pipelines/r4_valuation/model.py``
        传的是 ``passed=(status == "pass")``，于是 30 条 ``skipped`` 落进
        ``ok=False``，被 :func:`summarize_trace` 统计成「失败」——
        评审跑的第一条命令 ``run full --mock`` 就报「失败 30 条」，
        而那 30 条是**未校验**。``agent/evidence/models.py`` 的 ``is_usable``
        早已按三态判断，追踪层却把两者合并 —— 同一个决策写两处，第二处覆盖第一处。
        """
        if status is None:
            status = "pass" if passed else "fail"
        if status not in ASSERT_STATUSES:
            raise ValueError(f"未知断言状态：{status!r}（应为 {ASSERT_STATUSES}）")
        return self.emit(
            TraceKind.ASSERTION,
            tool=code,
            ok=status == "pass",
            stage=stage,
            extra={"name": name, "detail": detail, "status": status},
        )

    def routing(self, *, page: int, route: str, reason: str, stage: str | None = None) -> TraceEvent:
        """记录逐页路由决策 —— 让『为什么这一页走 OCR』有据可查。"""
        return self.emit(
            TraceKind.ROUTING,
            stage=stage,
            extra={"page": page, "route": route, "reason": reason},
        )

    def stage_event(
        self, *, stage: str | None = None, status: str, detail: str = ""
    ) -> TraceEvent:
        """记录阶段事件。

        ``stage`` 可以不传 —— 不传就是**当前正在跑的那个阶段**（见
        :meth:`_in_stage`）。流水线模块都这么用：阶段内的一条「已跳过 N 页」
        本来就属于它所在的那个阶段，模块自己复述一遍阶段名只会复述错
        （单跑与 ``full`` 叫法不同）。

        ``status`` 取 ``start`` / ``ok`` / ``warn`` / ``degraded`` / ``skipped`` 等。
        **只有 ``start`` 与明确的失败才算 ``ok=False``** ——
        早期实现写成 ``ok = (status == "ok")``，导致正常的事件
        （如 ``warn``、``skipped``）在审计报告里被显示成失败，
        干扰对运行状态的判断。

        ``status="start"`` 这一条**同时**往逐步记录里落一行边界（``kind="stage"``）：
        控制台右栏读的是逐步记录，而它列的全是模型与工具行 —— 没有边界的话，
        读者既分不出「这一段话是哪个阶段说的」，也看不出「R4/R5 那一大段一个
        模型都没调」（那两个选题是纯确定性计算，只发 stage 事件）。只记 ``start``：
        阶段内的 ``warn`` / ``skipped`` 是读数（已进「每步的计数」那一列），
        再往右栏抄一遍只会把流淹掉。
        """
        ok = status not in ("start", "fail", "failed", "error")
        if status == "start":
            # `start` 是**定义**当前阶段的那一条，它的名字不能被上一个阶段顶掉
            # （顶掉的话，第二个阶段的 start 事件会记成第一个阶段的名字）。
            if stage is not None:
                self._stage = stage
            stage = self._stage
        else:
            stage = self._in_stage(stage)
        event = self.emit(
            TraceKind.STAGE,
            stage=stage,
            ok=ok,
            extra={"status": status, "detail": detail},
        )
        if status == "start":
            self.journal("stage", stage=stage, status=status)
        return event

    def error(self, *, where: str, message: str, stage: str | None = None) -> TraceEvent:
        return self.emit(TraceKind.ERROR, stage=stage, tool=where, ok=False, error=message)

    # ---------------------------------------------------------------- 计时

    @contextmanager
    def timed(self) -> Iterator[dict[str, Any]]:
        """用法::

            with tracer.timed() as t:
                ...
            elapsed = t["ms"]
        """
        box: dict[str, Any] = {}
        t0 = time.perf_counter()
        try:
            yield box
        finally:
            box["ms"] = int((time.perf_counter() - t0) * 1000)

    # ---------------------------------------------------------------- 生命周期

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()
        if self._jfh is not None and not self._jfh.closed:
            self._jfh.close()

    def __enter__(self) -> "Tracer":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @property
    def count(self) -> int:
        return self._seq


def read_trace(path: str | Path) -> list[TraceEvent]:
    """读回 trace.jsonl —— 供 replay / audit 使用。"""
    events: list[TraceEvent] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                events.append(TraceEvent.model_validate(json.loads(line)))
    return events


def summarize_trace(events: list[TraceEvent]) -> dict[str, Any]:
    """汇总统计，供 audit 报告与 Web 面板使用。

    断言按**三态**分别计数（``assertions_passed`` / ``assertions_failed`` /
    ``assertions_skipped``）。``assertions_failed`` 只数 ``fail`` ——
    把 ``skipped`` 算进失败会让「缺数据、本次没校验」看起来像「校验不通过」，
    两者对读者的含义完全相反（详见 :meth:`Tracer.assertion`）。
    """
    llm = [e for e in events if e.kind is TraceKind.LLM_CALL]
    tools = [e for e in events if e.kind is TraceKind.TOOL_CALL]
    files = [e for e in events if e.kind is TraceKind.FILE_ACCESS]
    asserts = [e for e in events if e.kind is TraceKind.ASSERTION]
    cache_hits = sum(1 for e in llm if e.cache_hit)
    assert_states = [assertion_status(e) for e in asserts]
    return {
        "total_events": len(events),
        "llm_calls": len(llm),
        "llm_cache_hits": cache_hits,
        "cache_hit_rate": round(cache_hits / len(llm), 4) if llm else None,
        "tool_calls": len(tools),
        "file_accesses": len(files),
        "unique_files": len({e.path for e in files if e.path}),
        "assertions": len(asserts),
        "assertions_passed": assert_states.count("pass"),
        "assertions_failed": assert_states.count("fail"),
        "assertions_skipped": assert_states.count("skipped"),
        "prompt_tokens": sum(e.prompt_tokens or 0 for e in llm),
        "completion_tokens": sum(e.completion_tokens or 0 for e in llm),
        "cost_cny": round(sum(e.cost_cny or 0.0 for e in llm), 6),
        "errors": [e.error for e in events if e.kind is TraceKind.ERROR],
        "started_at": events[0].ts.isoformat() if events else None,
        "finished_at": events[-1].ts.isoformat() if events else None,
    }


__all__ = [
    "ASSERT_STATUSES",
    "JOURNAL_MAX_RECORD",
    "JOURNAL_MAX_STRING",
    "Tracer",
    "assertion_status",
    "payload_hash",
    "file_sha256",
    "read_trace",
    "summarize_trace",
    "utcnow",
]
