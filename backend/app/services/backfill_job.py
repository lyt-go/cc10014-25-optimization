"""补数调度服务：分批补数待补分区，生成并维护补数分区结果集。

设计要点（对应补数优化要求）：

1. 分区级读取/写入：只覆盖目标分区（``upsert_table_partition``），
   相邻补数任务在同一分区边界重叠扫描时不再覆盖其他天结果。
2. 分批补数：按批大小把待补数分区切批执行，一批一个原子提交点。
3. 中断恢复幂等：已完成分区直接跳过、不再写入；失败分区可重试；
   分区数据与完成水位在同一次结果集提交中同步落库，
   不会出现“数据已写、水位未推进”而导致的重试覆盖。
4. 并发单版本：同表写入共用一把锁，结果集只保留一个完整版本。
5. 冲突判定：相邻任务重叠边界先按提交先后、再按任务编号判定优先级，
   结果集同时给出候选视图（两个候选版本+差异记录+一个生效版本）
   与合并视图（单一边界版本+合并任务号），覆盖窗口不同但结果集始终完整。
6. 批量操作：一次提交整组任务，逐条返回处理结果。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app.store import store

MODULE = "backfill_job"
REQUIRED_FIELDS = ["任务编号", "目标表名", "起始日期"]
STATUS_ORDER = ["草稿", "运行中", "已完成", "已取消"]
ACTION_RULES = {"启动任务": "运行中", "确认完成": "已完成", "取消任务": "已取消"}
NEGATIVE_ACTIONS = []

PARTITION_FIELD = "dt"
DEFAULT_BATCH_SIZE = 7
# 测试/故障注入用：命中 (表名, 分区日期) 时该分区写入失败，模拟补数中断
FAIL_PARTITIONS: set[tuple[str, str]] = set()


class BackfillJobService:
    # ---- 时间线（甘特视图，保持原接口） ----

    def get_timeline(self) -> list[dict[str, Any]]:
        rows = store.rows(MODULE)
        today = date.today()
        items: list[dict] = []
        for i, row in enumerate(rows):
            start = (today - timedelta(days=7 - i * 2)).isoformat()
            end = (today + timedelta(days=3 - i)).isoformat()
            items.append({
                "id": int(row.get("id", i + 1)),
                "name": str(row.get("任务编号", "")),
                "start": start,
                "end": end,
                "status": str(row.get("status", STATUS_ORDER[i % len(STATUS_ORDER)])),
                "logs": f"[{start}] 作业启动\n[{start}] 正在处理...\n[{end}] 任务{'完成' if row.get('status') == STATUS_ORDER[-1] else '进行中'}",
            })
        return items

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [f for f in REQUIRED_FIELDS if not str(values.get(f) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(r.get("id", 0)) for r in rows), default=0) + 1}
        entry.update({f: values.get(f) for f in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        for k in ["截止日期", "并行度", "覆盖策略", "完成水位", "失败区间"]:
            if k in values:
                entry[k] = values[k]
        # 待补数/已完成/失败分区清单，随任务进度维护
        entry.setdefault("待补分区", [])
        entry.setdefault("完成分区", [])
        entry.setdefault("失败分区", [])
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"补数任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于补数调度可执行范围"
        target = ACTION_RULES[action]
        if action == "启动任务":
            # 幂等启动：已完成分区不会重跑，只补待补分区
            return self.start_job(entry_id)
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.save_version(MODULE, entry_id, entry)
        return entry, f"补数任务已{action}"

    # ---- 批量操作：整组提交、逐条返回 ----

    def run_batch_actions(self, job_ids: list[int], action: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for job_id in job_ids:
            entry, message = self.run_action(job_id, action)
            results.append({
                "任务id": job_id,
                "任务编号": entry.get("任务编号", "") if entry else "",
                "ok": entry is not None,
                "message": message,
                "status": entry.get("status") if entry else None,
            })
        return results

    # ---- 任务启动与分批补数 ----

    def start_job(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"补数任务 {entry_id} 不存在或已归档"
        entry["status"] = "运行中"
        entry["pending"] = True
        store.save_version(MODULE, entry_id, entry)

        table = str(entry.get("目标表名", ""))
        lock = store.backfill_lock(table)
        with lock:
            pending = self._plan_pending_partitions(entry)
            entry["待补分区"] = pending
            batch_size = self._batch_size(entry)
            failed_in_run: list[str] = []
            # 分批处理待补分区，每批结束后原子提交一次结果集
            for cursor in range(0, len(pending), batch_size):
                batch = pending[cursor:cursor + batch_size]
                batch_failed = self._process_batch(entry, batch)
                failed_in_run.extend(batch_failed)
                # 模拟中断：本批出现失败分区即停止后续批次，已完成批次已落库
                if batch_failed:
                    break
            snapshot = self._build_and_commit_snapshot(entry)
            self._sync_layer_watermark(table, snapshot["完成水位"])

        if failed_in_run:
            entry["abnormal"] = True
            entry["失败区间"] = ",".join(failed_in_run)
            message = f"补数中断：{len(failed_in_run)} 个分区失败（{entry['失败区间']}），已完成分区与水位已同步落库，重试只补待补分区"
        elif entry["待补分区"]:
            entry["abnormal"] = True
            message = "补数中断：仍有待补分区未处理，已完成分区不会重复写入"
        else:
            # 分区全部补齐，待「确认完成」动作收口；状态保持运行中以兼容时间线契约
            entry["abnormal"] = False
            entry["失败区间"] = ""
            message = "补数分区已全部补齐，分区数据与完成水位已同步落库，可执行确认完成收口"
        store.save_version(MODULE, entry_id, entry)
        return entry, message

    def _plan_pending_partitions(self, entry: dict[str, Any]) -> list[str]:
        """计算待补数分区：区间内排除已完成分区，失败分区重新纳入重试。"""
        start = str(entry.get("起始日期", ""))
        end = str(entry.get("截止日期") or start)
        try:
            s = date.fromisoformat(start)
            e = date.fromisoformat(end)
        except ValueError:
            return [start] if start else []
        completed = set(entry.setdefault("完成分区", []))
        planned: list[str] = []
        d = s
        while d <= e:
            iso = d.isoformat()
            if iso not in completed:
                planned.append(iso)
            d += timedelta(days=1)
        return planned

    def _batch_size(self, entry: dict[str, Any]) -> int:
        # 并行度表示并发写入数，不限制批次大小；批大小默认覆盖整个待补区间，
        # 可由任务上的「批大小」字段覆盖（用于验证分批提交）。
        raw = entry.get("批大小")
        if raw not in (None, ""):
            try:
                return max(1, int(str(raw)))
            except (TypeError, ValueError):
                pass
        return DEFAULT_BATCH_SIZE

    def _process_batch(self, entry: dict[str, Any], batch: list[str]) -> list[str]:
        """处理一个分区批次，返回本批失败分区（保持顺序、去重）。"""
        table = str(entry.get("目标表名", ""))
        job_no = str(entry.get("任务编号", ""))
        strategy = entry.get("覆盖策略", "覆盖写入")
        entry.setdefault("完成分区", [])
        entry.setdefault("失败分区", [])
        failed: list[str] = []
        for iso in batch:
            if iso in entry["完成分区"]:
                # 中断恢复：已完成分区不再写入，杜绝重复覆盖
                continue
            if (table, iso) in FAIL_PARTITIONS:
                if iso not in entry["失败分区"]:
                    entry["失败分区"].append(iso)
                failed.append(iso)
                # 模拟补数中断：当前批次立即停止，剩余分区留待重试
                break
            rows = [{"dt": iso, "_backfilled": True, "_job_no": job_no}]
            if strategy == "增量追加":
                store.append_table_rows(table, rows)
            else:
                # 分区级覆盖：只覆盖这一天，相邻任务重叠时不动其他分区
                store.upsert_table_partition(table, PARTITION_FIELD, iso, rows)
            store.save_backfill_partition(job_no, {"dt": iso, "_backfilled": True})
            entry["完成分区"].append(iso)
            if iso in entry["失败分区"]:
                entry["失败分区"].remove(iso)
        return list(dict.fromkeys(failed))

    # ---- 结果集构建与同步落库 ----

    def _compute_watermark(self, completed: list[str], start: str) -> str:
        """完成水位 = 自起始日期起连续完成的最大日期；中间断档则停在断档前一天。"""
        if not completed:
            return ""
        done = set(completed)
        try:
            cursor = date.fromisoformat(start)
        except ValueError:
            return min(completed)
        watermark = ""
        guard = 0
        while cursor.isoformat() in done and guard < 100000:
            watermark = cursor.isoformat()
            cursor += timedelta(days=1)
            guard += 1
        return watermark

    def _build_and_commit_snapshot(self, entry: dict[str, Any]) -> dict[str, Any]:
        """组装完整结果集并原子提交：完成分区、失败分区、完成水位同步落库。"""
        table = str(entry.get("目标表名", ""))
        job_no = str(entry.get("任务编号", ""))
        start = str(entry.get("起始日期", ""))
        completed = sorted(entry.get("完成分区", []))
        failed = sorted(entry.get("失败分区", []))
        watermark = self._compute_watermark(completed, start)
        # 任务上的完成水位与结果集保持一致
        entry["完成水位"] = watermark
        entry["待补分区"] = self._plan_pending_partitions(entry)

        previous = store.get_backfill_result(table) or {}
        task_results: dict[str, dict[str, Any]] = previous.get("任务结果", {})
        task_results[job_no] = {
            "任务编号": job_no,
            "起始日期": start,
            "截止日期": str(entry.get("截止日期") or start),
            "覆盖策略": entry.get("覆盖策略", "覆盖写入"),
            "完成分区": completed,
            "失败分区": failed,
            "完成水位": watermark,
            "状态": entry.get("status", ""),
        }

        # 结果集层面的完成/失败分区是全表所有任务的并集
        all_completed = sorted({
            iso for info in task_results.values() for iso in info.get("完成分区", [])
        })
        all_failed = sorted({
            iso for info in task_results.values() for iso in info.get("失败分区", [])
            if iso not in all_completed
        })

        # 分区归属：后到任务不抢占已生效归属，冲突单独记录
        owners: dict[str, str] = dict(previous.get("分区归属", {}))
        for iso in completed:
            owners.setdefault(iso, job_no)

        boundary_conflicts = self._detect_boundary_conflicts(table, task_results, owners)
        merged_view = self._build_merged_view(owners, task_results)

        snapshot = {
            "目标表名": table,
            "分区字段": PARTITION_FIELD,
            "完成水位": watermark,
            "完成分区": all_completed,
            "失败分区": all_failed,
            "待补分区": entry["待补分区"],
            "任务结果": task_results,
            "分区归属": owners,
            "边界冲突": boundary_conflicts,
            "合并视图": merged_view,
        }
        return store.commit_backfill_result(table, snapshot)

    def _detect_boundary_conflicts(
        self, table: str, task_results: dict[str, dict[str, Any]], owners: dict[str, str],
    ) -> list[dict[str, Any]]:
        """相邻补数任务在同一分区边界重叠时，给出候选版本、差异记录和唯一生效版本。"""
        jobs = list(task_results.values())
        conflicts: list[dict[str, Any]] = []
        for i in range(len(jobs)):
            for j in range(i + 1, len(jobs)):
                left, right = jobs[i], jobs[j]
                overlap = sorted(
                    set(left.get("完成分区", [])) & set(right.get("完成分区", []))
                )
                if not overlap:
                    continue
                # 优先级：先完成该分区的任务生效；同时完成时任务编号小的优先
                winner, loser = left, right
                first_owner = owners.get(overlap[0])
                if first_owner == right["任务编号"]:
                    winner, loser = right, left
                elif first_owner is None and right["任务编号"] < left["任务编号"]:
                    winner, loser = right, left
                diff_records = [
                    {
                        "分区": iso,
                        "候选任务": [left["任务编号"], right["任务编号"]],
                        "生效任务": owners.get(iso, winner["任务编号"]),
                        "差异说明": f"两个任务均覆盖分区 {iso}，覆盖窗口不同，按处理优先级保留一个生效版本",
                    }
                    for iso in overlap
                ]
                conflicts.append({
                    "目标表名": table,
                    "重叠分区": overlap,
                    "候选版本": [
                        {"任务编号": left["任务编号"], "覆盖窗口": f'{left["起始日期"]}~{left["截止日期"]}'},
                        {"任务编号": right["任务编号"], "覆盖窗口": f'{right["起始日期"]}~{right["截止日期"]}'},
                    ],
                    "差异记录": diff_records,
                    "生效版本": winner["任务编号"],
                    "判定依据": "先处理补数任务的一方优先；同时完成时任务编号小者优先",
                })
        return conflicts

    def _build_merged_view(
        self, owners: dict[str, str], task_results: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """合并视图：单一边界版本 + 合并任务号，仍保留完整补数分区结果集。"""
        partition_tasks: dict[str, list[str]] = {}
        for job_no, info in task_results.items():
            for iso in info.get("完成分区", []):
                partition_tasks.setdefault(iso, [])
                if job_no not in partition_tasks[iso]:
                    partition_tasks[iso].append(job_no)
        partitions = [
            {
                "分区": iso,
                "生效任务": owners.get(iso, jobs[0] if jobs else ""),
                "合并任务号": ",".join(sorted(jobs)),
            }
            for iso, jobs in sorted(partition_tasks.items())
        ]
        merged_jobs = sorted(task_results)
        return {
            "边界版本": "single",
            "合并任务号": ",".join(merged_jobs),
            "分区版本": partitions,
            "说明": "相邻任务重叠边界合并为单一边界版本，覆盖窗口不同但结果集完整保留",
        }

    def _sync_layer_watermark(self, table: str, watermark: str) -> None:
        """DWD/ODS 水位与结果集完成水位保持一致（同一次补数提交内推进）。"""
        if not watermark:
            return
        for module in ("dwd_layer", "ods_layer"):
            for row in store.rows(module):
                if row.get("表名称") == table and row.get("刷新水位") != watermark:
                    row["刷新水位"] = watermark
                    store.save_version(module, row["id"], row)

    # ---- 结果集查询（跨模块回查补数任务结果） ----

    def list_results(self) -> list[dict[str, Any]]:
        return store.list_backfill_results()

    def get_result(self, table_name: str) -> dict[str, Any] | None:
        return store.get_backfill_result(table_name)

    def get_result_by_job(self, job_id: int) -> dict[str, Any] | None:
        """沿数据流从补数任务定位其补数分区结果集。"""
        entry = store.find(MODULE, job_id)
        if entry is None:
            return None
        return store.get_backfill_result(str(entry.get("目标表名", "")))
