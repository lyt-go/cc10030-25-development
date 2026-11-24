"""格时窗口样本资格服务。

把坐标标准化结果按「格 + 时间窗」登记观测状态、样本资格和可逐点复算的明细，
并承载巡检临时领取、独立审核纠偏、正式统计版本与部分交付。

三条硬约束：

1. 空窗口不是异常。无观测、无有效坐标、仍待校验要分开表达；空窗必须带原因，
   构建时遇到空观测只登记原因，不抛错，也不返回"什么都没有"。
2. 领取即快照。巡检领取号落盘后，采用的点、坐标诊断和估算值都从快照读取并复算，
   不再回读原始点文件；原始文件之后被改动，旧领取号仍然能复算原值。
3. 临时估算不能冒充正式值。只有经过审核确认（含零观测窗确认）的窗口才进入
   正式统计；正式结果另起版本，逐点明细随版本冻结，旧版本不被改写。
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import datetime, timezone
from typing import Any, Iterable

from app.services.base import BaseModuleService

# 观测状态
OBS_NO_OBSERVATION = "无观测"
OBS_NO_VALID_COORDINATE = "无有效坐标"
OBS_PENDING = "待校验"
OBS_CONFIRMED = "已确认"

# 样本资格：热力侧据此区分临时估算与正式统计
ELIG_NONE = "无样本"
ELIG_INVALID = "不可用"
ELIG_PENDING = "待校验"
ELIG_PROVISIONAL = "临时样本"
ELIG_FORMAL = "正式样本"

EST_NONE = "无"
EST_PROVISIONAL = "临时估算"
EST_FORMAL = "正式统计"

CLAIM_INSPECTION = "巡检临时"
CLAIM_FORMAL = "正式"

CLAIM_OPENED = "待确认"
CLAIM_CONFIRMED = "已确认"
CLAIM_PARTIAL = "部分确认"
CLAIM_REPLAYED = "已复算"

DELIVERY_NONE = "none"
DELIVERY_ZERO_ONLY = "zero_only"
DELIVERY_WITH_OBSERVATIONS = "with_observations"

SCOPE_ALL = "all"
SCOPE_ZERO_ONLY = "zero_only"

UNASSIGNED_CELL = "UNASSIGNED"


class CoordWindowError(ValueError):
    """格时窗口、领取或审核流程中的领域错误。"""


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _checksum(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()[:16]


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _to_epoch(value: Any) -> float | None:
    """时间戳数字或 ISO 字符串转 epoch 秒；无法解析返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _fmt_time(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CoordWindowService(BaseModuleService):
    MODULE = "coord_transform"
    ENTITY = "格时窗口"
    OPERATION_TERM = "登记"
    LIST_FIELDS = [
        "窗口编号", "批次编号", "网格", "时间窗", "观测状态",
        "样本资格", "有效观测数", "估计类型",
    ]
    STATUS_ORDER = [OBS_NO_OBSERVATION, OBS_NO_VALID_COORDINATE, OBS_PENDING, OBS_CONFIRMED]

    # ---------------- 目录 ----------------

    def _windows_file(self, batch_key: str):
        return self._data_dir("coord_windows") / f"{batch_key}_windows.jsonl"

    def _detail_file(self, batch_key: str) -> Any:
        return self._data_dir("coord_windows") / f"{batch_key}_detail.jsonl"

    def _checkpoint_name(self, batch_key: str) -> str:
        return f"coord_windows_{batch_key}"

    def _claim_file(self, claim_id: str):
        return self._data_dir("coord_window_claims") / f"{claim_id}.json"

    def _audit_file(self, batch_key: str):
        return self._data_dir("coord_window_audits") / f"{batch_key}_audits.jsonl"

    def _formal_cells_file(self, batch_key: str, version: int):
        return self._data_dir("coord_formal") / f"{batch_key}_v{version}.jsonl"

    def _formal_detail_file(self, batch_key: str, version: int):
        return self._data_dir("coord_formal") / f"{batch_key}_v{version}_detail.jsonl"

    # ---------------- 坐标与窗口工具 ----------------

    @staticmethod
    def _geohash_center(geohash: str) -> tuple[float, float] | None:
        """把 geohash 还原成格中心经纬度，用于坐标诊断。"""
        base32 = "0123456789bcdefghjkmnpqrstuvwxyz"
        lat_range = [-90.0, 90.0]
        lon_range = [-180.0, 180.0]
        is_even = True
        for char in geohash:
            index = base32.find(char)
            if index < 0:
                return None
            for mask in (16, 8, 4, 2, 1):
                if is_even:
                    mid = (lon_range[0] + lon_range[1]) / 2
                    lon_range[0 if index & mask else 1] = mid
                else:
                    mid = (lat_range[0] + lat_range[1]) / 2
                    lat_range[0 if index & mask else 1] = mid
                is_even = not is_even
        return (lat_range[0] + lat_range[1]) / 2, (lon_range[0] + lon_range[1]) / 2

    def _resolve_coordinate(self, point: dict[str, Any]) -> dict[str, Any]:
        """选出该点采用的坐标并给出可读诊断，不抛错。"""
        source = "转换坐标"
        lat = point.get("trans_lat")
        lon = point.get("trans_lon")
        if lat is None or lon is None:
            source = "原始坐标"
            lat = point.get("lat")
            lon = point.get("lon")
        if lat is None or lon is None:
            source = "缺少坐标"
            return {
                "valid": False,
                "lat": None,
                "lon": None,
                "source": source,
                "reason": "缺少经纬度",
            }
        lat_f = _as_float(lat)
        lon_f = _as_float(lon)
        if lat_f is None or lon_f is None:
            return {
                "valid": False,
                "lat": lat,
                "lon": lon,
                "source": source,
                "reason": "坐标不是有限数值",
            }
        if not -90.0 <= lat_f <= 90.0:
            return {
                "valid": False,
                "lat": lat_f,
                "lon": lon_f,
                "source": source,
                "reason": f"纬度越界({lat_f})",
            }
        if not -180.0 <= lon_f <= 180.0:
            return {
                "valid": False,
                "lat": lat_f,
                "lon": lon_f,
                "source": source,
                "reason": f"经度越界({lon_f})",
            }
        return {
            "valid": True,
            "lat": lat_f,
            "lon": lon_f,
            "source": source,
            "reason": None,
        }

    @staticmethod
    def _point_weight(accuracy: Any) -> float:
        """单点权重：精度越好权重越高，缺精度按 1.0 计。"""
        acc = _as_float(accuracy)
        if acc is None or acc < 0:
            return 1.0
        return round(1.0 / (1.0 + acc / 10.0), 6)

    def _point_detail(self, batch_key: str, window: dict[str, Any], point: dict[str, Any],
                      audit: dict[str, Any] | None = None) -> dict[str, Any]:
        """把一条定位点整理成窗口明细；审核纠偏时用纠偏坐标覆盖采用坐标。"""
        coordinate = self._resolve_coordinate(point)
        audit_id = None
        audited = False
        if audit:
            correction = (audit.get("corrections") or {}).get(str(point.get("point_id")))
            if correction:
                coordinate = {
                    "valid": True,
                    "lat": correction["lat"],
                    "lon": correction["lon"],
                    "source": "审核纠偏",
                    "reason": None,
                }
            audited = str(point.get("point_id")) in {
                str(item) for item in (audit.get("observations_confirmed") or [])
            }
            audit_id = audit.get("audit_id")

        weight = self._point_weight(point.get("accuracy"))
        diagnostic: dict[str, Any] = {
            "valid": coordinate["valid"],
            "reason": coordinate["reason"],
            "coordinate_source": coordinate["source"],
            "inside_declared_cell": False,
            "distance_to_cell_center_m": None,
            "audited": audited,
            "audit_id": audit_id,
        }
        if coordinate["valid"] and window.get("geohash") not in (None, UNASSIGNED_CELL):
            center = self._geohash_center(window["geohash"])
            if center:
                diagnostic["distance_to_cell_center_m"] = round(
                    self._haversine(center[0], center[1], coordinate["lat"], coordinate["lon"]), 3
                )
            diagnostic["inside_declared_cell"] = (
                self._geohash_encode(coordinate["lat"], coordinate["lon"], window["grid_precision"])
                == window["geohash"]
            )
        # 明细里的贡献按"有效坐标"计；正式统计再按审核标记过滤，临时值不自动转正。
        contribution = round(weight, 6) if coordinate["valid"] else 0.0

        return {
            "window_id": window["window_id"],
            "batch_key": batch_key,
            "point_id": point.get("point_id"),
            "timestamp": point.get("timestamp"),
            "orig_lat": point.get("lat"),
            "orig_lon": point.get("lon"),
            "trans_lat": point.get("trans_lat"),
            "trans_lon": point.get("trans_lon"),
            "adopted_lat": coordinate["lat"],
            "adopted_lon": coordinate["lon"],
            "coordinate_source": coordinate["source"],
            "valid": coordinate["valid"],
            "invalid_reason": coordinate["reason"],
            "accuracy": point.get("accuracy"),
            "terminal": point.get("terminal"),
            "weight": weight,
            "contribution": contribution,
            "diagnostic": diagnostic,
        }

    @staticmethod
    def _summarize_detail(detail: list[dict[str, Any]]) -> dict[str, Any]:
        valid = [row for row in detail if row["valid"]]
        reasons: dict[str, int] = {}
        for row in detail:
            if not row["valid"]:
                reasons[row["invalid_reason"] or "坐标不可用"] = (
                    reasons.get(row["invalid_reason"] or "坐标不可用", 0) + 1
                )
        return {
            "observation_count": len(detail),
            "valid_count": len(valid),
            "invalid_reason_counts": reasons,
            "estimate": round(sum(row["contribution"] for row in detail), 6),
            "detail_checksum": _checksum(detail),
        }

    def _window_status(self, detail: list[dict[str, Any]], audited_ids: set[str],
                       is_zero: bool) -> tuple[str, str, str]:
        """返回(观测状态, 样本资格, 空窗原因)。"""
        if is_zero:
            return OBS_NO_OBSERVATION, ELIG_NONE, "该格在时间窗内没有观测"
        valid = [row for row in detail if row["valid"]]
        if not valid:
            return (
                OBS_NO_VALID_COORDINATE,
                ELIG_INVALID,
                "窗内观测存在但坐标均不可用",
            )
        confirmed = [row for row in valid if str(row["point_id"]) in audited_ids]
        if len(confirmed) == len(valid):
            return OBS_CONFIRMED, ELIG_FORMAL, ""
        return OBS_PENDING, ELIG_PENDING, ""

    @staticmethod
    def _invalid_reason_text(detail: list[dict[str, Any]]) -> str:
        reasons: dict[str, int] = {}
        for row in detail:
            if not row["valid"]:
                key = row["invalid_reason"] or "坐标不可用"
                reasons[key] = reasons.get(key, 0) + 1
        if not reasons:
            return ""
        parts = [f"{reason}{count}个" for reason, count in sorted(reasons.items())]
        return "窗内观测坐标均不可用：" + "、".join(parts)

    def _window_id(self, batch_key: str, geohash: str, bucket: int, precision: int) -> str:
        return f"{batch_key}:{geohash}:{bucket}:{precision}"

    # ---------------- 构建窗口 ----------------

    def build_windows(self, batch_key: str, grid_precision: int = 6,
                      bucket_sec: int = 3600,
                      expected_windows: Iterable[dict[str, Any]] | None = None,
                      limit: int | None = None,
                      resume: bool = True) -> dict[str, Any]:
        """按格和时间桶登记窗口；空观测只登记原因，不抛错。

        `limit` 用于模拟计算中断：本次只处理前 N 个待处理窗口，检查点保留
        已经采用的实际范围，后续 `resume=True` 重试从断点继续，不重复也不丢窗。
        """
        if bucket_sec <= 0:
            raise CoordWindowError("bucket_sec 必须为正整数")
        if grid_precision < 1:
            raise CoordWindowError("grid_precision 必须大于 0")

        params = {
            "batch_key": batch_key,
            "grid_precision": grid_precision,
            "bucket_sec": bucket_sec,
        }
        fingerprint = _checksum(params)
        checkpoint = self._read_checkpoint(self._checkpoint_name(batch_key))
        resumed = bool(
            resume and checkpoint and checkpoint.get("params_fingerprint") == fingerprint
        )
        # 中断重试时沿用上一轮登记过的期望窗口，避免重试丢掉还没处理的零窗。
        if resumed and not expected_windows and checkpoint:
            expected_windows = checkpoint.get("expected_windows") or []
        expected_windows = list(expected_windows or [])

        points_file = self._data_dir("coord_transformed") / f"{batch_key}_transformed.jsonl"
        points_origin = "coord_transformed"
        if not points_file.exists():
            points_file = self._data_dir("geo_points") / f"{batch_key}.jsonl"
            points_origin = "geo_points"
        points = self._read_jsonl(points_file) if points_file.exists() else []

        # 已有窗口（断点续算时整份保留）
        existing: dict[str, dict[str, Any]] = {}
        existing_detail: list[dict[str, Any]] = []
        if resumed:
            for row in self._read_jsonl(self._windows_file(batch_key)):
                existing[row["window_id"]] = row
            existing_detail = self._read_jsonl(self._detail_file(batch_key))

        # 分组：有效坐标按格+桶；坐标不可用的点按桶登记到"未归格"窗口
        grouped: dict[tuple[str, int], list[dict[str, Any]]] = {}
        unassigned: dict[int, list[dict[str, Any]]] = {}
        timestamps: list[float] = []
        for point in points:
            coordinate = self._resolve_coordinate(point)
            epoch = _to_epoch(point.get("timestamp"))
            bucket = int((epoch if epoch is not None else 0.0) // bucket_sec) * bucket_sec
            if epoch is not None:
                timestamps.append(epoch)
            if not coordinate["valid"]:
                unassigned.setdefault(bucket, []).append(point)
                continue
            geohash = self._geohash_encode(coordinate["lat"], coordinate["lon"], grid_precision)
            grouped.setdefault((geohash, bucket), []).append(point)

        # 期望窗口（显式登记的零观测窗）
        expected: dict[tuple[str, int], str] = {}
        for item in expected_windows:
            geohash = str(item.get("geohash") or item.get("grid") or "").strip()
            if not geohash:
                raise CoordWindowError("expected_windows 每项必须带 geohash")
            bucket_raw = item.get("time_bucket", item.get("bucket"))
            epoch = _to_epoch(bucket_raw)
            if epoch is None:
                raise CoordWindowError(f"无法解析时间桶：{bucket_raw}")
            bucket = int(epoch // bucket_sec) * bucket_sec
            expected[(geohash, bucket)] = str(item.get("reason") or "")

        targets: dict[tuple[str, int], str] = {}
        for key in list(grouped) + list(expected):
            targets[key] = expected.get(key, "")
        for bucket in unassigned:
            targets[(UNASSIGNED_CELL, bucket)] = ""

        ordered = sorted(targets, key=lambda item: (item[1], item[0]))
        processed_ids = set(existing)
        adopted_ranges: dict[str, list[float]] = {
            window_id: row.get("adopted_time_range") or []
            for window_id, row in existing.items()
            if row.get("adopted_time_range")
        }

        pending = [key for key in ordered
                   if self._window_id(batch_key, key[0], key[1], grid_precision) not in processed_ids]
        this_round = pending if limit is None else pending[: max(0, int(limit))]
        interrupted = len(this_round) < len(pending)

        for geohash, bucket in this_round:
            window_id = self._window_id(batch_key, geohash, bucket, grid_precision)
            if geohash == UNASSIGNED_CELL:
                source_points = unassigned.get(bucket, [])
                # 未归格窗口天然带观测（只是坐标不可用），不能按零观测处理
                is_zero = not source_points
            else:
                source_points = grouped.get((geohash, bucket), [])
                is_zero = not source_points
            if (geohash, bucket) in expected and not source_points:
                is_zero = True

            window_stub = {
                "window_id": window_id,
                "geohash": None if geohash == UNASSIGNED_CELL else geohash,
                "grid_precision": grid_precision,
                "estimate_kind": EST_NONE,
            }
            detail = [
                self._point_detail(batch_key, window_stub, point)
                for point in sorted(source_points, key=lambda p: str(p.get("point_id")))
            ]
            summary = self._summarize_detail(detail)
            status, eligibility, reason = self._window_status(detail, set(), is_zero)
            if not reason:
                reason = expected.get((geohash, bucket), "")
            if geohash == UNASSIGNED_CELL and not reason:
                reason = self._invalid_reason_text(detail) or "窗内观测坐标均不可用"

            epoch_values = [
                _to_epoch(point.get("timestamp")) for point in source_points
            ]
            epoch_values = [value for value in epoch_values if value is not None]
            adopted = [min(epoch_values), max(epoch_values)] if epoch_values else []
            if adopted:
                adopted_ranges[window_id] = adopted

            existing[window_id] = {
                "window_id": window_id,
                "batch_key": batch_key,
                "geohash": None if geohash == UNASSIGNED_CELL else geohash,
                "time_bucket": bucket,
                "time_bucket_str": _fmt_time(bucket),
                "grid_precision": grid_precision,
                "bucket_sec": bucket_sec,
                "observed": not is_zero,
                "observation_status": status,
                "empty_reason": reason,
                "observation_count": summary["observation_count"],
                "valid_count": summary["valid_count"],
                "invalid_reason_counts": summary["invalid_reason_counts"],
                "sample_eligibility": eligibility,
                "estimate_kind": EST_NONE,
                "provisional_estimate": None,
                "provisional_claim_id": None,
                "provisional_sample_count": 0,
                "formal_estimate": None,
                "formal_sample_count": 0,
                "formal_version": None,
                "audit_id": None,
                "detail_count": len(detail),
                "detail_checksum": summary["detail_checksum"],
                "adopted_time_range": adopted,
                "detail_ref": str(self._detail_file(batch_key)),
                "built_at": time.time(),
            }
            existing_detail.extend(detail)

        # 整份重写：重试不重复落明细。上一轮已登记的窗口原样保留，
        # 本轮只追加新窗口，因此已有的审核、领取与资格字段不会被覆盖。
        window_rows = sorted(existing.values(), key=lambda row: (row["time_bucket"], row["geohash"] or UNASSIGNED_CELL))
        self._write_jsonl_replace(self._windows_file(batch_key), window_rows)
        self._write_jsonl_replace(self._detail_file(batch_key), existing_detail)

        all_ranges = [value for value in adopted_ranges.values() if value]
        adopted_range = (
            [min(item[0] for item in all_ranges), max(item[1] for item in all_ranges)]
            if all_ranges else []
        )
        remaining = [
            self._window_id(batch_key, key[0], key[1], grid_precision)
            for key in pending[len(this_round):]
        ]
        state = {
            "batch_key": batch_key,
            "params_fingerprint": fingerprint,
            "params": params,
            "expected_windows": expected_windows,
            "processed_window_ids": [row["window_id"] for row in window_rows],
            "remaining_window_ids": remaining,
            "adopted_range": adopted_range,
            "adopted_point_count": sum(
                row.get("valid_count", 0) for row in window_rows
            ),
            "source_point_count": len(points),
            "points_origin": points_origin,
            "status": "processing" if remaining else "complete",
            "interrupted": interrupted,
            "updated_at": time.time(),
        }
        self._write_checkpoint(self._checkpoint_name(batch_key), state)

        empty_reason = ""
        if not window_rows:
            if not points:
                empty_reason = "批次没有任何定位点，且未提供期望窗口，未登记格时窗口"
            else:
                empty_reason = "定位点均无法归入格时窗口，未登记格时窗口"

        return {
            "ok": True,
            "batch_key": batch_key,
            "grid_precision": grid_precision,
            "bucket_sec": bucket_sec,
            "resumed": resumed,
            "already_processed": len(processed_ids) if resumed else 0,
            "processed_now": len(this_round),
            "window_count": len(window_rows),
            "windows": window_rows,
            "window_ids": [row["window_id"] for row in window_rows],
            "remaining_window_ids": remaining,
            "interrupted": interrupted,
            "resume_available": bool(remaining),
            "source_point_count": len(points),
            "points_origin": points_origin,
            "adopted_range": adopted_range,
            "adopted_range_str": (
                f"{_fmt_time(adopted_range[0])} ~ {_fmt_time(adopted_range[1])}"
                if adopted_range else None
            ),
            "adopted_point_count": state["adopted_point_count"],
            "empty_reason": empty_reason,
            "checkpoint": str(self._data_dir("checkpoints") / f"{self._checkpoint_name(batch_key)}.json"),
        }

    def get_checkpoint_state(self, batch_key: str) -> dict[str, Any]:
        return self._read_checkpoint(self._checkpoint_name(batch_key)) or {}

    # ---------------- 查询与复算 ----------------

    def list_windows(self, batch_key: str | None = None,
                     sample_eligibility: str | None = None,
                     observation_status: str | None = None,
                     include_detail: bool = False) -> dict[str, Any]:
        """列出窗口；没有窗口时返回原因而不是空响应。"""
        if batch_key:
            rows = self._read_jsonl(self._windows_file(batch_key))
            detail = self._read_jsonl(self._detail_file(batch_key))
        else:
            rows, detail = [], []
            base = self._data_dir("coord_windows")
            for file in sorted(base.glob("*_windows.jsonl")):
                rows.extend(self._read_jsonl(file))
            for file in sorted(base.glob("*_detail.jsonl")):
                if file.name.endswith("_detail.jsonl"):
                    detail.extend(self._read_jsonl(file))
        if sample_eligibility:
            rows = [row for row in rows if row.get("sample_eligibility") == sample_eligibility]
        if observation_status:
            rows = [row for row in rows if row.get("observation_status") == observation_status]
        detail_by_window: dict[str, list[dict[str, Any]]] = {}
        if include_detail:
            for item in detail:
                detail_by_window.setdefault(item["window_id"], []).append(item)
            for row in rows:
                row["detail"] = detail_by_window.get(row["window_id"], [])
        empty_reason = ""
        if not rows:
            empty_reason = "没有符合条件的格时窗口"
            if batch_key:
                empty_reason = f"批次 {batch_key} 尚未登记格时窗口，或筛选条件未命中"
        return {
            "ok": True,
            "total": len(rows),
            "windows": rows,
            "empty_reason": empty_reason,
            "eligibility_counts": self._count(rows, "sample_eligibility"),
            "status_counts": self._count(rows, "observation_status"),
        }

    @staticmethod
    def _count(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in rows:
            key = str(row.get(field) or "")
            counts[key] = counts.get(key, 0) + 1
        return counts

    def get_window(self, window_id: str) -> dict[str, Any] | None:
        # 批次号本身可能含冒号，按整表精确匹配，不靠 split 猜批次。
        base = self._data_dir("coord_windows")
        for file in base.glob("*_windows.jsonl"):
            for row in self._read_jsonl(file):
                if row.get("window_id") == window_id:
                    return row
        return None

    def get_window_detail(self, window_id: str) -> list[dict[str, Any]]:
        window = self.get_window(window_id)
        if window is None:
            return []
        batch_key = window["batch_key"]
        return [
            row for row in self._read_jsonl(self._detail_file(batch_key))
            if row["window_id"] == window_id
        ]

    def recompute_window(self, window_id: str) -> dict[str, Any]:
        """从明细逐点复算窗口，并与登记值核对。"""
        window = self.get_window(window_id)
        if window is None:
            raise CoordWindowError(f"格时窗口 {window_id} 不存在")
        detail = self.get_window_detail(window_id)
        recomputed = round(sum(row.get("contribution", 0.0) for row in detail), 6)
        checksum = _checksum(detail)
        return {
            "ok": True,
            "window_id": window_id,
            "batch_key": window["batch_key"],
            "observation_count": len(detail),
            "valid_count": sum(1 for row in detail if row.get("valid")),
            "recomputed_estimate": recomputed,
            "registered_provisional_estimate": window.get("provisional_estimate"),
            "registered_formal_estimate": window.get("formal_estimate"),
            "estimate_kind": window.get("estimate_kind"),
            "sample_eligibility": window.get("sample_eligibility"),
            "detail_checksum": checksum,
            "checksum_matches": checksum == window.get("detail_checksum"),
            "points": detail,
        }

    # ---------------- 领取 ----------------

    @staticmethod
    def _snapshot_window(window: dict[str, Any], detail: list[dict[str, Any]],
                         role: str) -> dict[str, Any]:
        """把窗口连同逐点明细冻结成领取快照，之后不再回读源文件。"""
        snapshot_detail = [dict(row) for row in detail]
        return {
            "window_id": window["window_id"],
            "geohash": window.get("geohash"),
            "time_bucket": window["time_bucket"],
            "time_bucket_str": window.get("time_bucket_str"),
            "grid_precision": window.get("grid_precision"),
            "observation_status": window.get("observation_status"),
            "empty_reason": window.get("empty_reason"),
            "sample_eligibility": window.get("sample_eligibility"),
            "role": role,
            "estimate_kind": (
                EST_PROVISIONAL if window.get("valid_count") else EST_NONE
            ),
            "estimate": round(sum(row.get("contribution", 0.0) for row in snapshot_detail), 6),
            "valid_count": sum(1 for row in snapshot_detail if row.get("valid")),
            "observation_count": len(snapshot_detail),
            "detail_checksum": _checksum(snapshot_detail),
            "detail": snapshot_detail,
        }

    def _new_claim_id(self, claim_kind: str) -> str:
        directory = self._data_dir("coord_window_claims")
        stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
        # 同一秒内多次领取要拿到不同编号，避免覆盖前一张凭证。
        seq = 0
        while True:
            seq += 1
            candidate = f"{'INSP' if claim_kind == CLAIM_INSPECTION else 'FORM'}-{stamp}-{seq:03d}"
            if not (directory / f"{candidate}.json").exists():
                return candidate

    def _write_claim(self, claim: dict[str, Any]) -> dict[str, Any]:
        claim["payload_checksum"] = _checksum({
            "claim_id": claim["claim_id"],
            "batch_key": claim["batch_key"],
            "claim_kind": claim["claim_kind"],
            "scope": claim.get("scope"),
            "windows": claim.get("windows"),
            "window_ids": claim.get("window_ids"),
            "audit_required": claim.get("audit_required"),
        })
        self._write_json_file(self._claim_file(claim["claim_id"]), claim)
        return claim

    def claim_inspection(self, batch_key: str, window_ids: list[str] | None = None) -> dict[str, Any]:
        """巡检领取：冻结窗口的采用点与坐标诊断，给出可独立复算的临时值。"""
        windows = self._read_jsonl(self._windows_file(batch_key))
        if not windows:
            raise CoordWindowError(
                f"批次 {batch_key} 尚未登记格时窗口，无法发起巡检领取"
            )
        wanted = set(window_ids or [])
        detail_rows = self._read_jsonl(self._detail_file(batch_key))
        detail_by_window: dict[str, list[dict[str, Any]]] = {}
        for row in detail_rows:
            detail_by_window.setdefault(row["window_id"], []).append(row)

        selected = [
            window for window in windows
            if not wanted or window["window_id"] in wanted
        ]
        if wanted:
            missing = sorted(wanted - {window["window_id"] for window in selected})
            if missing:
                raise CoordWindowError(f"以下窗口不存在：{'、'.join(missing)}")

        claim_id = self._new_claim_id(CLAIM_INSPECTION)
        snapshots = [
            self._snapshot_window(window, detail_by_window.get(window["window_id"], []),
                                  role="inspection")
            for window in selected
        ]
        claim = {
            "claim_id": claim_id,
            "batch_key": batch_key,
            "claim_kind": CLAIM_INSPECTION,
            "status": CLAIM_CONFIRMED,
            "scope": SCOPE_ALL,
            "audit_required": False,
            "created_at": time.time(),
            "window_ids": [item["window_id"] for item in snapshots],
            "windows": snapshots,
            "provisional_total": round(sum(item["estimate"] for item in snapshots), 6),
            # 领取时的复算基准，之后审核/确认都不会改写这份原始巡检值。
            "recomputed_total_at_issue": round(sum(item["estimate"] for item in snapshots), 6),
            "zero_window_ids": [item["window_id"] for item in snapshots if not item["valid_count"]],
            "observation_window_ids": [item["window_id"] for item in snapshots if item["valid_count"]],
        }
        self._write_claim(claim)

        # 窗口记录登记临时估算与来源领取号；资格保持待校验，不冒充正式。
        for window in windows:
            if window["window_id"] not in {item["window_id"] for item in snapshots}:
                continue
            snapshot = next(item for item in snapshots if item["window_id"] == window["window_id"])
            window["estimate_kind"] = EST_PROVISIONAL
            window["provisional_estimate"] = snapshot["estimate"]
            window["provisional_claim_id"] = claim_id
            window["provisional_sample_count"] = snapshot["valid_count"]
            window["provisional_detail_checksum"] = snapshot["detail_checksum"]
        self._write_jsonl_replace(self._windows_file(batch_key), windows)

        return {
            "ok": True,
            "claim_id": claim_id,
            "batch_key": batch_key,
            "claim_kind": CLAIM_INSPECTION,
            "window_count": len(snapshots),
            "provisional_total": claim["provisional_total"],
            "estimate_kind": EST_PROVISIONAL,
            "sample_eligibility": ELIG_PENDING,
            "windows": snapshots,
            "note": "临时估算仅供巡检复算，未审核前不计入正式统计",
        }

    def claim_formal(self, batch_key: str, window_ids: list[str] | None = None,
                     scope: str = SCOPE_ALL) -> dict[str, Any]:
        """正式领取：审核前发起，先落一张待确认凭证。

        scope=all：已通过审核的观测窗与零观测窗一起确认；
        scope=zero_only：只确认零观测窗，作为明确的部分交付。
        """
        if scope not in (SCOPE_ALL, SCOPE_ZERO_ONLY):
            raise CoordWindowError(f"不支持的领取范围：{scope}")
        windows = self._read_jsonl(self._windows_file(batch_key))
        if not windows:
            raise CoordWindowError(
                f"批次 {batch_key} 尚未登记格时窗口，无法发起正式领取"
            )
        wanted = set(window_ids or [])
        candidates = [
            window for window in windows if not wanted or window["window_id"] in wanted
        ]
        if wanted:
            missing = sorted(wanted - {window["window_id"] for window in candidates})
            if missing:
                raise CoordWindowError(f"以下窗口不存在：{'、'.join(missing)}")

        zero = [w for w in candidates if w.get("observation_status") == OBS_NO_OBSERVATION]
        # 有观测的窗（无论此刻是否已审）都应进入正式领取：领取在审核前发起，
        # 确认时再按审核结果决定它只是零窗部分交付，还是连同观测一起交付。
        observed = [w for w in candidates if (w.get("valid_count") or 0) > 0]
        if scope == SCOPE_ZERO_ONLY:
            selected = [w for w in zero if w.get("zero_confirmed")]
        else:
            selected = observed + zero
        if not selected:
            if scope == SCOPE_ZERO_ONLY:
                raise CoordWindowError("没有已确认的零观测窗可单独结算")
            raise CoordWindowError("该批次没有可发起正式领取的格时窗口")

        claim_id = self._new_claim_id(CLAIM_FORMAL)
        detail_rows = self._read_jsonl(self._detail_file(batch_key))
        detail_by_window: dict[str, list[dict[str, Any]]] = {}
        for row in detail_rows:
            detail_by_window.setdefault(row["window_id"], []).append(row)
        snapshots = [
            self._snapshot_window(window, detail_by_window.get(window["window_id"], []),
                                  role="formal")
            for window in selected
        ]
        has_observations = any(item["valid_count"] for item in snapshots)
        claim = {
            "claim_id": claim_id,
            "batch_key": batch_key,
            "claim_kind": CLAIM_FORMAL,
            "status": CLAIM_OPENED,
            "scope": scope,
            "audit_required": True,
            "created_at": time.time(),
            "window_ids": [item["window_id"] for item in snapshots],
            "windows": snapshots,
            "zero_window_ids": [item["window_id"] for item in snapshots if not item["valid_count"]],
            "observation_window_ids": [item["window_id"] for item in snapshots if item["valid_count"]],
            "delivery": DELIVERY_WITH_OBSERVATIONS if has_observations else DELIVERY_ZERO_ONLY,
            "formal_version": None,
        }
        self._write_claim(claim)
        for window in windows:
            if window["window_id"] in claim["window_ids"]:
                window["pending_formal_claim_id"] = claim_id
        self._write_jsonl_replace(self._windows_file(batch_key), windows)

        return {
            "ok": True,
            "claim_id": claim_id,
            "batch_key": batch_key,
            "claim_kind": CLAIM_FORMAL,
            "status": CLAIM_OPENED,
            "scope": scope,
            "audit_required": True,
            "delivery": claim["delivery"],
            "window_count": len(snapshots),
            "window_ids": claim["window_ids"],
            "zero_window_ids": claim["zero_window_ids"],
            "observation_window_ids": claim["observation_window_ids"],
            "windows": snapshots,
            "note": "审核确认前该领取不产出正式数值",
        }

    # ---------------- 独立审核 ----------------

    def audit_windows(self, batch_key: str, audit: dict[str, Any]) -> dict[str, Any]:
        """独立审核：确认观测可用于正式统计，可对坐标纠偏，零观测窗单独确认。

        audit 至少包含 audit_id 与由谁执行；`observations_confirmed` 列出确认
        可用的 point_id，`zero_windows_confirmed` 列出确认仍然为零的窗口，
        `corrections` 可按 point_id 给纠偏坐标。
        """
        audit_id = str(audit.get("audit_id") or "").strip()
        if not audit_id:
            raise CoordWindowError("审核必须带 audit_id")
        reviewer = str(audit.get("reviewer") or "").strip()
        if not reviewer:
            raise CoordWindowError("审核必须记录执行人 reviewer")
        existing = self._read_jsonl(self._audit_file(batch_key))
        if any(row.get("audit_id") == audit_id for row in existing):
            raise CoordWindowError(f"审核 {audit_id} 已登记，不能重复提交")

        windows = self._read_jsonl(self._windows_file(batch_key))
        if not windows:
            raise CoordWindowError(f"批次 {batch_key} 尚未登记格时窗口")
        by_id = {window["window_id"]: window for window in windows}

        confirmed = {str(item) for item in (audit.get("observations_confirmed") or [])}
        corrections_raw = audit.get("corrections") or {}
        corrections: dict[str, dict[str, float]] = {}
        for point_id, value in corrections_raw.items():
            if isinstance(value, dict):
                lat, lon = value.get("lat"), value.get("lon")
            elif isinstance(value, (list, tuple)) and len(value) == 2:
                lat, lon = value
            else:
                raise CoordWindowError(f"点 {point_id} 的纠偏格式无法识别")
            lat_f, lon_f = _as_float(lat), _as_float(lon)
            if lat_f is None or lon_f is None:
                raise CoordWindowError(f"点 {point_id} 的纠偏坐标不是有效数值")
            if not (-90 <= lat_f <= 90 and -180 <= lon_f <= 180):
                raise CoordWindowError(f"点 {point_id} 的纠偏坐标越界")
            corrections[str(point_id)] = {"lat": lat_f, "lon": lon_f}

        zero_confirmed = [str(item) for item in (audit.get("zero_windows_confirmed") or [])]
        window_lookup = {window["window_id"] for window in windows}
        # 允许纠正点 id 不在显式确认列表里，但必须能落到窗口
        for window_id in zero_confirmed:
            if window_id not in window_lookup:
                raise CoordWindowError(f"零观测窗 {window_id} 不存在")
            if (by_id[window_id].get("valid_count") or 0) > 0:
                raise CoordWindowError(
                    f"窗口 {window_id} 已有有效观测，不能当作零观测窗确认")
        detail_rows = self._read_jsonl(self._detail_file(batch_key))
        detail_by_window: dict[str, list[dict[str, Any]]] = {}
        point_to_window: dict[str, str] = {}
        for row in detail_rows:
            detail_by_window.setdefault(row["window_id"], []).append(row)
            point_to_window[str(row.get("point_id"))] = row["window_id"]
        for point_id in confirmed | set(corrections):
            if point_id not in point_to_window:
                raise CoordWindowError(f"观测点 {point_id} 不在任何格时窗口中")

        # 给了纠偏坐标等同于确认这个观测可用：纠偏不是"好看一下"，而是采纳。
        confirmed |= set(corrections)

        record = {
            "audit_id": audit_id,
            "batch_key": batch_key,
            "reviewer": reviewer,
            "independent": bool(audit.get("independent", True)),
            "observations_confirmed": sorted(confirmed),
            "zero_windows_confirmed": sorted(zero_confirmed),
            "corrections": corrections,
            "notes": audit.get("notes"),
            "created_at": time.time(),
        }
        existing.append(record)
        self._write_jsonl_replace(self._audit_file(batch_key), existing)

        # 重新计算受影响窗口的明细与状态，审核通过的点才计入正式样本。
        touched = {point_to_window[point_id] for point_id in confirmed | set(corrections)}
        touched |= {window_id for window_id in zero_confirmed if window_id in window_lookup}
        confirmed_by_window: dict[str, set[str]] = {}
        for point_id in confirmed:
            confirmed_by_window.setdefault(point_to_window[point_id], set()).add(point_id)
        for window_id in touched:
            window = by_id[window_id]
            source_points = []
            for row in detail_by_window.get(window_id, []):
                source_points.append({
                    "point_id": row.get("point_id"),
                    "lat": row.get("orig_lat"),
                    "lon": row.get("orig_lon"),
                    "trans_lat": row.get("trans_lat"),
                    "trans_lon": row.get("trans_lon"),
                    "timestamp": row.get("timestamp"),
                    "accuracy": row.get("accuracy"),
                    "terminal": row.get("terminal"),
                })
            if window_id in zero_confirmed:
                is_zero = True
            else:
                is_zero = not source_points
            new_detail = [
                self._point_detail(batch_key, window, point, audit=record)
                for point in source_points
            ]
            for row in new_detail:
                row["diagnostic"]["audit_id"] = audit_id
            detail_by_window[window_id] = new_detail
            detail_rows = [
                row for row in detail_rows if row["window_id"] != window_id
            ] + new_detail
            summary = self._summarize_detail(new_detail)
            audited_ids = confirmed_by_window.get(window_id, set())
            status, eligibility, reason = self._window_status(new_detail, audited_ids, is_zero)
            window.update({
                "observation_status": status,
                "sample_eligibility": eligibility,
                "empty_reason": reason or (
                    "该格在时间窗内没有观测，已由审核确认" if is_zero else ""
                ),
                "observation_count": summary["observation_count"],
                "valid_count": summary["valid_count"],
                "invalid_reason_counts": summary["invalid_reason_counts"],
                "detail_count": len(new_detail),
                "detail_checksum": summary["detail_checksum"],
                "audit_id": audit_id,
                "audit_verified_count": len(audited_ids),
                "zero_confirmed": bool(is_zero and window_id in zero_confirmed),
            })

        self._write_jsonl_replace(self._windows_file(batch_key), windows)
        self._write_jsonl_replace(self._detail_file(batch_key), detail_rows)
        for window_id in touched:
            window = by_id[window_id]
            # 审核后的正式值直接冻结，领取确认时引用这一份。
            if window.get("sample_eligibility") == ELIG_FORMAL and window.get("valid_count"):
                window["formal_estimate"] = round(
                    sum(row["contribution"] for row in detail_by_window[window_id]), 6
                )
                window["formal_sample_count"] = window["valid_count"]
            elif window.get("observation_status") == OBS_NO_OBSERVATION:
                window["formal_estimate"] = 0.0
                window["formal_sample_count"] = 0
        self._write_jsonl_replace(self._windows_file(batch_key), windows)

        return {
            "ok": True,
            "audit_id": audit_id,
            "batch_key": batch_key,
            "touched_windows": sorted(touched),
            "confirmed_points": sorted(confirmed),
            "corrections": corrections,
            "windows": [by_id[window_id] for window_id in sorted(touched)],
        }

    # ---------------- 领取确认与正式版本 ----------------

    def get_claim(self, claim_id: str) -> dict[str, Any]:
        claim = self._read_json_file(self._claim_file(claim_id))
        if claim is None:
            raise CoordWindowError(f"领取号 {claim_id} 不存在")
        return claim

    def list_claims(self, batch_key: str | None = None,
                    claim_kind: str | None = None) -> dict[str, Any]:
        directory = self._data_dir("coord_window_claims")
        claims = []
        for file in sorted(directory.glob("*.json")):
            claim = self._read_json_file(file)
            if not claim:
                continue
            if batch_key and claim.get("batch_key") != batch_key:
                continue
            if claim_kind and claim.get("claim_kind") != claim_kind:
                continue
            claims.append({
                "claim_id": claim["claim_id"],
                "batch_key": claim["batch_key"],
                "claim_kind": claim.get("claim_kind"),
                "status": claim.get("status"),
                "scope": claim.get("scope"),
                "delivery": claim.get("delivery"),
                "formal_version": claim.get("formal_version"),
                "window_count": len(claim.get("windows") or []),
                "provisional_total": claim.get("provisional_total"),
                "created_at": claim.get("created_at"),
                "confirmed_at": claim.get("confirmed_at"),
            })
        return {
            "ok": True,
            "total": len(claims),
            "claims": claims,
            "empty_reason": "" if claims else "没有符合条件的领取记录",
        }

    def replay_claim(self, claim_id: str) -> dict[str, Any]:
        """按领取号复算：只用凭证里的快照点和坐标诊断，不读原始点文件。"""
        claim = self.get_claim(claim_id)
        windows = claim.get("windows") or []
        recomputed = []
        for item in windows:
            detail = item.get("detail") or []
            checksum = _checksum(detail)
            value = round(sum(row.get("contribution", 0.0) for row in detail), 6)
            recomputed.append({
                "window_id": item["window_id"],
                "geohash": item.get("geohash"),
                "time_bucket": item.get("time_bucket"),
                "observation_status": item.get("observation_status"),
                "sample_eligibility": item.get("sample_eligibility"),
                "role": item.get("role"),
                "valid_count": item.get("valid_count"),
                "observation_count": item.get("observation_count"),
                "claimed_estimate": item.get("estimate"),
                "recomputed_estimate": value,
                "matches": value == item.get("estimate") and checksum == item.get("detail_checksum"),
                "detail_checksum": checksum,
                "points": [
                    {
                        "point_id": row.get("point_id"),
                        "adopted_lat": row.get("adopted_lat"),
                        "adopted_lon": row.get("adopted_lon"),
                        "coordinate_source": row.get("coordinate_source"),
                        "valid": row.get("valid"),
                        "weight": row.get("weight"),
                        "contribution": row.get("contribution"),
                        "diagnostic": row.get("diagnostic"),
                    }
                    for row in detail
                ],
            })
        total = round(sum(item["recomputed_estimate"] for item in recomputed), 6)
        return {
            "ok": True,
            "claim_id": claim_id,
            "batch_key": claim["batch_key"],
            "claim_kind": claim.get("claim_kind"),
            "status": claim.get("status"),
            "scope": claim.get("scope"),
            "delivery": claim.get("delivery"),
            "formal_version": claim.get("formal_version"),
            "estimate_kind": (
                EST_FORMAL if claim.get("claim_kind") == CLAIM_FORMAL
                and claim.get("status") in (CLAIM_CONFIRMED, CLAIM_PARTIAL)
                else EST_PROVISIONAL
            ),
            "claimed_total": claim.get("recomputed_total_at_issue", claim.get("provisional_total")),
            "recomputed_total": total,
            "all_match": all(item["matches"] for item in recomputed),
            "windows": recomputed,
        }

    def confirm_claim(self, claim_id: str, formal_version: int | None = None) -> dict[str, Any]:
        """审核后确认正式领取，产出新版本；审核前发起的领取会按当前资格重新认窗。

        - scope=zero_only：只交付已确认的零观测窗，明确标记为部分交付；
        - scope=all：已通过审核的观测窗 + 已确认的零观测窗一起确认；
        - 审核前领取、审核后确认时，同一领取号按确认时的审核结果决定交付范围。
        """
        claim = self.get_claim(claim_id)
        if claim.get("claim_kind") != CLAIM_FORMAL:
            raise CoordWindowError(f"领取号 {claim_id} 不是正式领取，不能确认正式结果")
        if claim.get("status") in (CLAIM_CONFIRMED, CLAIM_PARTIAL):
            # 重复确认返回首次结果，不重复升版本。
            return self._confirmed_response(claim)

        batch_key = claim["batch_key"]
        windows = self._read_jsonl(self._windows_file(batch_key))
        if not windows:
            raise CoordWindowError(f"批次 {batch_key} 的格时窗口已不存在，无法确认领取")
        by_id = {window["window_id"]: window for window in windows}
        scoped = [by_id[wid] for wid in claim["window_ids"] if wid in by_id]

        zero = [w for w in scoped if w.get("observation_status") == OBS_NO_OBSERVATION]
        # 确认时以审核后的资格为准：只有正式样本才算"已有两点"的结果，
        # 未通过审核的观测窗留在"其余窗"里另起领取。
        formal_samples = [w for w in scoped if w.get("sample_eligibility") == ELIG_FORMAL]
        if claim.get("scope") == SCOPE_ZERO_ONLY:
            selected = [w for w in zero if w.get("zero_confirmed") or w.get("formal_claim_id")]
        else:
            selected = formal_samples + zero
        if not selected:
            if claim.get("scope") == SCOPE_ZERO_ONLY:
                raise CoordWindowError("该领取范围内没有已确认的零观测窗，无法部分交付")
            raise CoordWindowError(
                "审核后仍没有可用于正式统计的窗口，请先完成独立审核再确认"
            )

        version = int(formal_version) if formal_version is not None else self._next_version(batch_key)
        if version < 1:
            raise CoordWindowError("formal_version 必须为正整数")
        if self._formal_cells_file(batch_key, version).exists():
            raise CoordWindowError(f"正式版本 v{version} 已存在")

        detail_rows = self._read_jsonl(self._detail_file(batch_key))
        detail_by_window: dict[str, list[dict[str, Any]]] = {}
        for row in detail_rows:
            detail_by_window.setdefault(row["window_id"], []).append(row)

        cells = []
        formal_detail: list[dict[str, Any]] = []
        for window in selected:
            detail = detail_by_window.get(window["window_id"], [])
            formal_points = [row for row in detail if row.get("diagnostic", {}).get("audited")]
            contribution = round(sum(row["contribution"] for row in formal_points), 6)
            cells.append({
                "window_id": window["window_id"],
                "batch_key": batch_key,
                "geohash": window.get("geohash"),
                "time_bucket": window["time_bucket"],
                "time_bucket_str": window.get("time_bucket_str"),
                "observation_status": window.get("observation_status"),
                "sample_eligibility": window.get("sample_eligibility"),
                "empty_reason": window.get("empty_reason"),
                "formal_sample_count": len(formal_points),
                "estimate_kind": EST_FORMAL if formal_points else EST_NONE,
                "formal_estimate": contribution,
                "audit_id": window.get("audit_id"),
                "claim_id": claim_id,
                "version": version,
            })
            for row in formal_points:
                frozen = dict(row)
                frozen.update({
                    "version": version,
                    "claim_id": claim_id,
                    "estimate_kind": EST_FORMAL,
                })
                formal_detail.append(frozen)

        self._write_jsonl_replace(self._formal_cells_file(batch_key, version), cells)
        self._write_jsonl_replace(self._formal_detail_file(batch_key, version), formal_detail)

        has_observations = any(cell["formal_sample_count"] for cell in cells)
        delivery = DELIVERY_WITH_OBSERVATIONS if has_observations else DELIVERY_ZERO_ONLY
        formal_total = round(sum(cell["formal_estimate"] for cell in cells), 6)
        delivered = {cell["window_id"] for cell in cells}
        previously_delivered: set[str] = set()
        if version > 1:
            previously_delivered = {
                row["window_id"]
                for row in self._read_jsonl(self._formal_cells_file(batch_key, version - 1))
            }
        # "其余窗" = 还没交付过的窗口，供调用方另起领取时直接引用。
        pending = [
            window["window_id"] for window in windows
            if window["window_id"] not in delivered
            and window["window_id"] not in previously_delivered
        ]
        claim.update({
            "status": CLAIM_CONFIRMED,
            "delivery": delivery,
            "formal_version": version,
            "confirmed_at": time.time(),
            "formal_total": formal_total,
            "delivered_window_ids": [cell["window_id"] for cell in cells],
            "zero_window_ids": [cell["window_id"] for cell in cells
                                if cell["observation_status"] == OBS_NO_OBSERVATION],
            "observation_window_ids": [cell["window_id"] for cell in cells
                                       if cell["formal_sample_count"]],
            "cells_checksum": _checksum(cells),
            "pending_window_ids": pending,
        })
        self._write_claim(claim)

        for window in windows:
            if window["window_id"] not in claim["delivered_window_ids"]:
                continue
            cell = next(cell for cell in cells if cell["window_id"] == window["window_id"])
            window["formal_estimate"] = cell["formal_estimate"]
            window["formal_sample_count"] = cell["formal_sample_count"]
            window["formal_version"] = version
            window["formal_claim_id"] = claim_id
            window["estimate_kind"] = cell["estimate_kind"]
            window["sample_eligibility"] = cell["sample_eligibility"]
            window.pop("pending_formal_claim_id", None)
        self._write_jsonl_replace(self._windows_file(batch_key), windows)

        return {
            "ok": True,
            "claim_id": claim_id,
            "batch_key": batch_key,
            "status": CLAIM_CONFIRMED,
            "scope": claim.get("scope"),
            "delivery": delivery,
            "formal_version": version,
            "window_count": len(cells),
            "formal_total": formal_total,
            "zero_window_ids": claim["zero_window_ids"],
            "observation_window_ids": claim["observation_window_ids"],
            "pending_window_ids": claim["pending_window_ids"],
            "partial_delivery": delivery == DELIVERY_ZERO_ONLY,
            "cells": cells,
            "formal_cells_file": str(self._formal_cells_file(batch_key, version)),
            "formal_detail_file": str(self._formal_detail_file(batch_key, version)),
        }

    def _confirmed_response(self, claim: dict[str, Any]) -> dict[str, Any]:
        batch_key = claim["batch_key"]
        version = claim.get("formal_version")
        cells = (
            self._read_jsonl(self._formal_cells_file(batch_key, int(version)))
            if version else []
        )
        return {
            "ok": True,
            "claim_id": claim["claim_id"],
            "batch_key": batch_key,
            "status": claim.get("status"),
            "scope": claim.get("scope"),
            "delivery": claim.get("delivery"),
            "formal_version": version,
            "window_count": len(cells),
            "formal_total": claim.get("formal_total"),
            "zero_window_ids": claim.get("zero_window_ids", []),
            "observation_window_ids": claim.get("observation_window_ids", []),
            "pending_window_ids": claim.get("pending_window_ids", []),
            "partial_delivery": claim.get("delivery") == DELIVERY_ZERO_ONLY,
            "cells": cells,
            "replayed": True,
            "formal_cells_file": (
                str(self._formal_cells_file(batch_key, int(version))) if version else None
            ),
            "formal_detail_file": (
                str(self._formal_detail_file(batch_key, int(version))) if version else None
            ),
        }

    def _next_version(self, batch_key: str) -> int:
        versions = [
            int(file.stem.rsplit("_v", 1)[1])
            for file in self._data_dir("coord_formal").glob(f"{batch_key}_v*.jsonl")
            if "_detail" not in file.stem and "_v" in file.stem
            and file.stem.rsplit("_v", 1)[1].isdigit()
        ]
        return max(versions, default=0) + 1

    def list_formal_versions(self, batch_key: str) -> dict[str, Any]:
        base = self._data_dir("coord_formal")
        versions = sorted(
            int(file.stem.rsplit("_v", 1)[1])
            for file in base.glob(f"{batch_key}_v*.jsonl")
            if "_detail" not in file.stem and file.stem.rsplit("_v", 1)[1].isdigit()
        )
        items = []
        for version in versions:
            cells = self._read_jsonl(self._formal_cells_file(batch_key, version))
            items.append({
                "version": version,
                "window_count": len(cells),
                "formal_total": round(sum(cell["formal_estimate"] for cell in cells), 6),
                "observation_windows": sum(1 for cell in cells if cell["formal_sample_count"]),
                "zero_windows": sum(
                    1 for cell in cells
                    if cell["observation_status"] == OBS_NO_OBSERVATION
                ),
                "delivery": (
                    DELIVERY_WITH_OBSERVATIONS
                    if any(cell["formal_sample_count"] for cell in cells)
                    else DELIVERY_ZERO_ONLY
                ),
                "checksum": _checksum(cells),
            })
        return {
            "ok": True,
            "batch_key": batch_key,
            "total": len(items),
            "versions": items,
            "empty_reason": "" if items else f"批次 {batch_key} 还没有正式版本",
        }

    def get_formal_result(self, batch_key: str, version: int | None = None,
                          include_detail: bool = False) -> dict[str, Any]:
        """读取正式结果；默认取最新版本，逐点明细随版本冻结、可单独复算。"""
        versions = self.list_formal_versions(batch_key)["versions"]
        if not versions:
            return {
                "ok": True,
                "batch_key": batch_key,
                "version": None,
                "cells": [],
                "empty_reason": f"批次 {batch_key} 还没有正式版本，临时估算不计入正式统计",
            }
        chosen = int(version) if version is not None else versions[-1]["version"]
        cells = self._read_jsonl(self._formal_cells_file(batch_key, chosen))
        if not cells:
            raise CoordWindowError(f"批次 {batch_key} 的正式版本 v{chosen} 不存在")
        detail = self._read_jsonl(self._formal_detail_file(batch_key, chosen))
        payload: dict[str, Any] = {
            "ok": True,
            "batch_key": batch_key,
            "version": chosen,
            "available_versions": [item["version"] for item in versions],
            "formal_total": round(sum(cell["formal_estimate"] for cell in cells), 6),
            "observation_windows": sum(1 for cell in cells if cell["formal_sample_count"]),
            "zero_windows": sum(
                1 for cell in cells if cell["observation_status"] == OBS_NO_OBSERVATION
            ),
            "delivery": (
                DELIVERY_WITH_OBSERVATIONS
                if any(cell["formal_sample_count"] for cell in cells)
                else DELIVERY_ZERO_ONLY
            ),
            "cells_checksum": _checksum(cells),
            "detail_checksum": _checksum(detail),
            "detail_count": len(detail),
            "cells": cells,
            "empty_reason": "",
        }
        if include_detail:
            by_window: dict[str, list[dict[str, Any]]] = {}
            for row in detail:
                by_window.setdefault(row["window_id"], []).append(row)
            for cell in cells:
                cell["detail"] = by_window.get(cell["window_id"], [])
        payload["detail"] = detail
        return payload
