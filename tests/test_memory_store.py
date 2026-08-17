from pathlib import Path

from chao.memory.store import MemoryStore


class FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


def make_store(tmp_path: Path, *, clock: FakeClock | None = None) -> MemoryStore:
    return MemoryStore(tmp_path / "test.db", clock=clock or FakeClock())


async def test_touch_viewer_creates_a_new_row(tmp_path: Path):
    clock = FakeClock(1000.0)
    store = make_store(tmp_path, clock=clock)

    viewer_id = await store.touch_viewer("someuser", "SomeUser")

    summary = await store.viewer_summary("someuser")
    assert summary is not None
    assert summary.id == viewer_id
    assert summary.interactions == 1
    assert summary.days_seen == 1
    assert summary.first_seen == 1000.0


async def test_touch_viewer_increments_interactions_on_repeat(tmp_path: Path):
    clock = FakeClock(1000.0)
    store = make_store(tmp_path, clock=clock)

    await store.touch_viewer("someuser", "SomeUser")
    clock.now += 10.0
    await store.touch_viewer("someuser", "SomeUser")

    summary = await store.viewer_summary("someuser")
    assert summary.interactions == 2


async def test_touch_viewer_bumps_days_seen_only_once_per_calendar_day(tmp_path: Path):
    clock = FakeClock(0.0)  # 1970-01-01
    store = make_store(tmp_path, clock=clock)

    await store.touch_viewer("someuser", None)
    clock.now += 3600.0  # same day
    await store.touch_viewer("someuser", None)
    clock.now += 3600.0  # still same day
    await store.touch_viewer("someuser", None)

    summary = await store.viewer_summary("someuser")
    assert summary.interactions == 3
    assert summary.days_seen == 1

    clock.now += 86400.0 * 2  # a later calendar day
    await store.touch_viewer("someuser", None)

    summary = await store.viewer_summary("someuser")
    assert summary.interactions == 4
    assert summary.days_seen == 2


async def test_touch_viewer_preserves_display_name_when_not_provided(tmp_path: Path):
    store = make_store(tmp_path)

    await store.touch_viewer("someuser", "SomeUser")
    await store.touch_viewer("someuser", None)

    cur = store._conn.execute("SELECT display_name FROM viewers WHERE login = ?", ("someuser",))
    assert cur.fetchone()[0] == "SomeUser"


async def test_viewer_summary_returns_none_for_unknown_login(tmp_path: Path):
    store = make_store(tmp_path)

    assert await store.viewer_summary("nobody") is None


async def test_recent_episodes_orders_newest_first_and_respects_limit(tmp_path: Path):
    clock = FakeClock(1000.0)
    store = make_store(tmp_path, clock=clock)
    viewer_id = await store.touch_viewer("someuser", None)

    for i in range(5):
        clock.now += 1.0
        await store.write_episode(
            source="chat", viewer_id=viewer_id, content=f"msg {i}", chao_response=f"reply {i}"
        )

    rows = await store.recent_episodes(viewer_id, limit=2)

    assert [r.content for r in rows] == ["msg 4", "msg 3"]


async def test_write_episode_allows_null_viewer_and_null_response(tmp_path: Path):
    store = make_store(tmp_path)

    await store.write_episode(
        source="ambient", viewer_id=None, content="thinking aloud", chao_response=None
    )

    cur = store._conn.execute("SELECT source, viewer_id, chao_response FROM episodes")
    row = cur.fetchone()
    assert row == ("ambient", None, None)


async def test_store_creates_parent_directory(tmp_path: Path):
    nested = tmp_path / "nested" / "dir" / "test.db"
    store = MemoryStore(nested, clock=FakeClock())

    assert nested.parent.exists()
    await store.touch_viewer("someuser", None)  # doesn't raise
