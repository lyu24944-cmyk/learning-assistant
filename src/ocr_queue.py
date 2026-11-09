"""识图任务的有界队列。

这一层只管「排队、派活、别把线程弄死」，不知道识别怎么做，也不碰数据库。
真正的活儿由调用方通过 `start(on_job)` 注入，所以这个模块可以脱离 FastAPI
和 SQLite 单独测试。

三个刻意的选择：

1. **`submit()` 非阻塞**。用 `put_nowait()`，满了返回 False。如果这里阻塞，
   事件循环又会被卡住，等于白改。
2. **工人用 daemon 线程，不用 ThreadPoolExecutor**。ThreadPoolExecutor 的线程
   在解释器退出时会 join，一个卡在 30 秒识别里的任务会把重启拖住。
   daemon 线程随进程一起走，重启是干脆的。
3. **队列里只放 draft_id，不放图片字节**。一张图最大 8MB，20 张就是 160MB。
   工人拿到 id 再去私有目录读原图。
"""

from __future__ import annotations

import logging
import os
import queue
import threading
from collections.abc import Callable

logger = logging.getLogger("app.ocr")

DEFAULT_QUEUE_SIZE = 20
DEFAULT_WORKERS = 2

_state_lock = threading.Lock()
_queue: queue.Queue[int] | None = None
_on_job: Callable[[int], None] | None = None
_threads: list[threading.Thread] = []
_started = False

_running_lock = threading.Lock()
_running = 0


class QueueNotStarted(RuntimeError):
    """队列还没启动。调用方应当回滚刚插入的行。"""


def queue_size() -> int:
    raw = os.environ.get("LEARNING_ASSISTANT_OCR_QUEUE_SIZE", "").strip()
    try:
        return max(1, int(raw)) if raw else DEFAULT_QUEUE_SIZE
    except ValueError:
        return DEFAULT_QUEUE_SIZE


def max_workers() -> int:
    raw = os.environ.get("LEARNING_ASSISTANT_OCR_WORKERS", "").strip()
    try:
        return max(1, int(raw)) if raw else DEFAULT_WORKERS
    except ValueError:
        return DEFAULT_WORKERS


def start(on_job: Callable[[int], None]) -> None:
    """启动队列。重复调用是安全的，第二次直接返回。"""
    global _queue, _on_job, _threads, _started
    with _state_lock:
        if _started:
            return
        _queue = queue.Queue(maxsize=queue_size())
        _on_job = on_job
        _started = True
        _threads = [
            threading.Thread(target=_loop, name=f"ocr-worker-{index}", daemon=True)
            for index in range(max_workers())
        ]
        for thread in _threads:
            thread.start()


def shutdown(wait: bool = False, timeout: float = 2.0) -> None:
    """停止派新活。

    不打断正在跑的任务：Python 杀不掉线程，硬杀会留下写了一半的行。
    那些任务下次启动会被 `recover_interrupted()` 标成失败。
    """
    global _started, _threads
    with _state_lock:
        if not _started and not _threads:
            return
        _started = False
        threads = list(_threads)
        _threads = []
    if wait:
        for thread in threads:
            thread.join(timeout=timeout)


def submit(draft_id: int) -> bool:
    """入队。满或未启动返回 False，调用方负责回滚。"""
    with _state_lock:
        current = _queue
        started = _started
    if current is None or not started:
        return False
    try:
        current.put_nowait(int(draft_id))
    except queue.Full:
        return False
    return True


def requeue(draft_ids: list[int]) -> dict[str, int]:
    """启动恢复用：能塞多少塞多少，塞不下的交回调用方标失败。"""
    accepted: list[int] = []
    rejected: list[int] = []
    for draft_id in draft_ids:
        if submit(draft_id):
            accepted.append(draft_id)
        else:
            rejected.append(draft_id)
    return {"requeued": len(accepted), "rejected": len(rejected)}


def stats() -> dict:
    with _state_lock:
        current = _queue
        started = _started
    with _running_lock:
        running = _running
    return {
        "depth": current.qsize() if current is not None else 0,
        "running": running,
        "max_workers": max_workers(),
        "max_queue": queue_size(),
        "started": started,
    }


def reset() -> None:
    """测试用：停掉队列并清空全部状态。"""
    global _queue, _on_job, _running
    shutdown(wait=True, timeout=5.0)
    with _state_lock:
        _queue = None
        _on_job = None
    with _running_lock:
        _running = 0


def _loop() -> None:
    while True:
        with _state_lock:
            current = _queue
            started = _started
        if current is None or not started:
            return
        try:
            draft_id = current.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            _run_one(draft_id)
        finally:
            current.task_done()


def _run_one(draft_id: int) -> None:
    """跑一个 job。任何异常都在这里吃掉——线程死了队列就永远不动，而且没人知道。"""
    global _running
    with _state_lock:
        job = _on_job
    with _running_lock:
        _running += 1
    try:
        if job is not None:
            job(draft_id)
    except Exception:
        logger.exception(
            "ocr.job_crashed",
            extra={"event": "ocr_job_crashed", "draft_id": int(draft_id)},
        )
    finally:
        with _running_lock:
            _running -= 1
