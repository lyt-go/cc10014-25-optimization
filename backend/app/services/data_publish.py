"""发布版本业务规则：从transform_dag获取最新输出，创建可消费的发布版本。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "data_publish"
LIST_FIELDS = ["版本编号", "数据集名称", "发布范围", "查询参数", "响应字段", "缓存秒数", "分页上限", "消费应用"]
REQUIRED_FIELDS = ["版本编号", "数据集名称"]
STATUS_ORDER = ["草稿", "待校验", "已发布", "已下线", "已撤回"]
ACTION_RULES = {"提交校验": "待校验", "确认发布": "已发布", "标记下线": "已下线", "撤回版本": "已撤回"}
NEGATIVE_ACTIONS = ["撤回版本"]


class DataPublishService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("版本编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        if values.get("来源转换节点"):
            entry["来源转换节点"] = values["来源转换节点"]
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"发布版本 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于数据发布可执行范围"
        # 质量稽核→数据发布：确认发布前必须通过质量校验
        if action == "确认发布":
            dataset = entry.get("数据集名称", "")
            qc_result = self._check_quality(dataset)
            if not qc_result.get("passed", False):
                return None, f"数据发布被阻断：{qc_result.get('message', '质量稽核未通过')}"
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        store.save_version(MODULE, entry_id, entry)
        if action == "确认发布":
            self._attach_latest_output(entry)
        return entry, f"发布版本已{action}"

    def _check_quality(self, dataset: str) -> dict[str, Any]:
        """检查数据集是否通过所有质量稽核规则。"""
        from app.services.quality_check import QualityCheckService
        qc = QualityCheckService()
        # 查找该数据集的所有生效稽核规则
        rules = []
        for row in store.rows("quality_check"):
            if row.get("校验表名") == dataset and row.get("status") == "已生效":
                rules.append(row)
        if not rules:
            # 没有规则默认通过，但记录日志
            return {"passed": True, "message": "无生效稽核规则，默认通过"}
        for rule in rules:
            result = qc.execute_check(rule["id"])
            res = result.get("result", {})
            if res.get("failed", 0) > 0:
                return {"passed": False, "message": f"规则 {rule.get('规则编号')} 稽核失败 {res.get('failed')} 条"}
        return {"passed": True, "message": f"全部 {len(rules)} 条稽核规则通过"}

    def _attach_latest_output(self, entry: dict[str, Any]) -> None:
        node_code = entry.get("来源转换节点", "")
        if not node_code:
            return
        # 查找转换节点
        for row in store.rows("transform_dag"):
            if row.get("节点编号") == node_code:
                output_table = row.get("输出表名", "")
                data = store.table_rows(output_table)
                entry["_published_data"] = {
                    "table": output_table,
                    "row_count": len(data),
                    "published_at": datetime.now().isoformat(),
                }
                break

    def get_published_data(self, entry_id: int) -> dict[str, Any] | None:
        entry = self.get_entry(entry_id)
        if not entry:
            return None
        node_code = entry.get("来源转换节点", "")
        if not node_code:
            return None
        for row in store.rows("transform_dag"):
            if row.get("节点编号") == node_code:
                return {
                    "table": row.get("输出表名", ""),
                    "rows": store.table_rows(row.get("输出表名", "")),
                }
        return None
