"""并发安全的构建缓存：**原子发布**一个构建目录。

为什么需要它
------------
各游戏的 evaluator 都把选手/后端的编译产物放在
``build_root/players/<id>-<内容哈希>/``，并用"产物存在就复用"做缓存。
原来的写法是：

.. code-block:: python

    if executable.is_file():
        return ...                    # 命中缓存
    if root.exists():
        shutil.rmtree(root)           # ← 删掉
    shutil.copytree(package, root)    # ← 重拷
    subprocess.run(("make",), cwd=make_dir)

单线程没问题，但评分（ladder eval）会**并行**跑几百局，同一个选手同时出现在
多局里。于是两个 worker 同时发现"缓存没命中"，一起 rmtree + copytree，
互相把对方正在用的文件删掉。实测在 32 核服务器上打出过三种症状，共 527 局失败：

* ``[Errno 17] File exists: .../players/<id>-<hash>``（都想创建同一个目录）
* ``cc1plus: fatal error: gameunit.cpp: No such file or directory``
  （make 正在编译，源文件被另一个 worker 删了）
* ``make: getcwd: No such file or directory``（make 的工作目录当场消失）

这些会被记成 ``infra_error``，直接污染 Elo：miracle 1830 局里 344 局作废、
lostspace 798 局里 101 局、generals 486 局里 82 局。**不是选手代码的问题。**

做法
----
标准的"先建后换"（build-then-publish）：

1. 已发布（有完成标记）就直接复用，**不删不碰**；
2. 否则拷到一个进程独占的暂存目录里编译；
3. 编译成功后写完成标记，再用 ``os.rename`` **原子**地移到最终位置；
4. 如果这期间别人已经发布好了，就丢掉自己的暂存目录、复用别人的成果
   （同一个内容哈希产出的东西是等价的）；
5. 编译失败则只删暂存目录——**绝不发布半成品**，否则下一次会命中坏缓存。

``os.rename`` 在同一文件系统内对目录是原子的，且当目标已存在（非空）时会失败，
正好用来做"谁先发布谁赢"的仲裁，不需要任何第三方锁库。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

__all__ = [
    "BACKEND_IGNORE",
    "COMPLETE_MARKER",
    "NO_WRITABLE_PARENT",
    "backend_run_dir",
    "ephemeral_run_dir",
    "is_published",
    "published_build_dir",
]

COMPLETE_MARKER = ".aa_arena-complete"

# 找不到任何可写目录时抛这个（而不是"退回只读源码目录"）。
# 静默退回正是 generals 那次事故的形态：后端在只读目录里跑，
# 它内部 open(...,"w") 失败被 except 吞掉 → 判选手非法操作 → Elo 全废。
# 宁可整局报 infra_error（会进 failure_digest、看得见），也不要错误的胜负。
NO_WRITABLE_PARENT = "no writable parent directory for backend run dir"


def is_published(target: Path) -> bool:
    """这个构建目录是否已经**完整**发布过。"""

    return (target / COMPLETE_MARKER).is_file()


@contextmanager
def published_build_dir(
    target: Path,
    *,
    source: Path,
    ignore: Callable[[str, list[str]], set[str]] | None = None,
) -> Iterator[tuple[Path, bool]]:
    """把 ``source`` 拷进 ``target`` 并发布，全过程并发安全。

    yield ``(work_dir, reused)``：

    * ``reused=True`` —— 已有别人发布好的成果，``work_dir`` 就是 ``target``，
      调用方**不要**再编译，直接用现成产物；
    * ``reused=False`` —— ``work_dir`` 是本进程独占的暂存目录，
      在 ``with`` 体内完成编译即可；正常退出时自动原子发布到 ``target``。

    ``with`` 体内抛异常时暂存目录会被清掉，``target`` 保持原状——
    这保证"编译失败"不会留下会被后续命中的半成品缓存。

    注意：调用方在 ``with`` 体内应当使用 ``work_dir`` 下的路径；发布之后
    对应产物在 ``target`` 下的同名相对路径处。
    """

    target = Path(target)
    source = Path(source)
    if is_published(target):
        yield target, True
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.staging-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    if staging.exists():  # 理论上不会撞，稳妥起见
        shutil.rmtree(staging, ignore_errors=True)

    # 上一次异常中断可能留下没有完成标记的残骸；它不会被复用，这里顺手清掉。
    if target.exists() and not is_published(target):
        shutil.rmtree(target, ignore_errors=True)

    shutil.copytree(source, staging, ignore=ignore)
    try:
        yield staging, False
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    (staging / COMPLETE_MARKER).write_text("ok\n", encoding="utf-8")
    try:
        os.rename(staging, target)
    except OSError:
        # 别人先发布了（或目标已被占用）。同一内容哈希的产物等价，复用即可。
        shutil.rmtree(staging, ignore_errors=True)
        if not is_published(target):
            raise


def _pick_writable_parent(candidates: Iterator[Path | None] | list[Path | None]) -> Path:
    """依次试探候选目录，返回第一个**确实能写**的。

    只用 ``os.access`` 判断是不够的：只读挂载、容器内的 CAP 限制、满盘、
    以及\"目录可写但文件系统只读\"都只有真写一次才暴露。所以这里真的建目录、
    写一个探针文件再删掉。
    """

    failures: list[str] = []
    for candidate in candidates:
        if candidate is None:
            continue
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / f".writable-probe-{os.getpid()}-{uuid.uuid4().hex[:8]}"
            probe.write_text("ok\n", encoding="utf-8")
            probe.unlink()
        except OSError as error:
            failures.append(f"{candidate}: {error}")
            continue
        return candidate
    raise OSError(f"{NO_WRITABLE_PARENT}; tried: {'; '.join(failures) or '(none)'}")


@contextmanager
def ephemeral_run_dir(
    source: Path,
    *,
    parent: Path | None = None,
    prefix: str = "run-",
    ignore: Callable[[str, list[str]], set[str]] | None = None,
) -> Iterator[Path]:
    """给**单局对战**准备一个独占、可写的目录（源码树的副本），退出即清理。

    为什么不是"拷一份共享目录大家一起用"
    ------------------------------------
    后端进程的工作目录必须满足两条，共享目录只能满足第一条：

    1. **可写**。判题器后端里有往 cwd 写文件的代码——Generals 的
       ``convert_command_list_str()`` 就有一行调试残留
       ``open("command_list_str.txt", "w")``，而它被 ``except Exception`` 包着，
       写失败会被翻译成"选手非法操作"。后端在 A 仓（只读事实源）里跑时，
       486 局 generals 全部第 1 回合判 P0 负，Elo 变成全员同分——
       **流水线不报错，只是结论全错**。
    2. **每局独占**。评分会并行跑几百局，如果所有 worker 共用一个后端目录，
       它们会同时写同一个文件名。这类冲突不一定当场报错，可能表现为
       "偶发的、无法复现的"对局异常——比事故更难查。

    后端源码树都很小（本仓 60 KB – 520 KB、9–46 个文件），纯 Python 无需编译，
    所以每局拷一份是最简单也最不容易坏的方案。配合退出即删，
    磁盘峰值只有"并发局数 × 单份体积"（32 并发也就几 MB）。

    ``parent`` 是首选的落地位置（通常给 evaluator 的 ``artifact_root`` 或
    ``build_root``，与产物同盘）。不可写时降级到系统临时目录；
    两者都不行就**抛错**，绝不退回只读的源码目录。

    回放不受影响：各游戏 arena 都是把 ``replay`` 的**绝对路径**写进 init 消息
    交给后端，产物落在 artifact 目录，与后端 cwd 无关。
    """

    source = Path(source)
    base = _pick_writable_parent(
        [
            parent,
            Path(tempfile.gettempdir()) / "aa_arena-backend-runs",
        ]
    )
    holder = Path(tempfile.mkdtemp(prefix=prefix, dir=base))
    target = holder / source.name
    try:
        shutil.copytree(source, target, ignore=ignore, symlinks=True)
        yield target
    finally:
        # 清理失败不能让对局作废（比如 NFS 上残留句柄）；下次启动前的
        # 临时目录清理会兜住，磁盘也不会因为一次失败就失控。
        shutil.rmtree(holder, ignore_errors=True)


# 后端副本里这些东西没用，还会拖慢每局的拷贝。
BACKEND_IGNORE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", "__MACOSX", ".git", ".DS_Store"
)


@contextmanager
def backend_run_dir(source: Path, *, parent: Path | None = None, tag: str = "backend") -> Iterator[Path]:
    """各游戏 evaluator 的统一入口：拿一个可写、每局独占的后端工作目录。

    这层封装的意义是**让 8 个游戏只有一种写法**。之前每个游戏各自决定后端
    在哪跑，结果 generals 漏了一处（直接用 A 仓源码根目录）就产出了一份
    看起来正常、其实全员同分的 Elo。少一个自由度就少一次写错的机会。

    用法::

        with backend_run_dir(layout.backend_source_root, parent=self._build_root) as cwd:
            backend_spec = ProcessSpec((sys.executable, "main.py"), cwd)
            result = arena.run_match(...)      # 整局都必须在 with 体内
    """

    with ephemeral_run_dir(source, parent=parent, prefix=f"{tag}-", ignore=BACKEND_IGNORE) as run_dir:
        yield run_dir
