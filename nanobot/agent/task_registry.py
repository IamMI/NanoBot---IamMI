"""
task_registry.py
----------------
管理后台 asyncio Task 的生命周期。
全局单例，供 tool 层启动/停止/查询后台任务。
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto

logger = logging.getLogger(__name__)


class TaskState(Enum):
    RUNNING  = auto()
    STOPPED  = auto()   
    FAILED   = auto()   


@dataclass
class TaskEntry:
    name: str
    task: asyncio.Task
    state: TaskState = TaskState.RUNNING
    started_at: datetime = field(default_factory=datetime.now)
    stopped_at: datetime | None = None
    error: str | None = None


class TaskRegistry:
    """
    管理具名后台 Task。

    用法：
        registry = TaskRegistry()
        registry.register("slurm_watcher", asyncio.create_task(watcher.start()))
        registry.cancel("slurm_watcher")
        registry.status("slurm_watcher")
    """

    def __init__(self):
        self._tasks: dict[str, TaskEntry] = {}

    def register(
        self,
        name: str,
        task: asyncio.Task,
        on_exit: callable = None,
    ) -> None:
        """
        注册一个后台 task。
        on_exit(name, error_or_none) 在 task 结束时被调用（无论正常/异常）。
        """
        # 如果已有同名 task 还在跑，先取消
        if name in self._tasks:
            existing = self._tasks[name]
            if not existing.task.done():
                logger.warning("注册 %s 时发现旧 task 仍在运行，强制取消", name)
                existing.task.cancel()

        entry = TaskEntry(name=name, task=task)
        self._tasks[name] = entry

        # 挂载完成回调
        def _on_done(t: asyncio.Task):
            entry.stopped_at = datetime.now()
            if t.cancelled():
                entry.state = TaskState.STOPPED
                logger.info("Task %s 已停止", name)
            elif t.exception():
                entry.state = TaskState.FAILED
                entry.error = str(t.exception())
                logger.error("Task %s 异常退出: %s", name, entry.error)
            else:
                entry.state = TaskState.STOPPED
                logger.info("Task %s 正常结束", name)

            if on_exit:
                try:
                    result = on_exit(name, entry.error)
                    # on_exit 是 async 函数时，用 create_task 调度执行
                    # done callback 是同步上下文，不能直接 await
                    if asyncio.iscoroutine(result):
                        asyncio.create_task(result)
                except Exception:
                    logger.exception("on_exit 回调异常")

        task.add_done_callback(_on_done)
        logger.info("Task %s 已注册并启动", name)

    async def cancel(self, name: str) -> bool:
        """取消指定 task，返回是否成功找到并取消。"""
        entry = self._tasks.get(name)
        if not entry:
            logger.warning("Task %s 不存在", name)
            return False
        if entry.task.done():
            logger.info("Task %s 已经结束，无需取消", name)
            return False
        entry.task.cancel()
        try:
            await entry.task
        except (asyncio.CancelledError, Exception):
            pass   
        return True

    def status(self, name: str) -> dict | None:
        """返回指定 task 的状态字典，不存在返回 None。"""
        entry = self._tasks.get(name)
        if not entry:
            return None
        return {
            "name": entry.name,
            "state": entry.state.name,
            "started_at": entry.started_at.strftime("%Y-%m-%d %H:%M:%S"),
            "stopped_at": entry.stopped_at.strftime("%Y-%m-%d %H:%M:%S") if entry.stopped_at else None,
            "error": entry.error,
        }

    def all_status(self) -> list[dict]:
        """返回所有 task 的状态列表。"""
        return [self.status(name) for name in self._tasks]

    def is_running(self, name: str) -> bool:
        entry = self._tasks.get(name)
        return bool(entry and entry.state == TaskState.RUNNING and not entry.task.done())
