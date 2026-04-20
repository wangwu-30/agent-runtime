import asyncio
import pytest
from agent_runtime.contracts import TaskState
from agent_runtime.kernel.scheduler import Scheduler


@pytest.mark.asyncio
async def test_enqueue_dequeue_order():
    s = Scheduler()
    await s.enqueue("task-low", priority=1.0)
    await s.enqueue("task-high", priority=10.0)
    await s.enqueue("task-mid", priority=5.0)

    first = await s.dequeue()
    second = await s.dequeue()
    third = await s.dequeue()

    assert first == "task-high"
    assert second == "task-mid"
    assert third == "task-low"


@pytest.mark.asyncio
async def test_dequeue_empty_returns_none():
    s = Scheduler()
    result = await s.dequeue()
    assert result is None


@pytest.mark.asyncio
async def test_on_complete_prevents_dequeue():
    s = Scheduler()
    await s.enqueue("task-1", priority=1.0)
    await s.on_complete("task-1", TaskState.FAILED)
    result = await s.dequeue()
    assert result is None


@pytest.mark.asyncio
async def test_on_resume_reenqueues():
    s = Scheduler()
    await s.enqueue("task-1", priority=5.0)
    tid = await s.dequeue()
    assert tid == "task-1"

    await s.on_yield("task-1", TaskState.SUSPENDED, resume_condition=object())
    await s.on_resume("task-1")

    result = await s.dequeue()
    assert result == "task-1"


@pytest.mark.asyncio
async def test_on_yield_suspended_requires_resume_condition():
    s = Scheduler()
    await s.enqueue("task-1", priority=1.0)
    with pytest.raises(ValueError, match="resume_condition is required"):
        await s.on_yield("task-1", TaskState.SUSPENDED, resume_condition=None)


@pytest.mark.asyncio
async def test_heartbeat_updates_timestamp():
    s = Scheduler()
    await s.enqueue("task-1", priority=1.0)
    await s.dequeue()
    await s.heartbeat("task-1")
    timed_out = await s.check_timeouts()
    assert "task-1" not in timed_out


@pytest.mark.asyncio
async def test_check_timeouts_detects_stale():
    from datetime import datetime, timedelta, timezone
    s = Scheduler()
    await s.enqueue("task-stale", priority=1.0)
    await s.dequeue()
    # Manually backdate the heartbeat
    s._heartbeats["task-stale"] = datetime.now(timezone.utc) - timedelta(seconds=60)
    timed_out = await s.check_timeouts()
    assert "task-stale" in timed_out


def test_compute_priority():
    p = Scheduler.compute_priority(10.0, depth=0)
    assert p == 10.0
    p2 = Scheduler.compute_priority(10.0, depth=1)
    assert abs(p2 - 9.0) < 1e-9
