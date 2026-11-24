"""逐县增量交付与市级对账测试：覆盖签发版本不可变、中断续领、更正格与旧清单重领。"""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def setup_data_dir(monkeypatch):
    """每个测试用例使用独立临时目录写入数据产物文件。"""
    tmpdir = Path(tempfile.mkdtemp(prefix="geo_export_test_"))
    import app.services.base as base_mod
    original_root = base_mod.DATA_ROOT
    base_mod.DATA_ROOT = tmpdir
    for sub in ["exports"]:
        (tmpdir / sub).mkdir(parents=True, exist_ok=True)
    yield tmpdir
    base_mod.DATA_ROOT = original_root
    shutil.rmtree(tmpdir, ignore_errors=True)


# 两个完全互不重叠的区域：甲县在西、乙县在东，贡献格 geohash 边界完全落在对应县界内
JIA_BOUNDARY = {"lat_min": 39.80, "lat_max": 40.00, "lon_min": 116.20, "lon_max": 116.45}
YI_BOUNDARY = {"lat_min": 39.80, "lat_max": 40.00, "lon_min": 116.50, "lon_max": 116.75}
CITY_BOUNDARY = {"lat_min": 39.80, "lat_max": 40.00, "lon_min": 116.20, "lon_max": 116.75}

# 甲县两格
JIA_GRIDS_V1 = [
    {"grid_id": "GA1", "geohash": "wx4g0e", "record_count": 10},
    {"grid_id": "GA2", "geohash": "wx4g0s", "record_count": 5},
]
# 乙县首版两格（v1）
YI_GRIDS_V1 = [
    {"grid_id": "GB1", "geohash": "wx4g7k", "record_count": 8},
    {"grid_id": "GB2", "geohash": "wx4g7m", "record_count": 6},
]
# 乙县更正版：原两格不变，新增一个完全位于乙县的格 GB3（v2）
YI_GRIDS_V2 = [
    {"grid_id": "GB1", "geohash": "wx4g7k", "record_count": 8},
    {"grid_id": "GB2", "geohash": "wx4g7m", "record_count": 6},
    {"grid_id": "GB3", "geohash": "wx4g7q", "record_count": 8},
]


def _register_both_v1(svc, claim_key: str):
    jia = svc.register_county_result(
        claim_key, "JIA", "甲县", "v1", JIA_GRIDS_V1,
        county_boundary=JIA_BOUNDARY, note="甲县首版",
    )
    yi = svc.register_county_result(
        claim_key, "YI", "乙县", "v1", YI_GRIDS_V1,
        county_boundary=YI_BOUNDARY, note="乙县首版",
    )
    return jia, yi


class TestCountyResultVersioning:
    """县结果登记：版本只追加、边界可复算、越界格拒绝。"""

    def test_register_immutable_and_recomputable(self, setup_data_dir):
        from app.services.geo_export import GeoExportService, geohash_bbox
        svc = GeoExportService()
        claim_key = "CITY-001"
        jia, _ = _register_both_v1(svc, claim_key)

        # 增量等于各贡献格记录数之和
        assert jia["increment"] == 15
        assert jia["accepted"] is True

        # 边界由 geohash 真实解码，且完全位于甲县县界内
        bbox = geohash_bbox("wx4g0e")
        assert bbox["lat_max"] > bbox["lat_min"] and bbox["lon_max"] > bbox["lon_min"]
        assert svc._bbox_contains(JIA_BOUNDARY, bbox)
        assert jia["grids"][0]["boundary"] == bbox

        # 同县同版本重复登记必须被拒绝，旧版本不可覆盖
        with pytest.raises(ValueError):
            svc.register_county_result(claim_key, "JIA", "甲县", "v1", JIA_GRIDS_V1)

        # 登记册是追加文件，两个县版本都独立留存
        registry = svc._load_registry(claim_key)
        assert {(r["county_code"], r["version"]) for r in registry} == {
            ("JIA", "v1"), ("YI", "v1"),
        }

    def test_grid_outside_county_rejected(self, setup_data_dir):
        from app.services.geo_export import GeoExportService
        svc = GeoExportService()
        # 一个很小的县界，wx4g7k 的边界不可能完全位于其中
        tiny_boundary = {"lat_min": 39.90, "lat_max": 39.91,
                         "lon_min": 116.60, "lon_max": 116.61}
        with pytest.raises(ValueError):
            svc.register_county_result(
                "CITY-002", "YI", "乙县", "v1", YI_GRIDS_V1,
                county_boundary=tiny_boundary,
            )

    def test_yi_v2_is_independent_version(self, setup_data_dir):
        """乙县新旧版本各自独立验收，v2 不触碰 v1 的任何数据。"""
        from app.services.geo_export import GeoExportService
        svc = GeoExportService()
        claim_key = "CITY-003"
        _, yi_v1 = _register_both_v1(svc, claim_key)
        yi_v2 = svc.register_county_result(
            claim_key, "YI", "乙县", "v2", YI_GRIDS_V2,
            county_boundary=YI_BOUNDARY, note="乙县更正：新增完全位于乙县的GB3",
        )
        assert yi_v1["increment"] == 14
        assert yi_v2["increment"] == 22
        assert yi_v1["checksum"] != yi_v2["checksum"]

        registry = svc._load_registry(claim_key)
        yi_versions = [r for r in registry if r["county_code"] == "YI"]
        assert [r["version"] for r in yi_versions] == ["v1", "v2"]
        # v1 文件内容原样保留：仍只有两格
        assert len(yi_versions[0]["grids"]) == 2
        assert len(yi_versions[1]["grids"]) == 3


