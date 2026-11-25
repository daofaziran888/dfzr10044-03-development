"""分区切换交接清单领域服务。

分区演化进入双分区并存后，旧分区文件是否已被新分区产物完整覆盖，原先没有
任何交接结果可核对。本服务为每笔演化开立一份交接清单：

- 开立时固化旧分区基线快照（时间旅行仍按旧快照编号读到原 A、B、C 文件）；
- 迁移产物逐文件登记，按「旧文件 -> 新文件」核对覆盖行数，重复到达只记一次，
  因此并存状态区分「已覆盖 3 行 / 仍欠 4 行」，不会把旧 A 和新 A 算成 6 行；
- 切换确认时排查迟到文件：已受理的迟到文件可由新清单临时引用，确认时记清
  它的行数归属于哪一次交接；未处理的迟到文件会阻断确认，旧分区继续承接；
- 确认接口关联实际采用的文件清单与新快照编号，沿新分区读取可核对全部已受理行；
- 归档只标记旧目录文件、不删除，基线快照与时间旅行读取保持可用，被临时引用
  的原文件先保留。

另一张湖表即使使用同样的旧分区键，其文件来源湖表不匹配，不能进入本清单。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.lake_store import PartitionFile, lake_store
from app.store import store

# 旧分区键为日分区（8 位 yyyyMMdd），新分区键带小时（10 位 yyyyMMddHH），
# 与链路与既有演化服务对双分区文件的判定约定保持一致。
OLD_KEY_LENGTH = 8
NEW_KEY_LENGTH = 10

# 交接清单状态
STATUS_PENDING = "待交接"
STATUS_IN_PROGRESS = "交接中"
STATUS_CONFIRMED = "已确认切换"
STATUS_ARCHIVED = "已归档"

COVERAGE_DONE = "已覆盖"
COVERAGE_PENDING = "待覆盖"


@dataclass
class CoverageItem:
    """单个旧分区文件的覆盖记录：一份旧文件最多被一份新产物覆盖。"""
    source_file_id: str
    source_partition: str
    source_rows: int
    new_file_id: str | None = None
    new_rows: int = 0
    handoff_no: int | None = None
    arrival_count: int = 1  # 迁移产物到达次数（重复到达不重复计行）
    status: str = COVERAGE_PENDING
    handled_at: float | None = None


@dataclass
class BorrowedFile:
    """迟到文件在新产物就绪前由新清单临时引用。"""
    source_file_id: str
    source_partition: str
    rows: int
    reason: str
    handoff_no: int | None = None
    borrowed_at: float = field(default_factory=time.time)


@dataclass
class HandoffEvent:
    """交接状态流转事件，供演化详情页跟踪这笔交接。"""
    seq: int
    handoff_no: int | None
    action: str
    detail: str
    at: float


@dataclass
class PartitionHandoff:
    handoff_id: str
    evolve_id: int
    lake_table: str
    old_key: str
    new_key: str
    status: str
    baseline_snapshot_id: str
    baseline_file_ids: list[str]
    coverage: dict[str, CoverageItem]
    borrowed: list[BorrowedFile]
    adopted_file_ids: list[str]
    archived_file_ids: list[str]
    retained_file_ids: list[str]
    confirmed_snapshot_id: str | None
    events: list[HandoffEvent]
    created_at: float
    confirmed_at: float | None = None


class HandoffRegistry:
    """交接清单内存仓库：按 handoff_id 与演化记录 id 双向索引。"""

    def __init__(self) -> None:
        self._handoffs: dict[str, PartitionHandoff] = {}

    def add(self, handoff: PartitionHandoff) -> None:
        self._handoffs[handoff.handoff_id] = handoff

    def get(self, handoff_id: str) -> PartitionHandoff | None:
        return self._handoffs.get(handoff_id)

    def for_evolve(self, evolve_id: int) -> PartitionHandoff | None:
        for handoff in self._handoffs.values():
            if handoff.evolve_id == evolve_id:
                return handoff
        return None

    def list(self, lake_table: str | None = None) -> list[PartitionHandoff]:
        handoffs = list(self._handoffs.values())
        if lake_table:
            handoffs = [h for h in handoffs if h.lake_table == lake_table]
        return handoffs

    def clear(self) -> None:
        self._handoffs.clear()


handoff_registry = HandoffRegistry()


def _is_old_key_file(pf: PartitionFile) -> bool:
    return len(pf.partition_key) == OLD_KEY_LENGTH


def _is_new_key_file(pf: PartitionFile) -> bool:
    return len(pf.partition_key) == NEW_KEY_LENGTH


class PartitionHandoffService:
    """分区切换交接清单领域服务。"""

    # ------------------------------------------------------------------ 开立

    def open_handoff(self, evolve_id: int) -> tuple[dict[str, Any] | None, str]:
        entry = store.find("partition_evolve", evolve_id)
        if entry is None:
            return None, f"演化记录 {evolve_id} 不存在或已归档"
        if handoff_registry.for_evolve(evolve_id) is not None:
            return None, "该笔演化已开立交接清单，不能重复开立"

        lake_table = entry["所属湖表"]
        old_key = entry["原分区键"]
        new_key = entry["新分区键"]
        current_files = lake_store.list_files(lake_table)
        baseline_ids = [f.file_id for f in current_files]

        # 固化旧分区基线快照：之后的时间旅行按这个快照编号仍读到原 A、B、C。
        snapshot = lake_store.create_snapshot(
            lake_table, baseline_ids,
            parent_snapshot=self._latest_available_snapshot(lake_table),
        )
        lake_store.mark_snapshot_available(lake_table, snapshot.snapshot_id)

        coverage = {
            fid: CoverageItem(
                source_file_id=fid,
                source_partition=pf.partition_key,
                source_rows=pf.file_rows,
            )
            for fid, pf in ((f.file_id, f) for f in current_files)
            if _is_old_key_file(pf)
        }
        handoff = PartitionHandoff(
            handoff_id=f"HANDOFF-{uuid.uuid4().hex[:8]}",
            evolve_id=evolve_id,
            lake_table=lake_table,
            old_key=old_key,
            new_key=new_key,
            status=STATUS_PENDING,
            baseline_snapshot_id=snapshot.snapshot_id,
            baseline_file_ids=list(baseline_ids),
            coverage=coverage,
            borrowed=[],
            adopted_file_ids=[],
            archived_file_ids=[],
            retained_file_ids=[],
            confirmed_snapshot_id=None,
            events=[],
            created_at=time.time(),
        )
        self._emit(handoff, "开立清单", (
            f"固化基线快照 {snapshot.snapshot_id}，"
            f"纳入旧分区文件 {len(coverage)} 个、基线文件 {len(baseline_ids)} 个"
        ))
        handoff_registry.add(handoff)
        return self.serialize(handoff), ""

    # ---------------------------------------------------------- 迁移产物登记

    def register_migration(
        self, handoff_id: str, source_file_id: str, new_file_id: str,
    ) -> tuple[dict[str, Any] | None, str]:
        handoff = handoff_registry.get(handoff_id)
        if handoff is None:
            return None, "交接清单不存在"
        if handoff.status in (STATUS_CONFIRMED, STATUS_ARCHIVED):
            return None, "交接已确认切换，迟到迁移产物不再受理，请走迟到文件处置流程"

        source = lake_store.get_file(handoff.lake_table, source_file_id)
        new_file = lake_store.get_file(handoff.lake_table, new_file_id)
        if source is None or new_file is None:
            return None, "源文件或新产物文件在本湖表不存在，无法登记交接"
        # 另一张湖表即使使用同样的旧分区键，来源湖表不匹配也不能进入这次交接。
        if source.source_table != handoff.lake_table or new_file.source_table != handoff.lake_table:
            return None, "文件来源湖表与本交接清单不一致，拒绝跨表交接"
        if not _is_old_key_file(source):
            return None, "源文件不是旧分区键（日分区）文件，不能作为迁移覆盖对象"
        if not _is_new_key_file(new_file):
            return None, "新产物不是新分区键文件，不能作为迁移覆盖结果"
        if source.file_rows != new_file.file_rows:
            return None, (
                f"覆盖行数未对齐：旧文件 {source.file_rows} 行，"
                f"新产物 {new_file.file_rows} 行"
            )

        active_old_ids = {
            f.file_id for f in lake_store.list_files(handoff.lake_table)
            if _is_old_key_file(f)
        }
        if source_file_id not in active_old_ids:
            return None, "源文件已归档或不在当前可读清单内，不能登记交接"

        item = handoff.coverage.get(source_file_id)
        duplicate = False
        if item is None:
            # 基线之后到达的迟到旧文件，首次被迁移产物覆盖时补建覆盖项。
            item = CoverageItem(
                source_file_id=source_file_id,
                source_partition=source.partition_key,
                source_rows=source.file_rows,
            )
            handoff.coverage[source_file_id] = item
        elif item.status == COVERAGE_DONE:
            # 迁移产物到了两次：只增到达次数，覆盖行数仍按一份旧文件计算。
            duplicate = True
            item.arrival_count += 1
            if new_file_id not in self._all_new_file_ids(handoff):
                self._emit(
                    handoff, "重复到达",
                    f"旧文件 {source_file_id}（{source.partition_key}，"
                    f"{source.file_rows} 行）迁移产物重复到达，覆盖行数不重复累计",
                )
            return self._migration_payload(handoff, item, duplicate=True), ""

        item.new_file_id = new_file_id
        item.new_rows = new_file.file_rows
        item.handoff_no = self._next_handoff_no(handoff)
        item.status = COVERAGE_DONE
        item.handled_at = time.time()
        if handoff.status == STATUS_PENDING:
            handoff.status = STATUS_IN_PROGRESS
        self._emit(
            handoff, "迁移交接",
            f"第 {item.handoff_no} 次交接：{source.partition_key} 旧文件 {source_file_id} "
            f"({source.file_rows} 行) 由新产物 {new_file_id} 覆盖",
            handoff_no=item.handoff_no,
        )
        return self._migration_payload(handoff, item, duplicate=False), ""

    # ---------------------------------------------------------- 并存覆盖进度

    def get_coexist_progress(self, handoff_id: str) -> tuple[dict[str, Any] | None, str]:
        handoff = handoff_registry.get(handoff_id)
        if handoff is None:
            return None, "交接清单不存在"

        active_files = lake_store.list_files(handoff.lake_table)
        active_old = [f for f in active_files if _is_old_key_file(f)]
        new_products = [f for f in active_files if _is_new_key_file(f)]

        covered_items = [c for c in handoff.coverage.values() if c.status == COVERAGE_DONE]
        covered_rows = sum(c.source_rows for c in covered_items)
        covered_partitions = {c.source_partition for c in covered_items}

        # 仍欠 = 当前仍可读、没有被覆盖、且未交给新清单临时引用的旧分区文件
        # （含基线欠账与未处理的迟到文件）。
        borrowed_ids = {b.source_file_id for b in handoff.borrowed}
        outstanding_files = [
            f for f in active_old
            if f.file_id not in borrowed_ids
            and (
                handoff.coverage.get(f.file_id) is None
                or handoff.coverage[f.file_id].status != COVERAGE_DONE
            )
        ]
        outstanding_rows = sum(f.file_rows for f in outstanding_files)

        partitions: dict[str, dict[str, Any]] = {}
        for pf in active_old:
            bucket = partitions.setdefault(pf.partition_key, {
                "partition": pf.partition_key,
                "old_rows": 0,
                "covered_rows": 0,
                "outstanding_rows": 0,
                "borrowed_rows": 0,
                "status": COVERAGE_PENDING,
            })
            bucket["old_rows"] += pf.file_rows
            item = handoff.coverage.get(pf.file_id)
            if item is not None and item.status == COVERAGE_DONE:
                bucket["covered_rows"] += item.source_rows
            elif pf.file_id in borrowed_ids:
                bucket["borrowed_rows"] += pf.file_rows
            else:
                bucket["outstanding_rows"] += pf.file_rows
            if bucket["outstanding_rows"] == 0 and (
                bucket["covered_rows"] or bucket["borrowed_rows"]
            ):
                bucket["status"] = "已交接" if bucket["borrowed_rows"] else COVERAGE_DONE
            elif bucket["covered_rows"] or bucket["borrowed_rows"]:
                bucket["status"] = "覆盖中"
        for item in covered_items:
            # 已覆盖但旧文件已不在可读清单（例如已归档）时仍保留分区维度结果。
            partitions.setdefault(item.source_partition, {
                "partition": item.source_partition,
                "old_rows": item.source_rows,
                "covered_rows": item.source_rows,
                "outstanding_rows": 0,
                "borrowed_rows": 0,
                "status": COVERAGE_DONE,
            })

        return {
            "handoff_id": handoff.handoff_id,
            "lake_table": handoff.lake_table,
            "status": handoff.status,
            "covered_rows": covered_rows,
            "outstanding_rows": outstanding_rows,
            "borrowed_rows": sum(b.rows for b in handoff.borrowed),
            "covered_partition_count": len(covered_partitions),
            "outstanding_partitions": sorted({f.partition_key for f in outstanding_files}),
            "old_active_rows": sum(f.file_rows for f in active_old),
            "new_product_rows": sum(f.file_rows for f in new_products),
            "duplicate_arrival_count": sum(
                max(0, c.arrival_count - 1) for c in handoff.coverage.values()
            ),
            "partitions": sorted(partitions.values(), key=lambda x: x["partition"]),
            "readable_snapshot_id": handoff.confirmed_snapshot_id or handoff.baseline_snapshot_id,
        }, ""

    # ---------------------------------------------------------- 迟到文件引用

    def register_borrowed(
        self, handoff_id: str, source_file_id: str,
    ) -> tuple[dict[str, Any] | None, str]:
        handoff = handoff_registry.get(handoff_id)
        if handoff is None:
            return None, "交接清单不存在"
        if handoff.status in (STATUS_CONFIRMED, STATUS_ARCHIVED):
            return None, "交接已确认切换，临时引用通道已关闭"
        source = lake_store.get_file(handoff.lake_table, source_file_id)
        if source is None:
            return None, "迟到文件在本湖表不存在"
        if source.source_table != handoff.lake_table:
            return None, "文件来源湖表与本交接清单不一致，拒绝跨表引用"
        if not _is_old_key_file(source):
            return None, "仅旧分区键的迟到文件可临时引用"
        if source_file_id in handoff.baseline_file_ids:
            return None, "基线旧文件必须走迁移覆盖交接，不能临时引用"
        if any(b.source_file_id == source_file_id for b in handoff.borrowed):
            return self._borrowed_payload(handoff, source_file_id, duplicate=True), ""

        borrowed = BorrowedFile(
            source_file_id=source_file_id,
            source_partition=source.partition_key,
            rows=source.file_rows,
            reason="上一轮已受理文件迟到，新产物就绪前由新清单临时引用",
        )
        handoff.borrowed.append(borrowed)
        if handoff.status == STATUS_PENDING:
            handoff.status = STATUS_IN_PROGRESS
        self._emit(
            handoff, "临时引用",
            f"迟到文件 {source_file_id}（{source.partition_key}，{source.file_rows} 行）"
            "交由新清单临时引用，原日分区文件先保留，归属交接批次待确认时落定",
        )
        return self._borrowed_payload(handoff, source_file_id, duplicate=False), ""

    # -------------------------------------------------------------- 确认切换

    def confirm_switch(self, handoff_id: str) -> tuple[dict[str, Any] | None, str]:
        handoff = handoff_registry.get(handoff_id)
        if handoff is None:
            return None, "交接清单不存在"
        if handoff.status == STATUS_CONFIRMED:
            return None, "交接已确认切换，不能重复确认"
        if handoff.status == STATUS_ARCHIVED:
            return None, "交接已归档"

        progress, _ = self.get_coexist_progress(handoff_id)
        if progress["outstanding_rows"] > 0:
            return None, (
                "仍有旧分区文件未完成覆盖（"
                f"欠 {progress['outstanding_rows']} 行，分区："
                f"{'、'.join(progress['outstanding_partitions']) or '无'}），"
                "当前仍由旧分区承接，待新产物可读并重新确认后再归档"
            )

        # 迟到文件在确认时落定归属：由下一次交接批次承接，记清是哪一次处理。
        for borrowed in handoff.borrowed:
            if borrowed.handoff_no is None:
                borrowed.handoff_no = self._next_handoff_no(handoff)
                self._emit(
                    handoff, "迟到文件处理",
                    f"第 {borrowed.handoff_no} 次交接处理迟到文件 "
                    f"{borrowed.source_file_id}（{borrowed.source_partition}，"
                    f"{borrowed.rows} 行），以临时引用方式进入新分区清单",
                    handoff_no=borrowed.handoff_no,
                )

        adopted_files: list[PartitionFile] = []
        seen_ids: set[str] = set()
        covered_source_ids = {
            c.source_file_id for c in handoff.coverage.values()
            if c.status == COVERAGE_DONE
        }
        for item in handoff.coverage.values():
            if item.status != COVERAGE_DONE or item.new_file_id is None:
                continue
            pf = lake_store.get_file(handoff.lake_table, item.new_file_id)
            if pf is None or pf.source_table != handoff.lake_table:
                return None, f"采用文件 {item.new_file_id} 不可读或来源湖表不符，不能确认切换"
            if pf.file_id not in seen_ids:
                seen_ids.add(pf.file_id)
                adopted_files.append(pf)
        for borrowed in handoff.borrowed:
            # 迟到文件若随后已由迁移产物覆盖，以新产物为准，不再采用原文件（避免双份计行）。
            if borrowed.source_file_id in covered_source_ids:
                continue
            pf = lake_store.get_file(handoff.lake_table, borrowed.source_file_id)
            if pf is None or pf.source_table != handoff.lake_table:
                return None, f"临时引用文件 {borrowed.source_file_id} 不可读，不能确认切换"
            if pf.file_id not in seen_ids:
                seen_ids.add(pf.file_id)
                adopted_files.append(pf)

        adopted_ids = [pf.file_id for pf in adopted_files]
        snapshot = lake_store.create_snapshot(
            handoff.lake_table, adopted_ids,
            parent_snapshot=handoff.baseline_snapshot_id,
            added_rows=sum(pf.file_rows for pf in adopted_files),
        )
        lake_store.mark_snapshot_available(handoff.lake_table, snapshot.snapshot_id)

        handoff.adopted_file_ids = list(adopted_ids)
        handoff.confirmed_snapshot_id = snapshot.snapshot_id
        handoff.confirmed_at = time.time()
        handoff.status = STATUS_CONFIRMED
        self._emit(
            handoff, "确认切换",
            f"新分区快照 {snapshot.snapshot_id} 生效，采用文件 {len(adopted_ids)} 个、"
            f"共 {sum(pf.file_rows for pf in adopted_files)} 行，旧分区转入待归档",
        )
        return {
            "handoff_id": handoff.handoff_id,
            "status": handoff.status,
            "confirmed_snapshot_id": snapshot.snapshot_id,
            "baseline_snapshot_id": handoff.baseline_snapshot_id,
            "adopted_files": [
                {
                    "file_id": pf.file_id,
                    "partition_key": pf.partition_key,
                    "rows": pf.file_rows,
                    "source_table": pf.source_table,
                }
                for pf in adopted_files
            ],
            "adopted_rows": sum(pf.file_rows for pf in adopted_files),
        }, ""

    # ---------------------------------------------------------- 沿新分区读取

    def read_new_partition(self, handoff_id: str) -> tuple[dict[str, Any] | None, str]:
        handoff = handoff_registry.get(handoff_id)
        if handoff is None:
            return None, "交接清单不存在"

        if handoff.status in (STATUS_CONFIRMED, STATUS_ARCHIVED):
            snapshot = lake_store.get_snapshot(
                handoff.lake_table, handoff.confirmed_snapshot_id,
            )
            if snapshot is None:
                return None, "确认快照不存在，无法沿新分区读取"
            files = [
                lake_store.get_file(handoff.lake_table, fid)
                for fid in snapshot.file_ids
            ]
            files = [f for f in files if f is not None and f.source_table == handoff.lake_table]
            return {
                "handoff_id": handoff.handoff_id,
                "status": handoff.status,
                "readable": True,
                "snapshot_id": snapshot.snapshot_id,
                "rows": sum(f.file_rows for f in files),
                "files": [
                    {"file_id": f.file_id, "partition_key": f.partition_key, "rows": f.file_rows}
                    for f in files
                ],
                "message": "沿新分区快照读取",
            }, ""

        # 确认前：只有已交给新清单临时引用的迟到文件可读。
        borrowed_files = [
            lake_store.get_file(handoff.lake_table, b.source_file_id)
            for b in handoff.borrowed
        ]
        borrowed_files = [f for f in borrowed_files if f is not None]
        if not borrowed_files:
            return {
                "handoff_id": handoff.handoff_id,
                "status": handoff.status,
                "readable": False,
                "snapshot_id": None,
                "rows": 0,
                "files": [],
                "message": "切换仍待交接，当前仍由旧分区承接，待新产物可读并重新确认后再归档",
            }, ""
        return {
            "handoff_id": handoff.handoff_id,
            "status": handoff.status,
            "readable": True,
            "snapshot_id": None,
            "rows": sum(f.file_rows for f in borrowed_files),
            "files": [
                {"file_id": f.file_id, "partition_key": f.partition_key, "rows": f.file_rows}
                for f in borrowed_files
            ],
            "message": "仅临时引用文件可读，切换确认后以新分区快照为准",
        }, ""

    # -------------------------------------------------------------- 归档旧目录

    def archive_old_directory(self, handoff_id: str) -> tuple[dict[str, Any] | None, str]:
        handoff = handoff_registry.get(handoff_id)
        if handoff is None:
            return None, "交接清单不存在"
        if handoff.status not in (STATUS_CONFIRMED, STATUS_ARCHIVED):
            return None, "切换仍待交接，旧分区继续承接，不能归档"

        # 被临时引用的迟到原文件先保留；若其新产物随后已完成迁移覆盖，则可归档。
        borrowed_ids = {b.source_file_id for b in handoff.borrowed}
        migrated_ids = {
            c.source_file_id for c in handoff.coverage.values()
            if c.status == COVERAGE_DONE and c.source_file_id in borrowed_ids
        }
        # 归档只针对旧分区键（日分区）文件，新分区产物即使在基线清单里也不动。
        def _is_archivable_old_file(fid: str) -> bool:
            pf = lake_store.get_file(handoff.lake_table, fid)
            return pf is not None and not pf.archived and _is_old_key_file(pf)

        retained = [
            fid for fid in borrowed_ids
            if fid not in migrated_ids
            and lake_store.get_file(handoff.lake_table, fid) is not None
        ]
        targets = [
            fid for fid in handoff.baseline_file_ids
            if fid not in borrowed_ids and _is_archivable_old_file(fid)
        ]
        targets.extend(
            fid for fid in migrated_ids
            if fid not in targets and _is_archivable_old_file(fid)
        )
        archived_ids = lake_store.archive_files(handoff.lake_table, targets)
        handoff.archived_file_ids.extend(
            fid for fid in archived_ids if fid not in handoff.archived_file_ids
        )
        handoff.retained_file_ids = retained
        handoff.status = STATUS_ARCHIVED
        self._emit(
            handoff, "归档旧目录",
            f"归档旧分区文件 {len(archived_ids)} 个；临时引用原文件 {len(retained)} 个先保留；"
            f"基线快照 {handoff.baseline_snapshot_id} 仍可供时间旅行读取",
        )
        return {
            "handoff_id": handoff.handoff_id,
            "status": handoff.status,
            "archived_file_ids": archived_ids,
            "retained_file_ids": retained,
            "baseline_snapshot_id": handoff.baseline_snapshot_id,
        }, ""

    # ------------------------------------------------------------------ 查询

    def list_handoffs(self, lake_table: str | None = None) -> list[dict[str, Any]]:
        return [self.serialize(h) for h in handoff_registry.list(lake_table)]

    def get_handoff(self, handoff_id: str) -> dict[str, Any] | None:
        handoff = handoff_registry.get(handoff_id)
        return self.serialize(handoff) if handoff else None

    def handoff_for_evolve(self, evolve_id: int) -> dict[str, Any] | None:
        handoff = handoff_registry.for_evolve(evolve_id)
        return self.serialize(handoff) if handoff else None

    def serialize(self, handoff: PartitionHandoff) -> dict[str, Any]:
        return {
            "handoff_id": handoff.handoff_id,
            "evolve_id": handoff.evolve_id,
            "lake_table": handoff.lake_table,
            "old_key": handoff.old_key,
            "new_key": handoff.new_key,
            "status": handoff.status,
            "baseline_snapshot_id": handoff.baseline_snapshot_id,
            "confirmed_snapshot_id": handoff.confirmed_snapshot_id,
            "baseline_file_ids": list(handoff.baseline_file_ids),
            "adopted_file_ids": list(handoff.adopted_file_ids),
            "archived_file_ids": list(handoff.archived_file_ids),
            "retained_file_ids": list(handoff.retained_file_ids),
            "coverage": [
                {
                    "source_file_id": c.source_file_id,
                    "source_partition": c.source_partition,
                    "source_rows": c.source_rows,
                    "new_file_id": c.new_file_id,
                    "new_rows": c.new_rows,
                    "handoff_no": c.handoff_no,
                    "arrival_count": c.arrival_count,
                    "status": c.status,
                    "handled_at": c.handled_at,
                }
                for c in handoff.coverage.values()
            ],
            "borrowed": [
                {
                    "source_file_id": b.source_file_id,
                    "source_partition": b.source_partition,
                    "rows": b.rows,
                    "reason": b.reason,
                    "handoff_no": b.handoff_no,
                }
                for b in handoff.borrowed
            ],
            "events": [
                {
                    "seq": e.seq,
                    "handoff_no": e.handoff_no,
                    "action": e.action,
                    "detail": e.detail,
                    "at": e.at,
                }
                for e in handoff.events
            ],
            "created_at": handoff.created_at,
            "confirmed_at": handoff.confirmed_at,
        }

    # ------------------------------------------------------------------ 辅助

    def _emit(
        self, handoff: PartitionHandoff, action: str, detail: str,
        handoff_no: int | None = None,
    ) -> HandoffEvent:
        event = HandoffEvent(
            seq=len(handoff.events) + 1,
            handoff_no=handoff_no,
            action=action,
            detail=detail,
            at=time.time(),
        )
        handoff.events.append(event)
        return event

    def _next_handoff_no(self, handoff: PartitionHandoff) -> int:
        used = [e.handoff_no for e in handoff.events if e.handoff_no is not None]
        return (max(used) if used else 0) + 1

    def _all_new_file_ids(self, handoff: PartitionHandoff) -> list[str]:
        return [
            c.new_file_id for c in handoff.coverage.values()
            if c.new_file_id is not None
        ]

    def _latest_available_snapshot(self, lake_table: str) -> str | None:
        available = [
            s for s in lake_store.list_snapshots(lake_table)
            if s.status == "available"
        ]
        return available[-1].snapshot_id if available else None

    def _migration_payload(
        self, handoff: PartitionHandoff, item: CoverageItem, duplicate: bool,
    ) -> dict[str, Any]:
        progress, _ = self.get_coexist_progress(handoff.handoff_id)
        return {
            "handoff_id": handoff.handoff_id,
            "status": handoff.status,
            "duplicate_arrival": duplicate,
            "coverage": {
                "source_file_id": item.source_file_id,
                "source_partition": item.source_partition,
                "source_rows": item.source_rows,
                "new_file_id": item.new_file_id,
                "new_rows": item.new_rows,
                "handoff_no": item.handoff_no,
                "arrival_count": item.arrival_count,
            },
            "progress": progress,
        }

    def _borrowed_payload(
        self, handoff: PartitionHandoff, source_file_id: str, duplicate: bool,
    ) -> dict[str, Any]:
        borrowed = next(b for b in handoff.borrowed if b.source_file_id == source_file_id)
        return {
            "handoff_id": handoff.handoff_id,
            "status": handoff.status,
            "duplicate_registration": duplicate,
            "borrowed": {
                "source_file_id": borrowed.source_file_id,
                "source_partition": borrowed.source_partition,
                "rows": borrowed.rows,
                "handoff_no": borrowed.handoff_no,
            },
        }
