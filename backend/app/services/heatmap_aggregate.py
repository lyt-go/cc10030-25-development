"""热力聚合领域服务：按网格层级和时间粒度对定位点进行聚合计算，生成热力聚合数据集。"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.services.coord_window import (
    ELIG_FORMAL, EST_FORMAL, EST_NONE, EST_PROVISIONAL,
)
from app.store import store


class HeatmapAggregateService(BaseModuleService):
    MODULE = "heatmap_aggregate"
    ENTITY = "聚合窗口"
    OPERATION_TERM = "聚合"
    LIST_FIELDS = ["窗口编号", "网格层级", "时间粒度", "聚合指标", "落点总量", "峰值网格", "窗口类型", "聚合水位"]
    REQUIRED_FIELDS = ["窗口编号", "网格层级", "时间粒度"]
    STATUS_ORDER = ["草稿", "待聚合", "聚合中", "已产出"]
    ACTION_RULES = {"启动聚合": "聚合中", "确认产出": "已产出", "标记作废": "已产出"}
    NEGATIVE_ACTIONS = []

    def aggregate_grid_heatmap(self, batch_key: str, grid_level: str = "L6",
                                time_bucket_sec: int = 3600) -> dict[str, Any]:
        """读取网格索引数据集，按网格层级和时间粒度进行热力聚合，生成热力聚合数据集文件。"""
        try:
            grid_svc = self._safe_import("app.services.grid_index", "GridIndexService")()
            grids, _ = grid_svc.get_grid_index(batch_key)
        except FileNotFoundError:
            grids_file = self._data_dir("grid_index") / f"{batch_key}_grid.jsonl"
            if not grids_file.exists():
                raise FileNotFoundError(f"批次 {batch_key} 的网格索引数据不存在，请先生成网格索引")
            grids = self._read_jsonl(grids_file)

        if not grids:
            return {"batch_key": batch_key, "heatmap_cells": 0}

        # 读取坐标转换后的定位点
        points_file = self._data_dir("coord_transformed") / f"{batch_key}_transformed.jsonl"
        if not points_file.exists():
            points_file = self._data_dir("geo_points") / f"{batch_key}.jsonl"
        points = self._read_jsonl(points_file) if points_file.exists() else []

        if not points:
            return {"batch_key": batch_key, "heatmap_cells": 0}

        # 按网格和时间桶聚合
        heatmap: dict[str, dict[str, Any]] = {}
        for p in points:
            lat = p.get("trans_lat", p.get("lat", 0))
            lon = p.get("trans_lon", p.get("lon", 0))
            gh = self._geohash_encode(lat, lon, 6)
            ts = p.get("timestamp", 0)
            time_bucket = int(ts // time_bucket_sec) * time_bucket_sec
            key = f"{gh}_{time_bucket}"
            if key not in heatmap:
                heatmap[key] = {
                    "geohash": gh,
                    "time_bucket": time_bucket,
                    "point_count": 0,
                    "lats": [],
                    "lons": [],
                }
            heatmap[key]["point_count"] += 1
            heatmap[key]["lats"].append(lat)
            heatmap[key]["lons"].append(lon)

        cells = []
        cell_id = 0
        peak_grid = None
        peak_count = 0
        total_points = 0
        for key, info in heatmap.items():
            cell_id += 1
            total_points += info["point_count"]
            center_lat = sum(info["lats"]) / len(info["lats"])
            center_lon = sum(info["lons"]) / len(info["lons"])
            cells.append({
                "cell_id": cell_id,
                "batch_key": batch_key,
                "geohash": info["geohash"],
                "grid_level": grid_level,
                "time_bucket": info["time_bucket"],
                "time_bucket_str": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(info["time_bucket"])),
                "point_count": info["point_count"],
                "center_lat": round(center_lat, 6),
                "center_lon": round(center_lon, 6),
            })
            if info["point_count"] > peak_count:
                peak_count = info["point_count"]
                peak_grid = info["geohash"]

        output_file = self._data_dir("heatmap") / f"{batch_key}_heatmap.jsonl"
        written = self._write_jsonl(output_file, cells)

        return {
            "batch_key": batch_key,
            "grid_level": grid_level,
            "time_bucket_sec": time_bucket_sec,
            "heatmap_cells": written,
            "total_points_aggregated": total_points,
            "peak_grid": peak_grid,
            "peak_count": peak_count,
            "output_file": str(output_file),
        }

    def get_heatmap_data(self, batch_key: str) -> tuple[list[dict[str, Any]], str]:
        """读取指定批次的热力聚合数据集文件。"""
        file_path = self._data_dir("heatmap") / f"{batch_key}_heatmap.jsonl"
        if not file_path.exists():
            raise FileNotFoundError(f"批次 {batch_key} 的热力聚合数据集不存在")
        data = self._read_jsonl(file_path)
        return data, str(file_path)

    def window_heatmap(self, batch_key: str) -> dict[str, Any]:
        """路由层入口的别名，便于按动作名直接挂载。"""
        return self.get_window_heatmap(batch_key)

    def get_window_heatmap(self, batch_key: str) -> dict[str, Any]:
        """按格时窗口输出热力视图，明确区分临时估算、正式统计与空窗。

        巡检阶段只有临时样本；正式统计只认审核后冻结的正式版本。空窗照样
        出现在结果里并带原因，不能因为落点为零就从热力侧消失。
        """
        svc = self._safe_import("app.services.coord_window", "CoordWindowService")()
        window_payload = svc.list_windows(batch_key=batch_key)
        windows = window_payload["windows"]
        if not windows:
            return {
                "ok": True,
                "batch_key": batch_key,
                "cells": [],
                "provisional_total": 0.0,
                "formal_total": 0.0,
                "eligible_totals": {EST_PROVISIONAL: 0, EST_FORMAL: 0},
                "empty_reason": window_payload.get("empty_reason") or "该批次没有格时窗口",
            }

        cells: list[dict[str, Any]] = []
        for window in windows:
            formal_estimate = window.get("formal_estimate")
            provisional_estimate = window.get("provisional_estimate")
            if formal_estimate is not None and window.get("sample_eligibility") == ELIG_FORMAL:
                headline = {
                    "estimate_kind": EST_FORMAL,
                    "estimate": formal_estimate,
                    "sample_count": window.get("formal_sample_count", 0),
                }
            elif provisional_estimate is not None:
                headline = {
                    "estimate_kind": EST_PROVISIONAL,
                    "estimate": provisional_estimate,
                    "sample_count": window.get("provisional_sample_count", 0),
                }
            else:
                headline = {"estimate_kind": EST_NONE, "estimate": None, "sample_count": 0}
            cells.append({
                "window_id": window["window_id"],
                "geohash": window.get("geohash"),
                "time_bucket": window["time_bucket"],
                "time_bucket_str": window.get("time_bucket_str"),
                "observation_status": window.get("observation_status"),
                "sample_eligibility": window.get("sample_eligibility"),
                "empty_reason": window.get("empty_reason") or "",
                "observation_count": window.get("observation_count", 0),
                "valid_count": window.get("valid_count", 0),
                "provisional_estimate": provisional_estimate,
                "formal_estimate": formal_estimate,
                "formal_version": window.get("formal_version"),
                "claim_id": window.get("provisional_claim_id") or window.get("formal_claim_id"),
                **headline,
            })

        empty_reason = ""
        eligible_totals = {
            EST_PROVISIONAL: round(sum(
                cell["provisional_estimate"] for cell in cells
                if cell["provisional_estimate"] is not None
            ), 6),
            EST_FORMAL: round(sum(
                cell["formal_estimate"] for cell in cells
                if cell["formal_estimate"] is not None
            ), 6),
        }
        return {
            "ok": True,
            "batch_key": batch_key,
            "cells": cells,
            "provisional_total": eligible_totals[EST_PROVISIONAL],
            "formal_total": eligible_totals[EST_FORMAL],
            "eligible_totals": eligible_totals,
            "formal_versions": sorted({
                cell["formal_version"] for cell in cells
                if cell["formal_version"] is not None
            }),
            "empty_reason": empty_reason,
        }