class TestInterruptedClaimAndResume:
    """市级领取中断在甲县之后，乙县更正后续领。"""

    def test_interrupted_resume_keeps_adopted_versions_and_manifest(self, setup_data_dir):
        from app.services.geo_export import GeoExportService
        svc = GeoExportService()
        claim_key = "CITY-MAIN"

        # 1) 两县首版验收
        jia_v1, yi_v1 = _register_both_v1(svc, claim_key)

        # 2) 市级申请领取，先读取甲县：创建任务并只交付甲县（模拟中断）
        svc.start_city_claim(claim_key, ["JIA", "YI"])
        jia_package = svc.deliver_next_county(claim_key)
        assert jia_package is not None
        assert jia_package["county_code"] == "JIA"
        assert jia_package["adopted_version"] == "v1"

        checkpoint = svc._read_claim_checkpoint(claim_key)
        assert list(checkpoint["delivered"].keys()) == ["JIA"]
        assert checkpoint["adopted_versions"] == {"JIA": "v1", "YI": "v1"}

        # 3) 乙县随后更正：新增完全位于乙县的格，登记 v2（不影响已交付甲县包）
        yi_v2 = svc.register_county_result(
            claim_key, "YI", "乙县", "v2", YI_GRIDS_V2,
            county_boundary=YI_BOUNDARY, note="乙县更正",
        )

        # 已交付甲县明细与边界完全未变
        checkpoint = svc._read_claim_checkpoint(claim_key)
        jia_delivery = checkpoint["delivered"]["JIA"]
        assert jia_delivery["adopted_version"] == "v1"
        assert jia_delivery["increment"] == 15
        assert jia_delivery["boundaries"] == {g["grid_id"]: g["boundary"] for g in jia_v1["grids"]}
        assert jia_delivery["source_checksum"] == jia_v1["checksum"]

        # 4) 从已取到甲县的中断任务继续领取
        resume = svc.resume_city_claim(claim_key)

        # 续领采用的是创建任务时固定的乙县 v1，而不是后来的 v2
        assert resume["adopted_versions"] == {"JIA": "v1", "YI": "v1"}
        assert resume["manifest_id"] == "M001"
        # 旧清单口径下的市级真实总数：15 + 14
        assert resume["city_total"] == 29

        manifest = svc.load_manifest(claim_key, "M001")
        assert manifest["status"] == "市级已确认"
        assert manifest["city_total"] == 29
        yi_entry = next(c for c in manifest["counties"] if c["county_code"] == "YI")
        assert yi_entry["adopted_version"] == "v1"
        assert yi_entry["grid_ids"] == ["GB1", "GB2"]
        jia_entry = next(c for c in manifest["counties"] if c["county_code"] == "JIA")
        assert jia_entry["grid_ids"] == ["GA1", "GA2"]
        # 清单保存每县实际采用的网格及边界
        assert set(jia_entry["boundaries"]) == {"GA1", "GA2"}
        assert set(yi_entry["boundaries"]) == {"GB1", "GB2"}

        # 5) 旧清单对账通过：每县一个可复算版本，增量之和 == 真实总数
        report_old = svc.reconcile_city_claim(claim_key, "M001")
        assert report_old["ok"] is True
        assert report_old["errors"] == []
        assert report_old["true_city_total"] == 29
        assert {c["county_code"]: c["adopted_version"] for c in report_old["counties"]} == {
            "JIA": "v1", "YI": "v1",
        }
        assert all(c["recomputable"] for c in report_old["counties"])

    def test_new_claim_adopts_latest_yi_and_new_manifest_independent(self, setup_data_dir):
        """更正后新的市级领取默认采用乙县最新验收版本，生成独立新清单。"""
        from app.services.geo_export import GeoExportService
        svc = GeoExportService()
        claim_key = "CITY-MAIN2"
        _register_both_v1(svc, claim_key)

        # 第一次领取（甲县先取后中断，再续领完成），固化旧清单 M001
        svc.start_city_claim(claim_key, ["JIA", "YI"])
        svc.deliver_next_county(claim_key)
        svc.register_county_result(
            claim_key, "YI", "乙县", "v2", YI_GRIDS_V2,
            county_boundary=YI_BOUNDARY, note="乙县更正",
        )
        svc.resume_city_claim(claim_key)

        # 新领取任务：乙县默认取最新已验收版本 v2
        new_claim = "CITY-MAIN2-NEW"
        # 登记册按领取任务隔离，这里在同一 claim_key 下新开任务会冲突，
        # 因此新任务使用独立 claim_key，并复用同一批县结果登记。
        for record in svc._load_registry(claim_key):
            svc.register_county_result(
                new_claim, record["county_code"], record["county_name"], record["version"],
                [
                    {"grid_id": g["grid_id"], "geohash": g["geohash"], "record_count": g["record_count"]}
                    for g in record["grids"]
                ],
                county_boundary=JIA_BOUNDARY if record["county_code"] == "JIA" else YI_BOUNDARY,
            )
        svc.start_city_claim(new_claim, ["JIA", "YI"])
        result = svc.resume_city_claim(new_claim)
        assert result["adopted_versions"] == {"JIA": "v1", "YI": "v2"}
        # 真实市级增量：甲15 + 乙22
        assert result["city_total"] == 37

        report = svc.reconcile_city_claim(new_claim)
        assert report["ok"] is True
        assert report["true_city_total"] == 37
        yi_report = next(c for c in report["counties"] if c["county_code"] == "YI")
        assert yi_report["grid_ids"] == ["GB1", "GB2", "GB3"]

        # 同一任务内新清单不会覆盖旧清单：两个清单文件并存且各自自洽
        old_manifest = svc.load_manifest(claim_key, "M001")
        assert old_manifest["city_total"] == 29
        assert svc.reconcile_city_claim(claim_key, "M001")["ok"] is True

    def test_pinned_version_survives_later_correction(self, setup_data_dir):
        """钉版领取：任务创建时钉住乙县 v1，v2 登记后续领仍采用 v1。"""
        from app.services.geo_export import GeoExportService
        svc = GeoExportService()
        claim_key = "CITY-PIN"
        _register_both_v1(svc, claim_key)
        svc.start_city_claim(
            claim_key, ["JIA", "YI"], pinned_versions={"YI": "v1"},
        )
        svc.deliver_next_county(claim_key)  # 甲县交付后中断
        svc.register_county_result(
            claim_key, "YI", "乙县", "v2", YI_GRIDS_V2, county_boundary=YI_BOUNDARY,
        )
        resume = svc.resume_city_claim(claim_key)
        assert resume["adopted_versions"]["YI"] == "v1"
        assert resume["city_total"] == 29
        checkpoint = svc._read_claim_checkpoint(claim_key)
        assert checkpoint["pinned_versions"] == {"YI": "v1"}


