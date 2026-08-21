import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from provenance_utils import DATE_FIELDS, _date_digits_match, _provenance_norm


def norm_key(s: str) -> str:
    """归一化文档名/键，用于匹配：去除扩展名、空白、全角转半角。"""
    if s is None:
        return ""
    s = str(s).strip()
    s = re.sub(r"\.pdf$", "", s, flags=re.I)
    s = re.sub(r"\s+", "", s)
    return s


def _is_null(v) -> bool:
    """判断一个值是否等价于空/null。"""
    if v is None:
        return True
    if isinstance(v, str) and v.strip().lower() in ("null", "none", "无法根据已有信息推断", ""):
        return True
    return False


def norm_value(v):
    """归一化字段值用于比较（支持标量/list/dict）；文本先做溯源归一化
    （NFKC + 部首补充块映射 + 去空白 + 去标点，保留小数点 .），与溯源侧逻辑一致。"""
    if v is None:
        return None
    if isinstance(v, list):
        items = [norm_value(x) for x in v]
        items = [x for x in items if x is not None]
        return sorted(set(items), key=lambda x: str(x))
    if isinstance(v, dict):
        return {k: norm_value(val) for k, val in v.items()}
    if isinstance(v, (int, float)):
        return str(v)
    return _provenance_norm(v)


def values_equal(a, b, field=None) -> bool:
    """比较两个字段值：先按溯源逻辑归一化再比对；日期类字段额外支持年月日数字分组匹配
    （与溯源 _date_digits_match 一致，容忍中文/ISO/零填充格式差异）。
    null 与 无法根据已有信息推断 视为一致。"""
    if _is_null(a) and _is_null(b):
        return True
    if _is_null(a) or _is_null(b):
        return False
    if norm_value(a) == norm_value(b):
        return True
    if field in DATE_FIELDS and (_date_digits_match(a, b) or _date_digits_match(b, a)):
        return True
    return False


def load_json_lenient(path: Path) -> dict:
    """加载 JSON，容错修复 gt 常见手写错误（缺逗号、全角逗号、trailing comma、数值带单位）。"""
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    fixed = text
    # 全角逗号 -> 半角
    fixed = fixed.replace("，", ",")
    # 数值带单位：37969.89元 / 720,000 元 -> "37969.89元"
    fixed = re.sub(r'(["\s:])\s*(-?\d[\d,]*(?:\.\d+)?)\s*(元)\s*(?=[,\n}\]])',
                   lambda m: f'{m.group(1)}"{m.group(2)}{m.group(3)}"', fixed)
    # "值" 后换行接 "键": 或 } 缺逗号
    fixed = re.sub(r'(?<=["\d})\]])\s*\n\s*(?="[^"]+"\s*:)', ",\n", fixed)
    # 对象/数组内最后一个元素后的多余逗号
    fixed = re.sub(r",\s*([}\]])", r"\1", fixed)
    try:
        return json.loads(fixed)
    except json.JSONDecodeError as e:
        raise ValueError(f"无法解析 JSON: {path} ({e})")


def load_gt(path: Path) -> dict:
    """加载 gt 文件，处理文档缺失 key（裸对象）的情况。"""
    data = load_json_lenient(path)
    out = {}
    prev_key = None
    for k, v in data.items():
        if isinstance(v, dict) and "提取说明" in v:
            out[norm_key(k)] = v
            prev_key = norm_key(k)
        else:
            # 裸对象：附着到前一个 key
            raise ValueError(f"gt 文件存在裸对象（缺少文档 key）: {path}，键: {k!r}")
    return out


def _unwrap_value(v):
    """结果字段可能是 {value, 置信度} 包装，取其中的 value；否则原样返回。"""
    if isinstance(v, dict) and "value" in v and "置信度" in v:
        return v["value"]
    return v


def _unwrap_confidence(v):
    """结果字段 {value, 置信度} 中的置信度（0-100）；无则 None。"""
    if isinstance(v, dict) and "value" in v and "置信度" in v:
        c = v.get("置信度")
        return c if isinstance(c, (int, float)) else None
    return None


