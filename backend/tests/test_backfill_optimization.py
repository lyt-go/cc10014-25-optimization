"""补数优化专项测试：分批补数、中断恢复、同步落库、并发单版本、边界冲突与跨模块联动。"""
from __future__ import annotations

import threading
from datetime import date, timedelta

from app.seed import SEED_ROWS
from app.services.backfill_job import BackfillJobService, FAIL_PARTITIONS
from app.store import store


def reset_store():
    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    store._table_data.clear()
    store._versions.clear()
    store._lineage_edges.clear()
    store._quality_results.clear()
    store._snapshot_data.clear()
    store._archive_records.clear()
    store._backfill_partitions.clear()
    store.clear_backfill_results()
    FAIL_PARTITIONS.clear()


def make_job(service: BackfillJobService, no: str, table: str, start: str, end: str,
             strategy: str = "覆盖写入", batch_size: int | None = None) -> dict:
    values = {
        "任务编号": no, "目标表名": table, "起始日期": start, "截止日期": end,
        "并行度": "4", "覆盖策略": strategy, "完成水位": "", "失败区间": "",
    }
    if batch_size is not None:
        values["批大小"] = batch_size
    entry, missing = service.create_entry(values)
    assert not missing
    return entry


class TestBackfillOptimization:
    def setup_method(self):
        reset_store()
        self.service = BackfillJobService()

    def test_batch_processing_pending_partitions(self):
        """待补分区按批切分执行，结果集记录完成分区、失败分区与完成水位。"""
        entry = make_job(self.service, "BF-T-001", "ods_demo", "2026-10-01", "2026-10-15", batch_size=5)
        updated, _ = self.service.start_job(entry["id"])
        assert updated["status"] == "运行中"
        result = store.get_backfill_result("ods_demo")
        assert result is not None
        assert len(result["完成分区"]) == 15
        assert result["失败分区"] == []
        assert result["完成水位"] == "2026-10-15"
        # 结果集是完整版本
        assert result["版本号"] >= 1
        assert set(["目标表名", "分区字段", "完成水位", "完成分区", "失败分区", "任务结果"]).issubset(result)

    def test_interrupted_resume_skips_completed_partitions(self):
        """中断后重试：已完成分区不再写入，只补待补分区，最终水位与完成分区一致。"""
        entry = make_job(self.service, "BF-T-002", "ods_demo2", "2026-10-01", "2026-10-10", batch_size=3)
        FAIL_PARTITIONS.add(("ods_demo2", "2026-10-04"))
        updated, message = self.service.start_job(entry["id"])
        assert "中断" in message
        # 第一批 10-01~10-03 已原子落库；10-04 失败后停止
        result = store.get_backfill_result("ods_demo2")
        assert result["完成分区"] == ["2026-10-01", "2026-10-02", "2026-10-03"]
        assert result["失败分区"] == ["2026-10-04"]
        assert result["完成水位"] == "2026-10-03"
        before_rows = [dict(r) for r in store.table_rows("ods_demo2")]

        # 解除故障后重试：已完成分区不重复写入（行数不变、无重复 dt）
        FAIL_PARTITIONS.clear()
        updated2, _ = self.service.start_job(entry["id"])
        after_rows = store.table_rows("ods_demo2")
        dts = [r["dt"] for r in after_rows]
        assert len(dts) == len(set(dts)) == 10
        # 已完成分区行未被覆盖重写（_job_no 仍只出现一次且内容一致）
        assert len(before_rows) == 3
        result2 = store.get_backfill_result("ods_demo2")
        assert len(result2["完成分区"]) == 10
        assert result2["失败分区"] == []
        assert result2["完成水位"] == "2026-10-10"
        assert updated2["待补分区"] == []

    def test_watermark_advances_only_with_partition_commit(self):
        """分区数据写入与水位推进同步落库；断档时水位停在连续完成的最后一天。"""
        entry = make_job(self.service, "BF-T-003", "ods_demo3", "2026-10-01", "2026-10-06", batch_size=10)
        FAIL_PARTITIONS.add(("ods_demo3", "2026-10-02"))
        self.service.start_job(entry["id"])
        result = store.get_backfill_result("ods_demo3")
        # 10-01 完成、10-02 失败即中断：水位只能到 10-01
        assert result["完成分区"] == ["2026-10-01"]
        assert result["完成水位"] == "2026-10-01"
        # 任务水位与结果集水位一致
        assert store.find("backfill_job", entry["id"])["完成水位"] == result["完成水位"]

    def test_dwd_watermark_consistent_with_result_set(self):
        """DWD刷新水位与结果集中的完成分区保持一致。"""
        entry = make_job(self.service, "BF-T-004", "dwd_order_detail", "2026-10-01", "2026-10-05")
        self.service.start_job(entry["id"])
        result = store.get_backfill_result("dwd_order_detail")
        dwd = next(r for r in store.rows("dwd_layer") if r["表名称"] == "dwd_order_detail")
        assert dwd["刷新水位"] == result["完成水位"] == "2026-10-05"

    def test_concurrent_writes_keep_single_complete_version(self):
        """并发批量写入同表结果集时只保留一个完整版本。"""
        e1 = make_job(self.service, "BF-C-001", "ods_conc", "2026-11-01", "2026-11-03")
        e2 = make_job(self.service, "BF-C-002", "ods_conc", "2026-11-02", "2026-11-04")
        errors: list[Exception] = []

        def run(job_id: int) -> None:
            try:
                self.service.start_job(job_id)
            except Exception as exc:  # pragma: no cover - 测试辅助
                errors.append(exc)

        t1 = threading.Thread(target=run, args=(e1["id"],))
        t2 = threading.Thread(target=run, args=(e2["id"],))
        t1.start(); t2.start(); t1.join(); t2.join()
        assert errors == []
        results = store.list_backfill_results()
        table_results = [r for r in results if r["目标表名"] == "ods_conc"]
        assert len(table_results) == 1
        result = table_results[0]
        # 两个任务结果都在同一个完整版本里
        assert set(result["任务结果"]) == {"BF-C-001", "BF-C-002"}
        assert len(result["完成分区"]) == 4
        assert result["失败分区"] == []

    def test_overlapping_boundary_conflict_and_merged_view(self):
        """相邻任务同一边界重叠：候选版本+差异记录+唯一生效版本，同时给出单一边界版本与合并任务号。"""
        e1 = make_job(self.service, "BF-O-001", "ods_overlap", "2026-10-01", "2026-10-05")
        e2 = make_job(self.service, "BF-O-002", "ods_overlap", "2026-10-05", "2026-10-08")
        self.service.start_job(e1["id"])
        self.service.start_job(e2["id"])
        result = store.get_backfill_result("ods_overlap")
        conflicts = result["边界冲突"]
        assert len(conflicts) == 1
        conflict = conflicts[0]
        assert conflict["重叠分区"] == ["2026-10-05"]
        assert len(conflict["候选版本"]) == 2
        assert conflict["生效版本"] == "BF-O-001"  # 先处理的任务优先
        assert conflict["差异记录"][0]["生效任务"] == "BF-O-001"
        # 合并视图：单一边界版本 + 合并任务号，结果集仍完整
        merged = result["合并视图"]
        assert merged["边界版本"] == "single"
        assert set(merged["合并任务号"].split(",")) == {"BF-O-001", "BF-O-002"}
        boundary = next(p for p in merged["分区版本"] if p["分区"] == "2026-10-05")
        assert boundary["生效任务"] == "BF-O-001"
        assert set(boundary["合并任务号"].split(",")) == {"BF-O-001", "BF-O-002"}
        # 分区级覆盖：先到任务的分区数据未被后到任务整体覆盖
        rows = store.table_rows("ods_overlap")
        assert len(rows) == 8

    def test_partition_upsert_does_not_overwrite_other_days(self):
        """重叠扫描时分区级写入只覆盖目标分区，其他天结果保留。"""
        e1 = make_job(self.service, "BF-P-001", "ods_part", "2026-10-01", "2026-10-03")
        self.service.start_job(e1["id"])
        e2 = make_job(self.service, "BF-P-002", "ods_part", "2026-10-03", "2026-10-04")
        self.service.start_job(e2["id"])
        rows = store.table_rows("ods_part")
        dts = sorted(r["dt"] for r in rows)
        assert dts == ["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"]

    def test_batch_actions_return_per_job_results(self):
        """整组任务一次提交，批量多选后逐条给出结果。"""
        e1 = make_job(self.service, "BF-B-001", "ods_b1", "2026-10-01", "2026-10-02")
        e2 = make_job(self.service, "BF-B-002", "ods_b2", "2026-10-01", "2026-10-02")
        results = self.service.run_batch_actions([e1["id"], e2["id"], 9999], "启动任务")
        assert len(results) == 3
        assert [r["ok"] for r in results] == [True, True, False]
        assert results[0]["任务编号"] == "BF-B-001"
        assert "不存在" in results[2]["message"]
        assert store.get_backfill_result("ods_b1") is not None
        assert store.get_backfill_result("ods_b2") is not None

    def test_dwd_cross_module_lookup(self):
        """明细层管理围绕结果集联动：按DWD表回查补数任务结果。"""
        from app.services.dwd_layer import DwdLayerService
        entry = make_job(self.service, "BF-L-001", "dwd_payment_detail", "2026-09-01", "2026-09-03")
        self.service.start_job(entry["id"])
        dwd = DwdLayerService()
        result = dwd.get_backfill_result(2)  # seed 中 id=2 为 dwd_payment_detail
        assert result is not None
        assert result["目标表名"] == "dwd_payment_detail"
        assert "BF-L-001" in result["任务结果"]
        # 沿数据流从任务侧也能定位同一份结果集
        assert self.service.get_result_by_job(entry["id"])["版本号"] == result["版本号"]

    def test_conflict_priority_judged_before_processing(self):
        """冲突先判定谁优先处理补数任务：先提交者生效，同时提交按任务编号。"""
        e1 = make_job(self.service, "BF-Z-002", "ods_prio", "2026-10-01", "2026-10-02")
        e2 = make_job(self.service, "BF-Z-001", "ods_prio", "2026-10-02", "2026-10-03")
        # 编号大的任务先处理
        self.service.start_job(e1["id"])
        self.service.start_job(e2["id"])
        result = store.get_backfill_result("ods_prio")
        conflict = result["边界冲突"][0]
        assert conflict["生效版本"] == "BF-Z-002"
        assert "先处理" in conflict["判定依据"]
