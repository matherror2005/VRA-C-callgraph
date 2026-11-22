"""VRA v1 整图转换、契约校验和 JSON 文件写入；不改变目标匹配规则。"""

from collections import Counter, defaultdict
from copy import deepcopy
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tempfile


SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "call-graph-v1.schema.json"


class GraphValidationError(ValueError):
    pass


def _require(condition, message):
    if not condition:
        raise GraphValidationError(message)


@lru_cache(maxsize=1)
def _schema():
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _shape(rule, value, path="$", schema=None):
    """仅解释本 VRA 契约使用的关键字；不是通用 JSON Schema 引擎。"""
    schema = _schema() if schema is None else schema
    if "$ref" in rule:
        _shape(schema["$defs"][rule["$ref"].split("/")[-1]], value, path, schema)
    if "const" in rule:
        _require(type(value) is type(rule["const"]) and value == rule["const"], f"{path}: 常量不匹配")
    if "enum" in rule:
        _require(any(type(value) is type(item) and value == item for item in rule["enum"]), f"{path}: 枚举值无效")
    if "type" in rule:
        types = rule["type"] if isinstance(rule["type"], list) else [rule["type"]]
        checks = {"object": isinstance(value, dict), "array": isinstance(value, list),
                  "string": isinstance(value, str), "integer": type(value) is int,
                  "boolean": type(value) is bool, "null": value is None}
        _require(any(checks[t] for t in types), f"{path}: 类型应为 {types}")
    if isinstance(value, dict):
        _require(all(key in value for key in rule.get("required", [])), f"{path}: 缺少必填字段")
        if rule.get("additionalProperties") is False:
            _require(set(value) <= set(rule.get("properties", {})), f"{path}: 含未定义字段")
        for key, child in rule.get("properties", {}).items():
            if key in value:
                _shape(child, value[key], f"{path}.{key}", schema)
    if isinstance(value, list):
        _require(len(value) >= rule.get("minItems", 0), f"{path}: 数组过短")
        _require(len(value) <= rule.get("maxItems", float("inf")), f"{path}: 数组过长")
        if rule.get("uniqueItems"):
            _require(len(value) == len({json.dumps(item, sort_keys=True) for item in value}), f"{path}: 数组含重复值")
        for i, item in enumerate(value):
            _shape(rule.get("items", {}), item, f"{path}[{i}]", schema)
    if isinstance(value, str):
        _require(len(value) >= rule.get("minLength", 0), f"{path}: 字符串过短")
    if type(value) is int:
        _require(value >= rule.get("minimum", float("-inf")), f"{path}: 数值过小")
    if "anyOf" in rule:
        for option in rule["anyOf"]:
            try:
                _shape(option, value, path, schema)
                break
            except GraphValidationError:
                continue
        else:
            raise GraphValidationError(f"{path}: 不符合任何允许的结构")
    for item in rule.get("allOf", []):
        _shape(item, value, path, schema)
    if "if" in rule:
        try:
            _shape(rule["if"], value, path, schema)
            branch = "then"
        except GraphValidationError:
            branch = "else"
        _shape(rule.get(branch, {}), value, path, schema)


def _relative(value, label):
    path = PurePosixPath(value)
    _require(not path.is_absolute() and ".." not in path.parts and "\\" not in value,
             f"{label}: 必须使用项目内的相对 POSIX 路径")


