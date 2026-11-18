"""内存数据仓库：支持模块间关联、版本管理、模拟表数据、血缘记录与状态流转。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

import threading
from copy import deepcopy
from datetime import datetime
from typing import Any

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 模拟真实表数据（分层存储的实际行数据）
        self._table_data: dict[str, list[dict[str, Any]]] = {}
        # 版本历史: "module:id" -> [{version, timestamp, data}, ...]
        self._versions: dict[str, list[dict[str, Any]]] = {}
        # 血缘边记录
        self._lineage_edges: list[dict[str, Any]] = []
        # 质量稽核结果
        self._quality_results: dict[str, list[dict[str, Any]]] = {}
        # 快照冻结数据: "snapshot_id" -> {table_name, rows, frozen_at}
        self._snapshot_data: dict[str, dict[str, Any]] = {}
        # 归档记录
        self._archive_records: list[dict[str, Any]] = []
        # 补数区间记录（按补数任务编号）
        self._backfill_partitions: dict[str, list[dict[str, Any]]] = {}
        # 补数分区结果集：按目标表名保存唯一完整版本
        self._backfill_results: dict[str, dict[str, Any]] = {}
        # 结果集写锁：同一张表的并发批量写入串行化，只保留一个完整版本
        self._backfill_locks: dict[str, threading.RLock] = {}
        self._backfill_locks_guard = threading.Lock()

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by_field(self, module: str, field: str, value: Any) -> dict[str, Any] | None:
        for row in self.rows(module):
            if row.get(field) == value:
                return row
        return None

    def find_all_by_field(self, module: str, field: str, value: Any) -> list[dict[str, Any]]:
        return [row for row in self.rows(module) if row.get(field) == value]

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}

    # ---- 模拟真实表数据 ----

    def table_rows(self, table_name: str) -> list[dict[str, Any]]:
        return self._table_data.setdefault(table_name, [])

    def set_table_rows(self, table_name: str, rows: list[dict[str, Any]]) -> None:
        self._table_data[table_name] = rows

    def append_table_rows(self, table_name: str, rows: list[dict[str, Any]]) -> None:
        self._table_data.setdefault(table_name, []).extend(rows)

    def upsert_table_partition(
        self, table_name: str, partition_field: str, partition_value: str,
        rows: list[dict[str, Any]],
    ) -> None:
        """分区级写入：只覆盖目标分区，相邻补数任务重叠扫描时不再覆盖其他分区/其他天结果。"""
        current = self._table_data.setdefault(table_name, [])
        kept = [row for row in current if str(row.get(partition_field, "")) != str(partition_value)]
        kept.extend(rows)
        self._table_data[table_name] = kept

    # ---- 版本管理 ----

    def _version_key(self, module: str, entry_id: int) -> str:
        return f"{module}:{entry_id}"

    def save_version(self, module: str, entry_id: int, data: dict[str, Any]) -> dict[str, Any]:
        key = self._version_key(module, entry_id)
        versions = self._versions.setdefault(key, [])
        version = {
            "version": len(versions) + 1,
            "timestamp": datetime.now().isoformat(),
            "data": dict(data),
        }
        versions.append(version)
        return version

    def get_versions(self, module: str, entry_id: int) -> list[dict[str, Any]]:
        key = self._version_key(module, entry_id)
        return list(self._versions.get(key, []))

    def get_latest_version(self, module: str, entry_id: int) -> dict[str, Any] | None:
        versions = self.get_versions(module, entry_id)
        return versions[-1] if versions else None

    # ---- 血缘 ----

    def add_lineage(self, upstream: str, downstream: str, field_map: str,
                    transform_logic: str = "", batch_id: str = "") -> dict[str, Any]:
        edge = {
            "id": len(self._lineage_edges) + 1,
            "上游表名": upstream,
            "下游表名": downstream,
            "来源字段": field_map,
            "目标字段": field_map,
            "转换逻辑": transform_logic,
            "采集批次": batch_id,
            "链路版本": f"v{len(self._lineage_edges) + 1}",
            "status": "已生效",
            "pending": False,
            "abnormal": False,
        }
        self._lineage_edges.append(edge)
        # 同时写入 lineage_track 模块表供查询
        self.rows("lineage_track").append(edge)
        return edge

    def get_lineage_edges(self) -> list[dict[str, Any]]:
        return list(self._lineage_edges)

    # ---- 质量稽核结果 ----

    def save_quality_result(self, rule_id: str, result: dict[str, Any]) -> None:
        self._quality_results.setdefault(rule_id, []).append(result)

    def get_quality_results(self, rule_id: str) -> list[dict[str, Any]]:
        return list(self._quality_results.get(rule_id, []))

    # ---- 快照 ----

    def save_snapshot_data(self, snapshot_id: str, table_name: str, rows: list[dict[str, Any]]) -> None:
        self._snapshot_data[snapshot_id] = {
            "table_name": table_name,
            "rows": [dict(r) for r in rows],
            "frozen_at": datetime.now().isoformat(),
            "row_count": len(rows),
        }

    def get_snapshot_data(self, snapshot_id: str) -> dict[str, Any] | None:
        return self._snapshot_data.get(snapshot_id)

    # ---- 归档 ----

    def add_archive_record(self, record: dict[str, Any]) -> None:
        self._archive_records.append(record)

    def get_archive_records(self) -> list[dict[str, Any]]:
        return list(self._archive_records)

    # ---- 补数分区（任务级，向后兼容） ----

    def save_backfill_partition(self, job_id: str, partition: dict[str, Any]) -> None:
        self._backfill_partitions.setdefault(job_id, []).append(partition)

    def get_backfill_partitions(self, job_id: str) -> list[dict[str, Any]]:
        return list(self._backfill_partitions.get(job_id, []))

    # ---- 补数分区结果集（按表，唯一完整版本，并发安全） ----

    REQUIRED_RESULT_KEYS = ("目标表名", "分区字段", "完成水位", "完成分区", "失败分区", "任务结果")

    def backfill_lock(self, table_name: str) -> threading.RLock:
        """同一张表的结果集写入共用一把锁，批量并发写入被串行化。"""
        with self._backfill_locks_guard:
            lock = self._backfill_locks.get(table_name)
            if lock is None:
                lock = threading.RLock()
                self._backfill_locks[table_name] = lock
            return lock

    def get_backfill_result(self, table_name: str) -> dict[str, Any] | None:
        result = self._backfill_results.get(table_name)
        return deepcopy(result) if result is not None else None

    def list_backfill_results(self) -> list[dict[str, Any]]:
        return [deepcopy(result) for result in self._backfill_results.values()]

    def commit_backfill_result(self, table_name: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        """原子提交结果集完整版本。

        - 只接受包含完成分区、失败分区、完成水位、任务结果的完整快照，
          半成品快照直接拒绝，保证库里永远只有一个完整版本；
        - 版本号单调递增并整体替换旧快照（旧版本不残留为并行版本）；
        - 调用方须持有所在表的 backfill_lock，本方法在锁内完成换版。
        """
        missing = [key for key in self.REQUIRED_RESULT_KEYS if key not in snapshot]
        if missing:
            raise ValueError(f"补数分区结果集缺少完整字段：{'、'.join(missing)}")
        previous = self._backfill_results.get(table_name)
        stored = deepcopy(snapshot)
        stored["目标表名"] = table_name
        stored["版本号"] = int(previous["版本号"]) + 1 if previous else 1
        stored["更新时间"] = datetime.now().isoformat()
        self._backfill_results[table_name] = stored
        return deepcopy(stored)

    def clear_backfill_results(self) -> None:
        self._backfill_results.clear()


store = Store()
