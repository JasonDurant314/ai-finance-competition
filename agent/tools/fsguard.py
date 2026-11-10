"""文件系统守卫 —— 落实题目『封闭或半封闭数据环境』要求。

两条铁律：
1. **读写分离**：输入目录只读，输出只允许写在当前 run 目录内。
2. **访问留痕**：每一次 open 都被记录到 trace.jsonl。

路径越权抛 ``SandboxViolation``，不静默放行。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO, Any, Iterable, Literal

from agent.runtime.errors import SandboxViolation
from agent.trace.tracer import Tracer, file_sha256

Mode = Literal["read", "write", "list"]


class FileGuard:
    """受限文件访问器。所有模块必须通过它访问磁盘。"""

    def __init__(
        self,
        *,
        read_roots: Iterable[str | Path],
        write_roots: Iterable[str | Path],
        tracer: Tracer | None = None,
        allow_network: bool = False,
    ) -> None:
        self.read_roots = [Path(p).resolve() for p in read_roots]
        self.write_roots = [Path(p).resolve() for p in write_roots]
        self.tracer = tracer
        self.allow_network = allow_network

    # ---------------------------------------------------------------- 校验

    @staticmethod
    def _is_within(path: Path, roots: list[Path]) -> bool:
        for root in roots:
            try:
                path.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def _authorize(self, path: str | Path, mode: Mode) -> Path:
        p = Path(path).resolve()
        roots = self.read_roots if mode in ("read", "list") else self.write_roots
        if not self._is_within(p, roots):
            raise SandboxViolation(
                "文件访问越权",
                path=str(p),
                mode=mode,
                allowed_roots=[str(r) for r in roots],
            )
        return p

    def _log(self, path: Path, mode: Mode, sha: str | None = None) -> None:
        if self.tracer is not None:
            try:
                # ★ 记**相对路径**，不是 str(path)。绝对路径会把本机用户名
                #   （`C:\Users\<名字>\…`）写进 trace.jsonl —— 那是要交给评审的
                #   交付物，同一条规矩见 `relpath()` 的 docstring 与
                #   `SUBMISSION_CHECKLIST` 里「产物不得含绝对路径」。
                rel = self.relpath(path)
            except Exception:  # pragma: no cover
                rel = path.name
            self.tracer.file_access(path=rel, mode=mode, sha=sha)

    def record(self, path: str | Path, mode: Mode, *, sha: str | None = None) -> Path:
        """登记一次**由调用方自己完成**的访问，校验 + 留痕，但不代劳读写。

        为什么需要它：有几类访问**根本进不来** —— PyMuPDF 的 ``fitz.open`` /
        ``pix.save``、matplotlib 的 ``savefig``、PIL 的 ``img.save``、
        pandas 的 ``to_excel`` 都在 C 扩展或第三方库里 open 文件，
        本类包不住它们。把它们改写成 ``guard.write_bytes(...)`` 会改变落盘路径
        （坐标系/编码/图元写法都可能差一个字节），而一个字节的差就是
        「产物哈希不一致」—— 代价远大于收益。所以这些点位改成
        「调用方照旧读写，guard 在旁边记一笔」：

            target = Path(out)
            render_page_png(doc, page_no, target)   # 照旧
            guard.record(target, "write")           # 记一笔

        ``sha`` 不传时按落盘后的文件现算（写入类调用应当**在写完之后**才调
        本方法，否则算出来的是旧内容或 None）。
        """
        p = self._authorize(path, mode)
        if sha is None and p.is_file():
            sha = file_sha256(p)
        self._log(p, mode, sha)
        return p

    # ---------------------------------------------------------------- 读

    def read_text(self, path: str | Path, *, encoding: str = "utf-8") -> str:
        p = self._authorize(path, "read")
        sha = file_sha256(p) if p.is_file() else None
        self._log(p, "read", sha)
        return p.read_text(encoding=encoding)

    def read_bytes(self, path: str | Path) -> bytes:
        p = self._authorize(path, "read")
        sha = file_sha256(p) if p.is_file() else None
        self._log(p, "read", sha)
        return p.read_bytes()

    def open_read(self, path: str | Path, *, binary: bool = True) -> IO[Any]:
        p = self._authorize(path, "read")
        self._log(p, "read", file_sha256(p) if p.is_file() else None)
        return open(p, "rb" if binary else "r", **({} if binary else {"encoding": "utf-8"}))

    def list_dir(self, path: str | Path) -> list[Path]:
        p = self._authorize(path, "list")
        self._log(p, "list")
        return sorted(p.iterdir())

    def exists(self, path: str | Path) -> bool:
        try:
            return self._authorize(path, "read").exists()
        except SandboxViolation:
            return False

    # ---------------------------------------------------------------- 写

    def write_text(self, path: str | Path, content: str, *, encoding: str = "utf-8") -> Path:
        p = self._authorize(path, "write")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding=encoding, newline="\n")
        self._log(p, "write", file_sha256(p))
        return p

    def write_bytes(self, path: str | Path, content: bytes) -> Path:
        p = self._authorize(path, "write")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
        self._log(p, "write", file_sha256(p))
        return p

    def write_json(self, path: str | Path, obj: Any, *, indent: int = 2) -> Path:
        import json
        from decimal import Decimal

        def default(o: Any) -> Any:
            if isinstance(o, Decimal):
                return str(o)
            if hasattr(o, "model_dump"):
                return o.model_dump(mode="json")
            if hasattr(o, "isoformat"):
                return o.isoformat()
            raise TypeError(f"无法序列化 {type(o)!r}")

        blob = json.dumps(obj, ensure_ascii=False, indent=indent, default=default)
        return self.write_text(path, blob + "\n")

    def read_json(self, path: str | Path) -> Any:
        import json

        return json.loads(self.read_text(path))

    # ---------------------------------------------------------------- 网络

    def check_network(self, host: str) -> None:
        """离线模式下禁止任何出网调用。"""
        if not self.allow_network:
            raise SandboxViolation(
                "离线模式下禁止网络访问（题目要求『封闭或半封闭数据环境』）", host=host
            )

    def relpath(self, path: str | Path) -> str:
        """转成相对路径 —— 产物中禁止出现绝对路径（含用户名等信息）。"""
        p = Path(path).resolve()
        for root in [*self.write_roots, *self.read_roots]:
            try:
                return str(p.relative_to(root)).replace(os.sep, "/")
            except ValueError:
                continue
        return p.name


__all__ = ["FileGuard", "Mode"]
