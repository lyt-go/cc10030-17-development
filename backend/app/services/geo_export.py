"""结果导出领域服务：将空间分析结果按区域和字段范围进行分片导出，生成导出任务数据集。

除批次分片导出外，本服务还承载市级跨县领取场景：
- 各县结果以「签发版本」登记，版本只追加、不改写，新旧版本各自独立验收并保留校验和；
- 市级领取按县顺序推进，按贡献格交付逐县增量包，交付清单记录每县实际采用的
  网格、边界与签发版本，每完成一县即落检查点，支持中断续领；
- 市级对账保证每县只采用一个可复算版本，各县采用增量之和等于真实总数；
- 旧交付清单保持不可变，可凭旧清单单独重领任一县且复算结果一致。
"""
from __future__ import annotations

import json
import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# geohash Base32 字母表，与基类 _geohash_encode 保持一致
_GEOHASH_ALPHABET = "0123456789bcdefghjkmnpqrstuvwxyz"


def geohash_bbox(geohash: str) -> dict[str, float]:
    """解码 geohash，返回网格的真实经纬度边界 {lat_min, lat_max, lon_min, lon_max}。"""
    lat_min, lat_max = -90.0, 90.0
    lon_min, lon_max = -180.0, 180.0
    is_even = True
    for ch in geohash:
        idx = _GEOHASH_ALPHABET.index(ch)
        for bit in range(4, -1, -1):
            bit_value = (idx >> bit) & 1
            if is_even:
                mid = (lon_min + lon_max) / 2
                if bit_value:
                    lon_min = mid
                else:
                    lon_max = mid
            else:
                mid = (lat_min + lat_max) / 2
                if bit_value:
                    lat_min = mid
                else:
                    lat_max = mid
            is_even = not is_even
    return {
        "lat_min": lat_min,
        "lat_max": lat_max,
        "lon_min": lon_min,
        "lon_max": lon_max,
    }


