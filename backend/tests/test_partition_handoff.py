"""分区切换交接清单测试：覆盖 A/B 迁移、重复到达、迟到 C、确认、归档与时间旅行。

叙事背景：
- 旧分区 A（日分区 20261101）3 行、B（日分区 20261102）4 行；
- A 的迁移产物到达两次，B 迁完前并存状态要区分「覆盖 3 行 / 仍欠 4 行」，
  不能把旧 A 和新 A 算成 6 行；
- 上一轮已受理的 C（2 行）在确认切换时迟到，需看清它的 2 行由哪次交接处理；
- 确认后沿新分区读到全部已受理行，按旧快照编号仍读到 A、B 共 7 行；
- 另一张湖表即使使用同样的旧分区键，文件也不能进入这次交接。
"""
from __future__ import annotations

import pytest

from app.lake_store import lake_store
from app.services.partition_evolve import PartitionEvolveService
from app.services.partition_handoff import (
    STATUS_CONFIRMED,
    STATUS_IN_PROGRESS,
    STATUS_PENDING,
    PartitionHandoffService,
    handoff_registry,
)
from app.services.time_travel import TimeTravelService
from app.store import store


@pytest.fixture(autouse=True)
def isolated_stores():
    """每个用例使用干净的交接清单与湖表存储。"""
    handoff_registry.clear()
    lake_store._files.clear()
    lake_store._snapshots.clear()
    yield
    handoff_registry.clear()
    lake_store._files.clear()
    lake_store._snapshots.clear()


def _make_evolve_entry(table: str = "ev_table") -> int:
    svc = PartitionEvolveService()
    entry, missing = svc.create_entry({
        "演化编号": f"EVOL-T-{table}",
        "所属湖表": table,
        "原分区键": "partition_dt",
        "新分区键": "partition_hh",
    })
    assert entry is not None and not missing
    svc.run_action(entry["id"], "启动演化")
    return entry["id"]