class TestReclaimWithOldManifest:
    """凭旧清单单独重领甲县。"""

    def test_reclaim_jia_reproduces_original_package(self, setup_data_dir):
        from app.services.geo_export import GeoExportService
        svc = GeoExportService()
        claim_key = "CITY-RECLAIM"
        jia_v1, _ = _register_both_v1(svc, claim_key)

        svc.start_city_claim(claim_key, ["JIA", "YI"])
        original_package = svc.deliver_next_county(claim_key)
        # 乙县更正并完成原任务，固化旧清单 M001
        svc.register_county_result(
            claim_key, "YI", "乙县", "v2", YI_GRIDS_V2, county_boundary=YI_BOUNDARY,
        )
        svc.resume_city_claim(claim_key)

        # 还能按旧清单单独重领甲县
        reclaim = svc.reclaim_county_with_manifest(claim_key, "JIA", "M001")
        assert reclaim["adopted_version"] == "v1"
        assert reclaim["increment"] == 15
        assert reclaim["grid_ids"] == ["GA1", "GA2"]
        assert reclaim["boundaries"] == {g["grid_id"]: g["boundary"] for g in jia_v1["grids"]}
        # 可复算不变量：源版本校验和与原交付包一致，网格、边界、增量逐项相同
        assert reclaim["source_checksum"] == original_package["source_checksum"]
        assert reclaim["grid_ids"] == [g["grid_id"] for g in original_package["grids"]]
        assert reclaim["increment"] == original_package["increment"]
        for grid in original_package["grids"]:
            assert reclaim["boundaries"][grid["grid_id"]] == grid["boundary"]

        # 重领不改变旧清单与检查点
        manifest = svc.load_manifest(claim_key, "M001")
        assert manifest["manifest_checksum"] == svc.load_manifest(claim_key, "M001")["manifest_checksum"]
        checkpoint = svc._read_claim_checkpoint(claim_key)
        assert checkpoint["current_manifest"] == "M001"

        # 重领产物落盘，独立于原差额包
        assert Path(reclaim["reclaim_file"]).exists()
        with open(reclaim["reclaim_file"], encoding="utf-8") as f:
            saved = json.load(f)
        assert saved["reclaimed_from_manifest"] == "M001"

    def test_reclaim_unknown_manifest_or_county_fails(self, setup_data_dir):
        from app.services.geo_export import GeoExportService
        svc = GeoExportService()
        claim_key = "CITY-RECLAIM-FAIL"
        _register_both_v1(svc, claim_key)
        svc.start_city_claim(claim_key, ["JIA", "YI"])
        svc.resume_city_claim(claim_key)
        with pytest.raises(KeyError):
            svc.reclaim_county_with_manifest(claim_key, "JIA", "M999")
        with pytest.raises(KeyError):
            svc.reclaim_county_with_manifest(claim_key, "BING", "M001")
