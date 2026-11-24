"""格时窗口 / 样本资格 / 巡检领取 / 独立审核 / 正式版本 的端到端测试。

覆盖题面要求：
- 空观测不报错、空窗有原因；
- 中断重试保留实际采用范围；
- 按格标记样本资格，热力侧区分临时估算与正式统计；
- 巡检领取后原始点文件被改动，旧领取号仍可复算；
- 正式结果按新版本核对；
- 零窗部分交付与全量确认两条路径；审核前发起、审核后确认。
"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def data_root(monkeypatch):
    tmpdir = Path(tempfile.mkdtemp(prefix="geo_window_"))
    import app.services.base as base_mod
    original = base_mod.DATA_ROOT
    base_mod.DATA_ROOT = tmpdir
    for sub in [
        "geo_points", "coord_transformed", "coord_windows", "coord_window_claims",
        "coord_window_audits", "coord_formal", "heatmap", "checkpoints",
    ]:
        (tmpdir / sub).mkdir(parents=True, exist_ok=True)
    yield tmpdir
    base_mod.DATA_ROOT = original
    shutil.rmtree(tmpdir, ignore_errors=True)


def _write_points(root: Path, batch_key: str, points: list[dict]) -> Path:
    path = root / "coord_transformed" / f"{batch_key}_transformed.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for point in points:
            f.write(json.dumps(point, ensure_ascii=False) + "\n")
    return path


def _point(point_id: int, lat: float, lon: float, ts: int, accuracy: float = 5.0) -> dict:
    return {
        "point_id": point_id,
        "batch_key": "B1",
        "orig_lat": lat,
        "orig_lon": lon,
        "trans_lat": lat,
        "trans_lon": lon,
        "timestamp": ts,
        "accuracy": accuracy,
        "terminal": "TERM-1",
    }


BATCH = "B1"
BUCKET = 3600
# 两个观测点落在同一格同一小时窗；零窗显式登记在别处
OBS_TS_A = 1_700_000_000
OBS_TS_B = 1_700_000_100
ZERO_TS = 1_700_007_200


def _service():
    from app.services.coord_window import CoordWindowService
    return CoordWindowService()


def _build(service, root: Path, limit: int | None = None):
    lat, lon = 39.9042, 116.4074
    geohash = service._geohash_encode(lat, lon, 6)
    points = [
        _point(1, lat, lon, OBS_TS_A, 4.0),
        _point(2, lat + 0.00001, lon + 0.00001, OBS_TS_B, 8.0),
    ]
    _write_points(root, BATCH, points)
    zero_bucket = int(ZERO_TS // BUCKET) * BUCKET
    result = service.build_windows(
        BATCH,
        expected_windows=[{"geohash": geohash, "time_bucket": zero_bucket, "reason": "该格该小时无观测"}],
        limit=limit,
    )
    obs_bucket = int(OBS_TS_A // BUCKET) * BUCKET
    obs_window = f"{BATCH}:{geohash}:{obs_bucket}:6"
    zero_window = f"{BATCH}:{geohash}:{zero_bucket}:6"
    return result, geohash, obs_window, zero_window


class TestWindowsAndEligibility:
    def test_empty_batch_returns_reason_not_error(self, data_root):
        service = _service()
        _write_points(data_root, BATCH, [])
        result = service.build_windows(BATCH)
        assert result["ok"] is True
        assert result["window_count"] == 0
        assert result["empty_reason"]

    def test_zero_window_kept_with_reason(self, data_root):
        service = _service()
        result, _, obs_window, zero_window = _build(service, data_root)
        assert result["window_count"] == 2
        windows = {w["window_id"]: w for w in result["windows"]}
        zero = windows[zero_window]
        assert zero["observation_status"] == "无观测"
        assert zero["sample_eligibility"] == "无样本"
        assert zero["empty_reason"]
        assert windows[obs_window]["observation_status"] == "待校验"
        assert windows[obs_window]["valid_count"] == 2

    def test_invalid_coordinates_are_distinct_status(self, data_root):
        service = _service()
        points = [
            _point(1, 39.9042, 116.4074, OBS_TS_A),
            {"point_id": 2, "trans_lat": None, "trans_lon": None, "lat": None, "lon": None,
             "timestamp": OBS_TS_A, "accuracy": 5.0},
        ]
        _write_points(data_root, "INVALID", points)
        result = service.build_windows("INVALID")
        statuses = {w["observation_status"] for w in result["windows"]}
        assert "无有效坐标" in statuses
        assert "已确认" not in statuses

    def test_interrupted_retry_keeps_adopted_range(self, data_root):
        service = _service()
        first, _, _, _ = _build(service, data_root, limit=1)
        assert first["interrupted"] is True
        assert first["remaining_window_ids"]
        adopted = first["adopted_range"]
        second = service.build_windows(BATCH)
        assert second["resumed"] is True
        assert second["interrupted"] is False
        assert second["window_count"] == first["window_count"] + 1
        assert second["adopted_range"][0] <= adopted[0]
        assert second["adopted_range"][1] >= adopted[1]
        state = service.get_checkpoint_state(BATCH)
        assert state["status"] == "complete"
        assert state["adopted_range"] == second["adopted_range"]


class TestClaimsAndAudit:
    def test_inspection_claim_survives_source_change(self, data_root):
        service = _service()
        _, _, obs_window, _ = _build(service, data_root)
        claim = service.claim_inspection(BATCH)
        assert claim["claim_id"]
        assert claim["estimate_kind"] == "临时估算"
        before = service.replay_claim(claim["claim_id"])
        assert before["all_match"] is True

        # 改动原始点文件：旧领取号必须仍能复算原值
        _write_points(data_root, BATCH, [
            _point(1, 0.0, 0.0, OBS_TS_A, 99.0),
            _point(2, 0.0, 0.0, OBS_TS_B, 99.0),
        ])
        after = service.replay_claim(claim["claim_id"])
        assert after["recomputed_total"] == before["recomputed_total"]
        assert after["all_match"] is True
        for window in after["windows"]:
            for point in window["points"]:
                assert point["adopted_lat"] is not None
                assert point["coordinate_source"] == "转换坐标"

    def test_claim_before_audit_confirmed_after_audit(self, data_root):
        service = _service()
        _, _, obs_window, zero_window = _build(service, data_root)
        claim = service.claim_formal(BATCH)
        assert claim["status"] == "待确认"
        assert claim["audit_required"] is True
        # 审核前没有正式值
        empty_formal = service.get_formal_result(BATCH)
        assert empty_formal["version"] is None
        assert empty_formal["empty_reason"]

        audit = service.audit_windows(BATCH, {
            "audit_id": "AUD-1",
            "reviewer": "审核员甲",
            "observations_confirmed": ["1", "2"],
            "zero_windows_confirmed": [zero_window],
        })
        assert set(audit["touched_windows"]) == {obs_window, zero_window}

        confirmed = service.confirm_claim(claim["claim_id"])
        assert confirmed["status"] == "已确认"
        assert confirmed["formal_version"] == 1
        assert confirmed["delivery"] == "with_observations"
        assert set(confirmed["observation_window_ids"]) == {obs_window}
        assert set(confirmed["zero_window_ids"]) == {zero_window}
        # 正式结果和巡检值要能分开核对
        formal = service.get_formal_result(BATCH, include_detail=True)
        assert formal["version"] == 1
        assert formal["observation_windows"] == 1
        assert formal["zero_windows"] == 1
        obs_cells = [c for c in formal["cells"] if c["window_id"] == obs_window]
        assert obs_cells and obs_cells[0]["formal_sample_count"] == 2
        assert all(p["estimate_kind"] == "正式统计" for p in formal["detail"])

    def test_zero_only_partial_delivery_then_new_claim(self, data_root):
        service = _service()
        _, _, obs_window, zero_window = _build(service, data_root)
        service.audit_windows(BATCH, {
            "audit_id": "AUD-0",
            "reviewer": "审核员乙",
            "zero_windows_confirmed": [zero_window],
        })
        partial = service.claim_formal(BATCH, scope="zero_only")
        assert partial["observation_window_ids"] == []
        result = service.confirm_claim(partial["claim_id"])
        assert result["delivery"] == "zero_only"
        assert result["partial_delivery"] is True
        assert result["pending_window_ids"]

        # 观测窗后来才审过，另起领取
        service.audit_windows(BATCH, {
            "audit_id": "AUD-2",
            "reviewer": "审核员丙",
            "observations_confirmed": ["1", "2"],
        })
        second = service.claim_formal(BATCH, scope="all")
        assert second["observation_window_ids"] == [obs_window]
        second_result = service.confirm_claim(second["claim_id"])
        assert second_result["formal_version"] == 2
        assert set(second_result["observation_window_ids"]) == {obs_window}

        versions = service.list_formal_versions(BATCH)["versions"]
        assert [v["version"] for v in versions] == [1, 2]
        assert versions[0]["delivery"] == "zero_only"
        assert versions[1]["delivery"] == "with_observations"

    def test_audit_correction_and_independent_check(self, data_root):
        service = _service()
        _, geohash, obs_window, zero_window = _build(service, data_root)
        before = service.get_window(obs_window)
        _write_points(data_root, BATCH, [
            _point(1, 39.9042, 116.4074, OBS_TS_A, 4.0),
            _point(2, 39.90421, 116.40741, OBS_TS_B, 8.0),
        ])
        service.audit_windows(BATCH, {
            "audit_id": "AUD-3",
            "reviewer": "审核员丁",
            "observations_confirmed": ["1", "2"],
            "corrections": {"2": {"lat": 39.9050, "lon": 116.4080}},
        })
        detail = service.get_window_detail(obs_window)
        corrected = [row for row in detail if row["point_id"] == 2][0]
        assert abs(corrected["adopted_lat"] - 39.9050) < 1e-9
        assert corrected["coordinate_source"] == "审核纠偏"
        assert corrected["diagnostic"]["audited"] is True
        # 纠偏后正式值可以逐点复算
        recompute = service.recompute_window(obs_window)
        assert recompute["checksum_matches"] is True
        assert recompute["valid_count"] == 2


class TestHeatmapEligibility:
    def test_heatmap_exposes_eligibility_and_empty_reason(self, data_root):
        service = _service()
        _, _, obs_window, zero_window = _build(service, data_root)
        service.claim_inspection(BATCH)
        from app.services.heatmap_aggregate import HeatmapAggregateService
        heatmap = HeatmapAggregateService()
        payload = heatmap.get_window_heatmap(BATCH)
        assert payload["ok"] is True
        assert payload["empty_reason"] == ""
        assert payload["eligible_totals"]["临时估算"] >= 0
        statuses = {cell["sample_eligibility"] for cell in payload["cells"]}
        assert "临时样本" in statuses or "待校验" in statuses
        assert any(cell["window_id"] == zero_window for cell in payload["cells"])

    def test_heatmap_without_windows_returns_reason(self, data_root):
        from app.services.heatmap_aggregate import HeatmapAggregateService
        payload = HeatmapAggregateService().get_window_heatmap("NOPE")
        assert payload["ok"] is True
        assert payload["cells"] == []
        assert payload["empty_reason"]


class TestScenarioNarrative:
    """按题面叙述走一遍：巡检临时值 → 审核纠偏 → 正式领取前发起/后确认。

    区间最初只有未审核原始点，巡检拿到临时值；正式热力任务打开结果后，
    独立审核纠偏确认该区间两个实际观测可用于正式统计，另一个零观测窗的
    确认一直有效；公开数据与明细能逐点复算，旧领取号仍能复算原巡检值。
    """

    def test_narrative_end_to_end(self, data_root):
        service = _service()
        build, geohash, obs_window, zero_window = _build(service, data_root)

        # 1) 巡检领取：只有未审核原始点，拿到临时值
        inspection = service.claim_inspection(BATCH)
        inspection_id = inspection["claim_id"]
        inspection_window = next(
            w for w in inspection["windows"] if w["window_id"] == obs_window)
        original_inspection_value = inspection_window["estimate"]
        assert inspection_window["estimate_kind"] == "临时估算"

        # 2) 正式热力任务打开结果：此时只有临时估算，没有正式值
        from app.services.heatmap_aggregate import HeatmapAggregateService
        heatmap = HeatmapAggregateService()
        opened = heatmap.get_window_heatmap(BATCH)
        obs_cell = next(c for c in opened["cells"] if c["window_id"] == obs_window)
        assert obs_cell["formal_estimate"] is None
        assert obs_cell["provisional_estimate"] == original_inspection_value
        assert obs_cell["sample_eligibility"] != "正式样本"

        # 3) 第一次正式领取在审核前发起
        formal = service.claim_formal(BATCH)
        assert formal["status"] == "待确认"
        assert set(formal["window_ids"]) == {obs_window, zero_window}

        # 4) 独立审核：纠偏确认两个实际观测；零窗确认仍然有效
        audit = service.audit_windows(BATCH, {
            "audit_id": "AUD-N1",
            "reviewer": "独立审核组-甲",
            "observations_confirmed": ["1"],
            "corrections": {"2": {"lat": 39.90425, "lon": 116.40745}},
            "zero_windows_confirmed": [zero_window],
        })
        assert set(audit["touched_windows"]) == {obs_window, zero_window}
        obs_after_audit = service.get_window(obs_window)
        assert obs_after_audit["sample_eligibility"] == "正式样本"
        assert obs_after_audit["observation_status"] == "已确认"

        # 5) 审完再确认同一领取：包含两个观测点，不是零窗部分成果
        confirmed = service.confirm_claim(formal["claim_id"])
        assert confirmed["delivery"] == "with_observations"
        assert confirmed["observation_window_ids"] == [obs_window]
        assert confirmed["zero_window_ids"] == [zero_window]
        formal_cell = next(c for c in confirmed["cells"] if c["window_id"] == obs_window)
        assert formal_cell["formal_sample_count"] == 2

        # 6) 公开数据与明细逐点复算：正式值 = 冻结明细贡献之和
        formal_result = service.get_formal_result(BATCH, include_detail=True)
        detail_points = [
            p for p in formal_result["detail"] if p["window_id"] == obs_window]
        assert len(detail_points) == 2
        assert round(sum(p["contribution"] for p in detail_points), 6) == \
            formal_cell["formal_estimate"]
        # 纠偏点采用纠偏坐标，另一点采用转换坐标
        sources = {p["point_id"]: p["coordinate_source"] for p in detail_points}
        assert sources[2] == "审核纠偏"
        assert sources[1] == "转换坐标"

        # 7) 巡检员用旧领取号复算，原巡检值不变，且不读原始文件
        replay = service.replay_claim(inspection_id)
        assert replay["all_match"] is True
        assert replay["recomputed_total"] == inspection["provisional_total"]
        replay_obs = next(
            w for w in replay["windows"] if w["window_id"] == obs_window)
        assert replay_obs["recomputed_estimate"] == original_inspection_value

        # 8) 正式结果按新版本另行核对，不与旧临时值混用
        versions = service.list_formal_versions(BATCH)["versions"]
        assert versions and versions[-1]["version"] == confirmed["formal_version"]
        official = service.get_formal_result(BATCH, version=confirmed["formal_version"])
        assert all(cell["estimate_kind"] in ("正式统计", "无") for cell in official["cells"])