class GeoExportService(BaseModuleService):
    MODULE = "geo_export"
    ENTITY = "导出任务"
    OPERATION_TERM = "导出"
    LIST_FIELDS = ["导出编号", "快照编号", "导出格式", "区域范围", "字段范围", "记录条数", "分片大小", "导出水位"]
    REQUIRED_FIELDS = ["导出编号", "快照编号", "导出格式"]
    STATUS_ORDER = ["草稿", "待导出", "导出中", "已交付"]
    ACTION_RULES = {"启动导出": "导出中", "确认交付": "已交付", "标记失败": "已交付"}
    NEGATIVE_ACTIONS = []

    def export_batch_results(self, batch_key: str, export_format: str = "jsonl",
                              shard_size: int = 100) -> dict[str, Any]:
        """读取分析快照数据集，按指定格式将热力、OD矩阵和区域数据分片导出，生成导出结果文件。"""
        results = []
        total_records = 0

        # 导出热力数据
        heatmap_file = self._data_dir("heatmap") / f"{batch_key}_heatmap.jsonl"
        if heatmap_file.exists():
            heatmap_data = self._read_jsonl(heatmap_file)
            results.extend(heatmap_data)
            total_records += len(heatmap_data)

        # 导出OD矩阵数据
        od_file = self._data_dir("od_matrix") / f"{batch_key}_od.jsonl"
        if od_file.exists():
            od_data = self._read_jsonl(od_file)
            results.extend(od_data)
            total_records += len(od_data)

        # 导出区域聚合数据
        region_file = self._data_dir("region_aggregates") / f"{batch_key}_region.jsonl"
        if region_file.exists():
            region_data = self._read_jsonl(region_file)
            results.extend(region_data)
            total_records += len(region_data)

        if not results:
            return {"batch_key": batch_key, "exported": 0, "total": 0}

        # 分片导出
        shards = []
        for i in range(0, len(results), shard_size):
            shard = results[i:i + shard_size]
            shard_file = self._data_dir("exports") / f"{batch_key}_shard_{i // shard_size + 1:03d}.{export_format}"
            self._write_jsonl(shard_file, shard)
            shards.append(str(shard_file))

        # 写入导出汇总数据集
        export_summary = {
            "batch_key": batch_key,
            "export_format": export_format,
            "total_records": total_records,
            "shards": len(shards),
            "shard_size": shard_size,
            "exported_at": time.time(),
        }
        summary_file = self._data_dir("exports") / f"{batch_key}_export_summary.jsonl"
        self._write_jsonl(summary_file, [export_summary])

        # 创建导出记录
        entry, _ = self.create_entry({
            "导出编号": f"EXPORT-{batch_key}",
            "快照编号": f"SNAP-{batch_key}",
            "导出格式": export_format,
            "区域范围": "all",
            "字段范围": "all",
            "记录条数": str(total_records),
            "分片大小": str(shard_size),
            "导出水位": f"已导出{total_records}条/{len(shards)}分片",
        })
        if entry:
            self.run_action(entry["id"], "确认交付")

        return {
            "batch_key": batch_key,
            "exported_records": total_records,
            "shards": len(shards),
            "format": export_format,
            "shard_files": shards,
        }

    def get_export_summary(self, batch_key: str) -> dict[str, Any]:
        """读取批次的导出任务汇总数据集。"""
        file_path = self._data_dir("exports") / f"{batch_key}_export_summary.jsonl"
        summaries = self._read_jsonl(file_path)
        return summaries[0] if summaries else {}

    # ------------------------------------------------------------------
    # 逐县增量交付、中断续领与市级对账
    # ------------------------------------------------------------------

    def _claim_dir(self, claim_key: str):
        """每个市级领取任务使用独立目录，登记、检查点、清单、差额包互不串扰。"""
        path = self._data_dir("exports") / "county_claims" / claim_key
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _registry_file(self, claim_key: str):
        return self._claim_dir(claim_key) / "county_results.jsonl"

    def _checkpoint_file(self, claim_key: str):
        return self._claim_dir(claim_key) / "claim_checkpoint.json"

    def _package_file(self, claim_key: str, county_code: str, version: str):
        safe_version = version.replace("/", "_")
        return self._claim_dir(claim_key) / f"package_{county_code}_{safe_version}.json"

    @staticmethod
    def _bbox_contains(outer: dict[str, float], inner: dict[str, float]) -> bool:
        """判断 inner 边界是否完全位于 outer 边界之内（含贴边）。"""
        return (
            outer["lat_min"] <= inner["lat_min"]
            and inner["lat_max"] <= outer["lat_max"]
            and outer["lon_min"] <= inner["lon_min"]
            and inner["lon_max"] <= outer["lon_max"]
        )

    def register_county_result(
        self,
        claim_key: str,
        county_code: str,
        county_name: str,
        version: str,
        grids: list[dict[str, Any]],
        *,
        county_boundary: dict[str, float] | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """登记一个县的签发结果版本。

        版本记录只追加、不改写：同一县同一版本重复登记直接报错，已用于交付的
        旧版本不会因新版本登记而变化。每个贡献格的边界由 geohash 统一解码，
        保证后续可独立复算；提供 county_boundary 时校验所有格完全位于县界内。

        grids: [{"grid_id": "G1", "geohash": "wx4g0", "record_count": 12}, ...]
        """
        if not grids:
            raise ValueError(f"{county_name} 版本 {version} 至少需要一个贡献格")

        registry = self._read_jsonl(self._registry_file(claim_key))
        if any(r["county_code"] == county_code and r["version"] == version for r in registry):
            raise ValueError(f"{county_name} 的版本 {version} 已登记，签发版本不可覆盖改写")

        grid_ids = [g["grid_id"] for g in grids]
        if len(grid_ids) != len(set(grid_ids)):
            raise ValueError(f"{county_name} 版本 {version} 存在重复网格编号")

        normalized_grids: list[dict[str, Any]] = []
        for grid in grids:
            geohash = grid["geohash"]
            boundary = geohash_bbox(geohash)
            if county_boundary is not None and not self._bbox_contains(county_boundary, boundary):
                raise ValueError(
                    f"网格 {grid['grid_id']}({geohash}) 不完全位于 {county_name} 县界内，不能计入该县贡献格"
                )
            record_count = int(grid["record_count"])
            if record_count < 0:
                raise ValueError(f"网格 {grid['grid_id']} 记录数不能为负")
            normalized_grids.append({
                "grid_id": grid["grid_id"],
                "geohash": geohash,
                "boundary": boundary,
                "record_count": record_count,
            })

        normalized_grids.sort(key=lambda g: g["grid_id"])
        increment = sum(g["record_count"] for g in normalized_grids)
        payload = {
            "claim_key": claim_key,
            "county_code": county_code,
            "county_name": county_name,
            "version": version,
            "grids": normalized_grids,
            "increment": increment,
            "note": note,
            "registered_at": time.time(),
            "accepted": True,
        }
        payload["checksum"] = self._compute_checksum(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        )

        # 追加写：历史版本原样保留
        with open(self._registry_file(claim_key), "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        return payload

    def _load_registry(self, claim_key: str) -> list[dict[str, Any]]:
        return self._read_jsonl(self._registry_file(claim_key))

    def _find_county_version(self, claim_key: str, county_code: str, version: str) -> dict[str, Any]:
        for record in self._load_registry(claim_key):
            if record["county_code"] == county_code and record["version"] == version:
                return record
        raise KeyError(f"县 {county_code} 的版本 {version} 不存在或已归档")

    def _latest_accepted_version(self, claim_key: str, county_code: str) -> str:
        records = [
            r for r in self._load_registry(claim_key)
            if r["county_code"] == county_code and r.get("accepted")
        ]
        if not records:
            raise KeyError(f"县 {county_code} 尚无已验收签发版本")
        # 登记顺序即签发顺序，最后一个为最新验收版本
        return records[-1]["version"]

    def start_city_claim(
        self,
        claim_key: str,
        county_sequence: list[str],
        *,
        pinned_versions: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """创建市级领取任务并固定领取顺序与每县采用版本。

        钉版（pinned_versions）在任务创建时写入检查点，续领过程中不得更改；
        未钉版的县默认采用最新已验收版本。此处只做登记，逐县交付由
        deliver_next_county 推进，天然支持中途中断。
        """
        if not county_sequence:
            raise ValueError("领取县序列不能为空")
        if len(county_sequence) != len(set(county_sequence)):
            raise ValueError("领取县序列中存在重复县")
        if self._checkpoint_file(claim_key).exists():
            raise ValueError(f"领取任务 {claim_key} 已存在，请走续领接口")

        pinned = dict(pinned_versions or {})
        # 预解析版本，提前暴露「版本不存在」问题
        resolved: dict[str, str] = {}
        for county_code in county_sequence:
            if county_code in pinned:
                self._find_county_version(claim_key, county_code, pinned[county_code])
                resolved[county_code] = pinned[county_code]
            else:
                resolved[county_code] = self._latest_accepted_version(claim_key, county_code)

        checkpoint = {
            "claim_key": claim_key,
            "sequence": list(county_sequence),
            "pinned_versions": pinned,
            "adopted_versions": resolved,
            "delivered": {},
            "status": "进行中",
            "current_manifest": None,
            "started_at": time.time(),
        }
        self._write_claim_checkpoint(claim_key, checkpoint)
        return checkpoint

    def _read_claim_checkpoint(self, claim_key: str) -> dict[str, Any]:
        checkpoint_file = self._checkpoint_file(claim_key)
        if not checkpoint_file.exists():
            raise KeyError(f"领取任务 {claim_key} 不存在")
        with open(checkpoint_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write_claim_checkpoint(self, claim_key: str, checkpoint: dict[str, Any]) -> None:
        checkpoint["_updated_at"] = time.time()
        with open(self._checkpoint_file(claim_key), "w", encoding="utf-8") as f:
            json.dump(checkpoint, f, ensure_ascii=False, indent=2, default=str)

    def deliver_next_county(self, claim_key: str) -> dict[str, Any] | None:
        """交付领取序列中下一个未交付县的逐县增量包，并推进检查点。

        已交付县的包不会被再次生成或改写——这是「不把已交付的甲县明细改成
        乙县版本」的硬保证；每次只处理序列里第一个待领取县。
        """
        checkpoint = self._read_claim_checkpoint(claim_key)
        if checkpoint["status"] == "已完成":
            return None

        next_county = next(
            (code for code in checkpoint["sequence"] if code not in checkpoint["delivered"]),
            None,
        )
        if next_county is None:
            return None

        version = checkpoint["adopted_versions"][next_county]
        record = self._find_county_version(claim_key, next_county, version)

        package = {
            "claim_key": claim_key,
            "county_code": record["county_code"],
            "county_name": record["county_name"],
            "adopted_version": record["version"],
            "grids": record["grids"],
            "increment": record["increment"],
            "source_checksum": record["checksum"],
            "delivered_at": time.time(),
        }
        package["package_checksum"] = self._compute_checksum(
            json.dumps(package, sort_keys=True, ensure_ascii=False, default=str)
        )
        package_file = self._package_file(claim_key, record["county_code"], record["version"])
        with open(package_file, "w", encoding="utf-8") as f:
            json.dump(package, f, ensure_ascii=False, indent=2, default=str)

        checkpoint["delivered"][next_county] = {
            "county_name": record["county_name"],
            "adopted_version": record["version"],
            "grid_ids": [g["grid_id"] for g in record["grids"]],
            "boundaries": {g["grid_id"]: g["boundary"] for g in record["grids"]},
            "increment": record["increment"],
            "source_checksum": record["checksum"],
            "package_checksum": package["package_checksum"],
            "package_file": str(package_file),
            "delivered_at": package["delivered_at"],
        }
        self._write_claim_checkpoint(claim_key, checkpoint)
        return package

    def resume_city_claim(self, claim_key: str) -> dict[str, Any]:
        """从检查点续领：跳过已交付县，依次交付剩余县，全部完成后固化交付清单。"""
        checkpoint = self._read_claim_checkpoint(claim_key)
        delivered_before = set(checkpoint["delivered"])

        while True:
            package = self.deliver_next_county(claim_key)
            if package is None:
                break

        checkpoint = self._read_claim_checkpoint(claim_key)
        pending = [code for code in checkpoint["sequence"] if code not in checkpoint["delivered"]]
        if pending:
            raise RuntimeError(f"领取任务 {claim_key} 仍有待领取县：{pending}")

        manifest = self._finalize_manifest(claim_key, checkpoint)
        return {
            "claim_key": claim_key,
            "resumed_from": sorted(delivered_before),
            "manifest_id": manifest["manifest_id"],
            "adopted_versions": manifest["adopted_versions"],
            "city_total": manifest["city_total"],
        }

    def _finalize_manifest(self, claim_key: str, checkpoint: dict[str, Any]) -> dict[str, Any]:
        """将本次确认结果固化为不可变交付清单，历史清单原样保留。"""
        existing = sorted(self._claim_dir(claim_key).glob("manifest_*.json"))
        manifest_id = f"M{len(existing) + 1:03d}"

        counties = []
        for county_code in checkpoint["sequence"]:
            delivery = checkpoint["delivered"][county_code]
            counties.append({
                "county_code": county_code,
                "county_name": delivery["county_name"],
                "adopted_version": delivery["adopted_version"],
                "grid_ids": delivery["grid_ids"],
                "boundaries": delivery["boundaries"],
                "increment": delivery["increment"],
                "source_checksum": delivery["source_checksum"],
                "package_checksum": delivery["package_checksum"],
                "package_file": delivery["package_file"],
            })

        manifest = {
            "manifest_id": manifest_id,
            "claim_key": claim_key,
            "status": "市级已确认",
            "counties": counties,
            "adopted_versions": {c["county_code"]: c["adopted_version"] for c in counties},
            "city_total": sum(c["increment"] for c in counties),
            "created_at": time.time(),
        }
        manifest["manifest_checksum"] = self._compute_checksum(
            json.dumps(manifest, sort_keys=True, ensure_ascii=False, default=str)
        )
        manifest_file = self._claim_dir(claim_key) / f"manifest_{manifest_id}.json"
        with open(manifest_file, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2, default=str)

        checkpoint["status"] = "已完成"
        checkpoint["current_manifest"] = manifest_id
        self._write_claim_checkpoint(claim_key, checkpoint)
        return manifest

    def load_manifest(self, claim_key: str, manifest_id: str) -> dict[str, Any]:
        """读取指定交付清单（旧清单不会被新清单覆盖）。"""
        manifest_file = self._claim_dir(claim_key) / f"manifest_{manifest_id}.json"
        if not manifest_file.exists():
            raise KeyError(f"交付清单 {manifest_id} 不存在")
        with open(manifest_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def reconcile_city_claim(self, claim_key: str, manifest_id: str | None = None) -> dict[str, Any]:
        """市级对账：每县恰好一个可复算版本，采用增量之和必须等于真实总数。

        对账不直接信任清单里存的增量，而是回到县结果登记册重新取版本、
        重算校验和与增量，确保清单数字可独立复算。
        """
        checkpoint = self._read_claim_checkpoint(claim_key)
        manifest_id = manifest_id or checkpoint["current_manifest"]
        if not manifest_id:
            raise KeyError("该领取任务尚未生成交付清单，无法对账")
        manifest = self.load_manifest(claim_key, manifest_id)

        county_reports = []
        recomputed_total = 0
        ok = True
        errors: list[str] = []

        sequence = checkpoint["sequence"]
        if len(manifest["counties"]) != len(sequence):
            ok = False
            errors.append("清单县数与领取序列不一致")

        for county_code in sequence:
            entries = [c for c in manifest["counties"] if c["county_code"] == county_code]
            if len(entries) != 1:
                ok = False
                errors.append(f"县 {county_code} 在清单中采用版本数为 {len(entries)}，要求恰好 1 个")
                continue
            entry = entries[0]
            report: dict[str, Any] = {
                "county_code": county_code,
                "adopted_version": entry["adopted_version"],
                "manifest_increment": entry["increment"],
            }
            try:
                record = self._find_county_version(claim_key, county_code, entry["adopted_version"])
            except KeyError as exc:
                ok = False
                report["recomputable"] = False
                report["error"] = str(exc)
                errors.append(str(exc))
                county_reports.append(report)
                continue

            # 重算版本校验和：剔除登记册自带 checksum 后按同一规范摘要
            record_payload = {k: v for k, v in record.items() if k != "checksum"}
            recomputed_checksum = self._compute_checksum(
                json.dumps(record_payload, sort_keys=True, ensure_ascii=False, default=str)
            )
            checksum_ok = recomputed_checksum == record["checksum"] == entry["source_checksum"]
            recomputed_increment = sum(g["record_count"] for g in record["grids"])
            increment_ok = recomputed_increment == entry["increment"]

            report.update({
                "recomputable": checksum_ok,
                "recomputed_increment": recomputed_increment,
                "grid_count": len(record["grids"]),
                "grid_ids": [g["grid_id"] for g in record["grids"]],
            })
            recomputed_total += recomputed_increment

            if not checksum_ok:
                ok = False
                errors.append(f"县 {county_code} 版本 {entry['adopted_version']} 校验和不一致，结果不可复算")
            if not increment_ok:
                ok = False
                errors.append(
                    f"县 {county_code} 清单增量 {entry['increment']} 与复算增量 {recomputed_increment} 不符"
                )
            county_reports.append(report)

        manifest_total = manifest["city_total"]
        total_ok = manifest_total == recomputed_total
        if not total_ok:
            ok = False
            errors.append(f"采用增量之和 {manifest_total} 不等于真实总数 {recomputed_total}")

        return {
            "claim_key": claim_key,
            "manifest_id": manifest_id,
            "ok": ok,
            "errors": errors,
            "true_city_total": recomputed_total,
            "manifest_city_total": manifest_total,
            "counties": county_reports,
        }

    def reclaim_county_with_manifest(
        self,
        claim_key: str,
        county_code: str,
        manifest_id: str,
    ) -> dict[str, Any]:
        """凭旧交付清单单独重领某县。

        旧清单保持不可变：重领只按清单钉住的版本从登记册重新取数复算，
        不影响其它县已交付包。版本缺失或被篡改（校验和不符）时拒绝重领。
        """
        manifest = self.load_manifest(claim_key, manifest_id)
        entries = [c for c in manifest["counties"] if c["county_code"] == county_code]
        if len(entries) != 1:
            raise KeyError(f"旧清单 {manifest_id} 中县 {county_code} 记录不唯一，无法重领")
        entry = entries[0]

        record = self._find_county_version(claim_key, county_code, entry["adopted_version"])
        record_payload = {k: v for k, v in record.items() if k != "checksum"}
        recomputed_checksum = self._compute_checksum(
            json.dumps(record_payload, sort_keys=True, ensure_ascii=False, default=str)
        )
        if recomputed_checksum != record["checksum"]:
            raise ValueError(f"县 {county_code} 版本 {entry['adopted_version']} 已被篡改，拒绝按旧清单重领")
        if recomputed_checksum != entry["source_checksum"]:
            raise ValueError(f"旧清单 {manifest_id} 记录的 {county_code} 校验和与登记册不符，拒绝重领")

        package = {
            "claim_key": claim_key,
            "county_code": record["county_code"],
            "county_name": record["county_name"],
            "adopted_version": record["version"],
            "grids": record["grids"],
            "increment": record["increment"],
            "source_checksum": record["checksum"],
            "reclaimed_from_manifest": manifest_id,
            "delivered_at": time.time(),
        }
        package["package_checksum"] = self._compute_checksum(
            json.dumps(package, sort_keys=True, ensure_ascii=False, default=str)
        )
        reclaim_file = (
            self._claim_dir(claim_key)
            / f"reclaim_{county_code}_{manifest_id}_{record['version'].replace('/', '_')}.json"
        )
        with open(reclaim_file, "w", encoding="utf-8") as f:
            json.dump(package, f, ensure_ascii=False, indent=2, default=str)

        return {
            "claim_key": claim_key,
            "manifest_id": manifest_id,
            "county_code": county_code,
            "adopted_version": record["version"],
            "increment": record["increment"],
            "grid_ids": [g["grid_id"] for g in record["grids"]],
            "boundaries": {g["grid_id"]: g["boundary"] for g in record["grids"]},
            "source_checksum": record["checksum"],
            "package_checksum": package["package_checksum"],
            "reclaim_file": str(reclaim_file),
        }