class TestHandoffLifecycle:
    def test_full_ab_migration_duplicate_arrival_late_c_and_archive(self):
        # ---- 旧分区基线：A 3 行、B 4 行 ----
        file_a = lake_store.register_file("ev_table", "20261101", rows=3, bytes_=300)
        file_b = lake_store.register_file("ev_table", "20261102", rows=4, bytes_=400)

        evolve_id = _make_evolve_entry()
        handoff_svc = PartitionHandoffService()
        opened, error = handoff_svc.open_handoff(evolve_id)
        assert error == ""
        handoff_id = opened["handoff_id"]
        baseline_snapshot_id = opened["baseline_snapshot_id"]
        assert set(opened["baseline_file_ids"]) == {file_a.file_id, file_b.file_id}

        # ---- A 的迁移产物到达，B 尚未迁完 ----
        new_a = lake_store.register_file("ev_table", "2026110100", rows=3, bytes_=300)
        payload, error = handoff_svc.register_migration(
            handoff_id, file_a.file_id, new_a.file_id)
        assert error == ""
        assert payload["coverage"]["handoff_no"] == 1
        progress, _ = handoff_svc.get_coexist_progress(handoff_id)
        assert progress["status"] == STATUS_IN_PROGRESS
        assert progress["covered_rows"] == 3          # 只覆盖 A 的 3 行
        assert progress["outstanding_rows"] == 4      # B 的 4 行仍欠
        # 旧 A + 新 A 不能算成 6 行：旧侧仍是 7 行，新侧 3 行分开统计
        assert progress["old_active_rows"] == 7
        assert progress["new_product_rows"] == 3
        partition_map = {p["partition"]: p for p in progress["partitions"]}
        assert partition_map["20261101"]["status"] == "已覆盖"
        assert partition_map["20261102"]["status"] == "待覆盖"

        # ---- A 的迁移产物第二次到达：幂等，不重复计行 ----
        new_a_dup = lake_store.register_file("ev_table", "2026110101", rows=3, bytes_=300)
        payload, error = handoff_svc.register_migration(
            handoff_id, file_a.file_id, new_a_dup.file_id)
        assert error == ""
        assert payload["duplicate_arrival"] is True
        progress, _ = handoff_svc.get_coexist_progress(handoff_id)
        assert progress["covered_rows"] == 3
        assert progress["outstanding_rows"] == 4
        assert progress["duplicate_arrival_count"] == 1

        # ---- B 未迁完时确认切换被拒 ----
        result, error = handoff_svc.confirm_switch(handoff_id)
        assert result is None
        assert "欠 4 行" in error
        assert "旧分区承接" in error

        # ---- B 迁完 ----
        new_b = lake_store.register_file("ev_table", "2026110200", rows=4, bytes_=400)
        _, error = handoff_svc.register_migration(
            handoff_id, file_b.file_id, new_b.file_id)
        assert error == ""
        progress, _ = handoff_svc.get_coexist_progress(handoff_id)
        assert progress["covered_rows"] == 7
        assert progress["outstanding_rows"] == 0

        # ---- 迟到 C（上一轮已受理，2 行）：确认前只有旧日分区文件可读 ----
        file_c = lake_store.register_file("ev_table", "20261103", rows=2, bytes_=200)
        read_before, _ = handoff_svc.read_new_partition(handoff_id)
        assert read_before["readable"] is False
        assert "仍由旧分区承接" in read_before["message"]

        # C 未交给新清单时，确认切换被其 2 行阻断
        result, error = handoff_svc.confirm_switch(handoff_id)
        assert result is None
        assert "欠 2 行" in error

        # 交给新清单临时引用：可读 2 行
        borrowed, error = handoff_svc.register_borrowed(handoff_id, file_c.file_id)
        assert error == ""
        read_borrowed, _ = handoff_svc.read_new_partition(handoff_id)
        assert read_borrowed["readable"] is True
        assert read_borrowed["rows"] == 2

        # ---- 确认切换：C 的 2 行由第 3 次交接处理 ----
        confirmed, error = handoff_svc.confirm_switch(handoff_id)
        assert error == ""
        assert confirmed["status"] == STATUS_CONFIRMED
        assert confirmed["adopted_rows"] == 9  # 3 + 4 + 2
        assert {f["file_id"] for f in confirmed["adopted_files"]} == {
            new_a.file_id, new_b.file_id, file_c.file_id,
        }
        switch_snapshot_id = confirmed["confirmed_snapshot_id"]
        assert confirmed["baseline_snapshot_id"] == baseline_snapshot_id
        detail = handoff_svc.get_handoff(handoff_id)
        c_event = next(
            e for e in detail["events"]
            if file_c.file_id in e["detail"] and e["handoff_no"] is not None
        )
        assert c_event["handoff_no"] == 3
        assert "2 行" in c_event["detail"]
        confirm_event = next(e for e in detail["events"] if e["action"] == "确认切换")
        assert switch_snapshot_id in confirm_event["detail"]

        # ---- 确认后沿新分区读取：全部已受理 9 行 ----
        read_after, _ = handoff_svc.read_new_partition(handoff_id)
        assert read_after["readable"] is True
        assert read_after["snapshot_id"] == switch_snapshot_id
        assert read_after["rows"] == 9

        # ---- 旧快照编号时间旅行：仍是 A、B 共 7 行，不含迟到 C ----
        tt = TimeTravelService()
        old_read = tt.read_at_snapshot("ev_table", baseline_snapshot_id)
        assert old_read["success"] is True
        assert old_read["rows"] == 7
        assert set(old_read["partitions_hit"]) == {"20261101", "20261102"}

        # ---- 归档旧目录：A、B 归档，C 原文件先保留 ----
        archived, error = handoff_svc.archive_old_directory(handoff_id)
        assert error == ""
        assert set(archived["archived_file_ids"]) == {file_a.file_id, file_b.file_id}
        assert archived["retained_file_ids"] == [file_c.file_id]
        # 常规清单不再看到 A、B，但文件实体仍可经快照解析
        active = lake_store.list_files("ev_table")
        assert file_a.file_id not in {f.file_id for f in active}
        assert file_b.file_id not in {f.file_id for f in active}
        assert lake_store.get_file("ev_table", file_a.file_id).archived is True
        assert lake_store.get_file("ev_table", file_c.file_id).archived is False
        all_files = lake_store.list_files("ev_table", include_archived=True)
        assert {f.file_id for f in all_files} >= {file_a.file_id, file_b.file_id}
        # 归档后旧快照时间旅行仍读到 7 行
        old_read_after_archive = tt.read_at_snapshot("ev_table", baseline_snapshot_id)
        assert old_read_after_archive["rows"] == 7
        # 新分区读取仍为 9 行（快照可解析被归档之外的采用文件）
        read_final, _ = handoff_svc.read_new_partition(handoff_id)
        assert read_final["rows"] == 9

    def test_foreign_table_file_cannot_enter_handoff(self):
        file_a = lake_store.register_file("ev_table", "20261101", rows=3, bytes_=300)
        # 另一张湖表使用同样的旧分区键
        lake_store.register_file("other_table", "20261101", rows=3, bytes_=300)
        # 以及挂在本表命名空间但来源湖表是别表的文件
        foreign = lake_store.register_file(
            "ev_table", "2026110100", rows=3, bytes_=300, source_table="other_table")

        evolve_id = _make_evolve_entry()
        handoff_svc = PartitionHandoffService()
        opened, _ = handoff_svc.open_handoff(evolve_id)
        handoff_id = opened["handoff_id"]

        # 别表文件不在本湖表，登记直接被拒
        foreign_other = lake_store.list_files("other_table")[0]
        _, error = handoff_svc.register_migration(
            handoff_id, file_a.file_id, foreign_other.file_id)
        assert "不存在" in error
        # 来源湖表不匹配的本命名空间文件同样被拒
        _, error = handoff_svc.register_migration(
            handoff_id, file_a.file_id, foreign.file_id)
        assert "来源湖表" in error
        _, error = handoff_svc.register_borrowed(handoff_id, foreign.file_id)
        assert "来源湖表" in error

        progress, _ = handoff_svc.get_coexist_progress(handoff_id)
        assert progress["covered_rows"] == 0

    def test_row_count_mismatch_rejected(self):
        file_a = lake_store.register_file("ev_table", "20261101", rows=3, bytes_=300)
        bad_new = lake_store.register_file("ev_table", "2026110100", rows=2, bytes_=200)
        evolve_id = _make_evolve_entry()
        handoff_svc = PartitionHandoffService()
        opened, _ = handoff_svc.open_handoff(evolve_id)
        _, error = handoff_svc.register_migration(
            opened["handoff_id"], file_a.file_id, bad_new.file_id)
        assert "覆盖行数未对齐" in error

    def test_evolve_confirm_blocked_until_handoff_confirmed(self):
        lake_store.register_file("ev_table", "20261101", rows=3, bytes_=300)
        evolve_id = _make_evolve_entry()
        evolve_svc = PartitionEvolveService()

        # 没有交接清单时确认完成被拒
        entry, message = evolve_svc.run_action(evolve_id, "确认完成")
        assert entry is None and "交接清单" in message

        handoff_svc = PartitionHandoffService()
        opened, _ = handoff_svc.open_handoff(evolve_id)
        # 清单待交接时仍被拒
        entry, message = evolve_svc.run_action(evolve_id, "确认完成")
        assert entry is None and "待交接" in message

        # 制造一个无欠账的清单并确认
        old_file = lake_store.list_files("ev_table", "20261101")[0]
        new_file = lake_store.register_file("ev_table", "2026110100", rows=3, bytes_=300)
        handoff_svc.register_migration(opened["handoff_id"], old_file.file_id, new_file.file_id)
        _, error = handoff_svc.confirm_switch(opened["handoff_id"])
        assert error == ""
        entry, message = evolve_svc.run_action(evolve_id, "确认完成")
        assert entry is not None and entry["status"] == "已完成"

    def test_evolve_detail_tracks_handoff_status_flow(self):
        file_a = lake_store.register_file("ev_table", "20261101", rows=3, bytes_=300)
        evolve_id = _make_evolve_entry()
        handoff_svc = PartitionHandoffService()
        evolve_svc = PartitionEvolveService()
        opened, _ = handoff_svc.open_handoff(evolve_id)
        new_a = lake_store.register_file("ev_table", "2026110100", rows=3, bytes_=300)
        handoff_svc.register_migration(opened["handoff_id"], file_a.file_id, new_a.file_id)

        detail, error = evolve_svc.evolve_detail(evolve_id)
        assert error == ""
        assert detail["evolve"]["id"] == evolve_id
        assert detail["handoff"]["handoff_id"] == opened["handoff_id"]
        actions = [e["action"] for e in detail["handoff"]["events"]]
        assert actions == ["开立清单", "迁移交接"]
        assert detail["progress"]["covered_rows"] == 3

    def test_cannot_open_duplicate_handoff_or_register_after_confirm(self):
        lake_store.register_file("ev_table", "20261101", rows=3, bytes_=300)
        evolve_id = _make_evolve_entry()
        handoff_svc = PartitionHandoffService()
        opened, _ = handoff_svc.open_handoff(evolve_id)
        _, error = handoff_svc.open_handoff(evolve_id)
        assert "重复开立" in error

        old_file = lake_store.list_files("ev_table", "20261101")[0]
        new_file = lake_store.register_file("ev_table", "2026110100", rows=3, bytes_=300)
        handoff_svc.register_migration(opened["handoff_id"], old_file.file_id, new_file.file_id)
        handoff_svc.confirm_switch(opened["handoff_id"])

        late = lake_store.register_file("ev_table", "20261102", rows=2, bytes_=200)
        _, error = handoff_svc.register_borrowed(opened["handoff_id"], late.file_id)
        assert "临时引用通道已关闭" in error
        _, error = handoff_svc.archive_old_directory(opened["handoff_id"])
        assert error == ""
        # 已归档清单不可再次确认
        _, error = handoff_svc.confirm_switch(opened["handoff_id"])
        assert "已归档" in error

    def test_borrow_requires_non_baseline_old_file(self):
        file_a = lake_store.register_file("ev_table", "20261101", rows=3, bytes_=300)
        evolve_id = _make_evolve_entry()
        handoff_svc = PartitionHandoffService()
        opened, _ = handoff_svc.open_handoff(evolve_id)
        # 基线文件必须走迁移覆盖，不能临时引用
        _, error = handoff_svc.register_borrowed(opened["handoff_id"], file_a.file_id)
        assert "迁移覆盖" in error
        assert opened["status"] == STATUS_PENDING

    def test_borrowed_file_migrated_later_is_adopted_once_and_archived(self):
        evolve_id = _make_evolve_entry()
        handoff_svc = PartitionHandoffService()
        opened, _ = handoff_svc.open_handoff(evolve_id)
        hid = opened["handoff_id"]

        # 迟到 C 先临时引用，随后新产物也就绪
        file_c = lake_store.register_file("ev_table", "20261103", rows=2, bytes_=200)
        handoff_svc.register_borrowed(hid, file_c.file_id)
        new_c = lake_store.register_file("ev_table", "2026110300", rows=2, bytes_=200)
        _, error = handoff_svc.register_migration(hid, file_c.file_id, new_c.file_id)
        assert error == ""

        confirmed, error = handoff_svc.confirm_switch(hid)
        assert error == ""
        # 同一份 2 行数据只采用一次，新产物优先
        assert confirmed["adopted_rows"] == 2
        assert [f["file_id"] for f in confirmed["adopted_files"]] == [new_c.file_id]

        archived, error = handoff_svc.archive_old_directory(hid)
        assert error == ""
        # 原文件已被新产物覆盖，可归档且不再保留
        assert archived["archived_file_ids"] == [file_c.file_id]
        assert archived["retained_file_ids"] == []
        assert lake_store.get_file("ev_table", file_c.file_id).archived is True

    def test_archive_only_touches_old_key_files(self):
        # 开立清单时湖表里已存在一个新分区键文件，归档不能波及它
        file_a = lake_store.register_file("ev_table", "20261101", rows=3, bytes_=300)
        pre_existing_new = lake_store.register_file("ev_table", "2026110100", rows=3, bytes_=300)
        evolve_id = _make_evolve_entry()
        handoff_svc = PartitionHandoffService()
        opened, _ = handoff_svc.open_handoff(evolve_id)
        hid = opened["handoff_id"]
        assert pre_existing_new.file_id in opened["baseline_file_ids"]

        new_a = lake_store.register_file("ev_table", "2026110101", rows=3, bytes_=300)
        handoff_svc.register_migration(hid, file_a.file_id, new_a.file_id)
        handoff_svc.confirm_switch(hid)
        archived, error = handoff_svc.archive_old_directory(hid)
        assert error == ""
        assert archived["archived_file_ids"] == [file_a.file_id]
        assert lake_store.get_file("ev_table", pre_existing_new.file_id).archived is False
        assert lake_store.get_file("ev_table", new_a.file_id).archived is False