def compare_folder(folder: Path) -> dict:
    """对比一个文件夹的 results 与 gt，返回字段统计。"""
    results_files = sorted(folder.glob("*_results.json"))
    gt_files = sorted(folder.glob("*_gt.json"))
    folder_name = folder.name

    if not results_files:
        return {"folder": folder_name, "error": "缺少 *_results.json"}
    if not gt_files:
        return {"folder": folder_name, "error": "缺少 *_gt.json"}
    if len(results_files) > 1 or len(gt_files) > 1:
        return {"folder": folder_name, "error": "存在多个 *_results.json 或 *_gt.json"}

    results = load_json_lenient(results_files[0])
    gt = load_gt(gt_files[0])

    # 归一化匹配文档
    results_map = {norm_key(k): v for k, v in results.items()}
    gt_map = {norm_key(k): v for k, v in gt.items()}

    field_stats = {}  # 字段名 -> {"matched","total","conf_sum","conf_count"}
    doc_stats = {}    # 文档名 -> {matched, total, avg_置信度, 各字段对比}

    for gk, gv in gt_map.items():
        rk = next((k for k in results_map if k == gk), None)
        if rk is None:
            doc_stats[gk] = {"matched": 0, "total": 0, "status": "未匹配到结果"}
            continue
        rv = results_map[rk]

        doc_matched = 0
        doc_total = 0
        doc_conf_sum = 0.0
        doc_conf_count = 0
        field_results = {}
        for field, gt_val in gv.items():
            if field == "提取说明":
                continue
            doc_total += 1
            raw_res = rv.get(field)
            res_val = _unwrap_value(raw_res)
            conf = _unwrap_confidence(raw_res)
            # 先按溯源逻辑归一化再比对（案号等含空格/标点差异由归一化统一处理）
            ok = values_equal(gt_val, res_val, field=field)
            if ok:
                doc_matched += 1
            st = field_stats.setdefault(field, {"matched": 0, "total": 0, "conf_sum": 0.0, "conf_count": 0})
            st["total"] += 1
            if ok:
                st["matched"] += 1
            if conf is not None:
                st["conf_sum"] += conf
                st["conf_count"] += 1
                doc_conf_sum += conf
                doc_conf_count += 1
            field_results[field] = {
                "匹配": ok,
                "gt": gt_val,
                "结果": res_val,
                "置信度": conf,
            }
        doc_stats[gk] = {
            "matched": doc_matched,
            "total": doc_total,
            "match_rate": round(doc_matched / doc_total, 4) if doc_total else None,
            "avg_置信度": round(doc_conf_sum / doc_conf_count, 1) if doc_conf_count else None,
            "字段": field_results,
        }

    # 汇总
    total_matched = sum(v["matched"] for v in field_stats.values())
    total_fields = sum(v["total"] for v in field_stats.values())
    total_conf_sum = sum(v["conf_sum"] for v in field_stats.values())
    total_conf_count = sum(v["conf_count"] for v in field_stats.values())
    field_rates = {
        f: {
            "matched": v["matched"],
            "total": v["total"],
            "match_rate": round(v["matched"] / v["total"], 4) if v["total"] else None,
            "avg_置信度": round(v["conf_sum"] / v["conf_count"], 1) if v["conf_count"] else None,
        }
        for f, v in sorted(field_stats.items())
    }
    return {
        "folder": folder_name,
        "results_file": results_files[0].name,
        "gt_file": gt_files[0].name,
        "doc_count": len(gt_map),
        "doc_stats": doc_stats,
        "field_rates": field_rates,
        "total_matched": total_matched,
        "total_fields": total_fields,
        "total_match_rate": round(total_matched / total_fields, 4) if total_fields else None,
        "avg_置信度": round(total_conf_sum / total_conf_count, 1) if total_conf_count else None,
    }


def main(root_str: str):
    root = Path(root_str).resolve()
    assert root.is_dir(), f"不是有效文件夹: {root}"

    folders = sorted(p for p in root.iterdir() if p.is_dir())
    report = {"root": str(root), "folders": []}

    all_matched = 0
    all_total = 0
    all_conf_sum = 0.0
    all_conf_count = 0
    for folder in folders:
        res = compare_folder(folder)
        report["folders"].append(res)
        if res.get("total_fields"):
            all_matched += res["total_matched"]
            all_total += res["total_fields"]
            if res.get("avg_置信度") is not None:
                all_conf_sum += res["avg_置信度"] * res["doc_count"]
                all_conf_count += res["doc_count"]

    report["overall_matched"] = all_matched
    report["overall_total"] = all_total
    report["overall_match_rate"] = round(all_matched / all_total, 4) if all_total else None
    report["overall_avg_置信度"] = round(all_conf_sum / all_conf_count, 1) if all_conf_count else None

    out_path = root / "eval_report.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"评估完成，结果已保存: {out_path}")
    print(f"总匹配率: {report['overall_match_rate']} ({all_matched}/{all_total})")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"用法: python {sys.argv[0]} <testset根目录>")
        sys.exit(1)
    main(sys.argv[1])