def validate_call_graph(graph):
    """校验 v1 字段形状及跨记录约束；成功返回 None，失败抛出明确异常。"""
    try:
        json.dumps(graph, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise GraphValidationError(f"结果不是有效 JSON 数据：{error}") from error
    _shape(_schema(), graph)
    indexed = {}
    for collection in ("nodes", "edges", "call_sites"):
        indexed[collection] = {item["id"]: item for item in graph[collection]}
        _require(len(indexed[collection]) == len(graph[collection]), f"{collection}: ID 重复")
    nodes, sites = indexed["nodes"], indexed["call_sites"]
    for node in nodes.values():
        _require(node["language"] in graph["metadata"]["languages"], "节点语言未在 metadata 声明")
        _require(node["build_configuration_id"] == graph["metadata"]["build_configuration_id"], "图中混入其他构建配置")
        _relative(node["location"]["file"], "定义位置")
        if node["translation_unit"] is not None:
            _relative(node["translation_unit"], "编译单元")
    for site in sites.values():
        _require(site["caller_id"] in nodes, "调用点引用了不存在的 caller")
        _require(all(fid in nodes for fid in site["candidate_ids"]), "候选 ID 引用了不存在的节点")
        _relative(site["location"]["file"], "调用位置")
        if site["resolution"] == "matched":
            _require(site["callee_id"] in nodes, "matched 调用点缺少有效目标")
            _require(site["candidate_ids"] == [site["callee_id"]], "matched 调用点的唯一候选与目标不一致")
        if site["resolution"] == "ambiguous":
            _require(len(site["candidate_ids"]) > 1, "ambiguous 调用点至少需要两个候选")
    relations = set()
    membership = Counter()
    for edge in graph["edges"]:
        source, target = edge["source"], edge["target"]
        _require(source in nodes and target in nodes, "边端点引用了不存在的节点")
        pair = (source, target, edge["kind"])
        _require(pair not in relations, "同一函数对存在重复关系边")
        relations.add(pair)
        _require(edge["is_cross_repo"] == (nodes[source]["repo"] != nodes[target]["repo"]), "边的跨 Repo 标记不正确")
        for site_id in edge["call_site_ids"]:
            _require(site_id in sites, "边引用了不存在的调用点")
            site = sites[site_id]
            _require(site["resolution"] == "matched", "未匹配调用点不能进入边")
            _require((site["caller_id"], site["callee_id"]) == (source, target), "调用点与边端点不一致")
            membership[site_id] += 1
    for site in sites.values():
        expected = 1 if site["resolution"] == "matched" else 0
        _require(membership[site["id"]] == expected, "调用点的关系边归属不正确")


def _location(record):
    return {"file": record["relative_file_path"], "line": record["line_number"], "column": record["column"]}


def export_call_graph(analyzer, *, project_id, revision=None):
    """从现有分析器转换整张图，不修改分析器，不调用新的目标推断。"""
    nodes = []
    for fid, func in sorted(analyzer.functions_by_id.items()):
        attrs = {key: deepcopy(func[key]) for key in ("return_type", "parameters", "function_type", "linkage_name") if key in func}
        nodes.append({"id": fid, "kind": "function", "name": func["name"], "language": "c", "repo": func["repo"],
                      "location": _location(func), "translation_unit": func.get("translation_unit", func["relative_file_path"]),
                      "is_static": func["is_static"], "build_target": func.get("build_target"),
                      "build_configuration_id": func.get("build_configuration_id"), "attributes": attrs})
    sites, pairs = [], defaultdict(list)
    native_ids = Counter(call["call_id"] for call in analyzer.call_sites)
    used_ids, occurrences = set(), Counter()
    for call in sorted(analyzer.call_sites, key=lambda item: (item["call_id"], item.get("end_byte", 0), item["expression"])):
        attrs = {key: deepcopy(call[key]) for key in (
            "syntax_kind", "location_kind", "macro_expansion", "spelling_location", "caller_target", "callee_target",
            "build_root_target", "match_scope", "binding", "start_byte", "end_byte", "end_line_number", "end_column") if key in call}
        site_id = native_id = call["call_id"]
        if native_ids[native_id] > 1:
            # 旧 Tree-sitter 记录仅按起点编号，factory()() 的两个调用可共享该 ID。
            # 导出层补充稳定序号，不丢记录，也不修改解析器或原有匹配结果。
            while True:
                site_id = f"{native_id}#export={occurrences[native_id]}"
                occurrences[native_id] += 1
                if site_id not in native_ids and site_id not in used_ids:
                    break
            attrs["source_call_id"] = native_id
        used_ids.add(site_id)
        # 原匹配器未提供独立 evidence 时，保留明确的规则原因作为依据。
        sites.append({"id": site_id, "caller_id": call["caller_id"], "callee_id": call["callee_id"],
                      "callee_name": call.get("callee_name"), "expression": call["expression"], "location": _location(call),
                      "resolution": call["resolution"], "candidate_ids": list(call["candidate_ids"]),
                      "reason": call["reason"], "evidence": call.get("evidence") or call["reason"],
                      "linker_verified": False, "attributes": attrs})
        if call["resolution"] == "matched":
            pairs[(call["caller_id"], call["callee_id"])].append(site_id)
    edges = []
    for (source, target), ids in sorted(pairs.items()):
        key = json.dumps([source, target, "calls"], ensure_ascii=False, separators=(",", ":"))
        edge_id = "e_" + hashlib.sha256(key.encode("utf-8")).hexdigest()
        edges.append({"id": edge_id, "kind": "calls", "source": source, "target": target,
                      "call_site_ids": sorted(ids), "is_cross_repo": analyzer.functions_by_id[source]["repo"] != analyzer.functions_by_id[target]["repo"]})
    diagnostics = []
    def diagnostic(severity, code, message):
        diagnostics.append({"severity": severity, "code": code, "message": message, "location": None})
    for file in sorted(analyzer.metainfo.get("parse_errors", [])):
        diagnostic("warning", "TREE_SITTER_PARSE_ERROR", f"{file} 存在语法错误；结果可能不完整。")
    for job in analyzer.metainfo.get("compiler", {}).get("jobs", []):
        if job.get("diagnostics", "").strip():
            diagnostic("warning", "COMPILER_DIAGNOSTIC", job["diagnostics"].strip())
    for warning in analyzer.link_warnings:
        diagnostic("warning", "MULTIPLE_EXTERNAL_DEFINITIONS", f"外部符号 {warning['linkage_name']} 存在多个定义候选；未执行链接验证。")
    if any(c["reason"] == "cross_repo_requires_build_context" for c in sites):
        diagnostic("warning", "BUILD_CONTEXT_REQUIRED", "存在其他 Repo 中的候选；未提供构建清单，不自动生成跨 Repo 边。")
    if not nodes:
        diagnostic("info", "NO_FUNCTION_DEFINITIONS", "所选 C 源码中没有提取到函数定义。")
    if any(count > 1 for count in native_ids.values()):
        diagnostic("info", "CALL_SITE_ID_DISAMBIGUATED", "共享原始调用点 ID 的记录已按稳定序号区分；原始 ID 保留在 attributes.source_call_id。")
    counts = dict(sorted(Counter(c["resolution"] for c in sites).items()))
    build = analyzer.metainfo.get("build_context") or {}
    graph = {"schema_version": "1.0.0", "directed": True,
             "metadata": {"project_id": project_id, "revision": revision, "frontend": analyzer.frontend,
                          "languages": ["c"], "build_configuration_id": build.get("configuration_id"),
                          "build_root_target": build.get("root"), "matching_policy": "matched_edges_only",
                          "linker_verified": False,
                          "attributes": {"summary": {"node_count": len(nodes), "edge_count": len(edges),
                                                     "call_site_count": len(sites), "resolution_counts": counts},
                                         "compiler": deepcopy(analyzer.metainfo.get("compiler")),
                                         "build_context": deepcopy(analyzer.metainfo.get("build_context")),
                                         "limitations": ["function_pointer_targets_not_resolved", "runtime_path_feasibility_not_checked", "linker_results_not_verified"]}},
             "nodes": nodes, "edges": edges, "call_sites": sites, "diagnostics": diagnostics}
    validate_call_graph(graph)
    return graph


def save_call_graph(graph, output_path):
    """先完整校验，再原子写入 .json；失败不会留下半份 JSON 或损坏旧结果。"""
    validate_call_graph(graph)
    output = Path(output_path)
    if output.suffix.lower() != ".json":
        raise ValueError("调用图输出文件必须使用 .json 扩展名")
    payload = json.dumps(graph, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".vra-", suffix=".json.tmp", dir=output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
