import json
import re
import sys
from pathlib import Path


def norm_key(s: str) -> str:
    """归一化文档名/键，用于匹配：去除扩展名、空白、全角转半角。"""
    if s is None:
        return ""
    s = str(s).strip()
    s = re.sub(r"\.pdf$", "", s, flags=re.I)
    s = re.sub(r"\s+", "", s)
    return s


def norm_text(s) -> str:
    """归一化文本值：去空白、全角转半角、统一括号和连字符。"""
    if s is None:
        return ""
    s = str(s)
    # 全角转半角
    s = s.replace("（", "(").replace("）", ")")
    s = s.replace("：", ":").replace("，", ",").replace("。", ".")
    s = s.replace("　", "")
    # 连字符统一为半角 -
    s = s.replace("‑", "-").replace("–", "-").replace("—", "-").replace("－", "-")
    s = re.sub(r"\s+", "", s)
    return s


def _chinese_digits(s: str) -> str:
    """将中文数字转为阿拉伯数字（用于数值/期限类字段比较）。"""
    cn = {"零": "0", "一": "1", "二": "2", "两": "2", "三": "3",
          "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9"}
    return "".join(cn.get(c, c) for c in s)


def _is_null(v) -> bool:
    """判断一个值是否等价于空/null。"""
    if v is None:
        return True
    if isinstance(v, str) and v.strip().lower() in ("null", "none", "无法根据已有信息推断", ""):
        return True
    return False


def norm_value(v):
    """归一化字段值用于比较（支持标量/list/dict）。"""
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
    return _chinese_digits(norm_text(v))


def values_equal(a, b) -> bool:
    """比较归一化后的两个字段值是否一致（null 与 无法根据已有信息推断 视为一致）。"""
    if _is_null(a) and _is_null(b):
        return True
    if _is_null(a) or _is_null(b):
        return False
    return norm_value(a) == norm_value(b)


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
    """结果字段可能是 {value, 编辑距离, 置信度} 包装，取其中的 value；否则原样返回。"""
    if isinstance(v, dict) and "value" in v and "编辑距离" in v:
        return v["value"]
    return v


def _strip_space(v):
    """去除字符串中的全部空白（含全角空格），用于案号比较。"""
    if isinstance(v, str):
        return re.sub(r"\s+", "", v)
    return v


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

    field_stats = {}  # 字段名 -> [matched, total]
    doc_stats = {}    # 文档名 -> {matched, total, 各字段对比}

    for gk, gv in gt_map.items():
        rk = next((k for k in results_map if k == gk), None)
        if rk is None:
            doc_stats[gk] = {"matched": 0, "total": 0, "status": "未匹配到结果"}
            continue
        rv = results_map[rk]

        doc_matched = 0
        doc_total = 0
        field_results = {}
        for field, gt_val in gv.items():
            if field == "提取说明":
                continue
            doc_total += 1
            res_val = _unwrap_value(rv.get(field))
            # 案号比较前去除所有空格（gt 与结果可能带/不带空格）
            cmp_gt = _strip_space(gt_val) if field == "案号" else gt_val
            cmp_res = _strip_space(res_val) if field == "案号" else res_val
            ok = values_equal(cmp_gt, cmp_res)
            if ok:
                doc_matched += 1
            field_stats.setdefault(field, [0, 0])
            field_stats[field][1] += 1
            if ok:
                field_stats[field][0] += 1
            field_results[field] = {
                "匹配": ok,
                "gt": gt_val,
                "结果": res_val,
            }
        doc_stats[gk] = {
            "matched": doc_matched,
            "total": doc_total,
            "match_rate": round(doc_matched / doc_total, 4) if doc_total else None,
            "字段": field_results,
        }

    # 汇总
    total_matched = sum(v[0] for v in field_stats.values())
    total_fields = sum(v[1] for v in field_stats.values())
    field_rates = {
        f: {"matched": v[0], "total": v[1], "match_rate": round(v[0] / v[1], 4) if v[1] else None}
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
    }


def main(root_str: str):
    root = Path(root_str).resolve()
    assert root.is_dir(), f"不是有效文件夹: {root}"

    folders = sorted(p for p in root.iterdir() if p.is_dir())
    report = {"root": str(root), "folders": []}

    all_matched = 0
    all_total = 0
    for folder in folders:
        res = compare_folder(folder)
        report["folders"].append(res)
        if res.get("total_fields"):
            all_matched += res["total_matched"]
            all_total += res["total_fields"]

    report["overall_matched"] = all_matched
    report["overall_total"] = all_total
    report["overall_match_rate"] = round(all_matched / all_total, 4) if all_total else None

    out_path = root / "eval_report.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"评估完成，结果已保存: {out_path}")
    print(f"总匹配率: {report['overall_match_rate']} ({all_matched}/{all_total})")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"用法: python {sys.argv[0]} <testset根目录>")
        sys.exit(1)
    main(sys.argv[1])
