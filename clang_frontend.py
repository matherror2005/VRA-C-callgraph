"""实验前端：读取本机 Clang 的 JSON AST，转换为现有 metainfo。

直接调用使用 DeclRefExpr -> FunctionDecl -> previousDecl 链；不猜测变量指向。
JSON AST 是 Clang 的工具输出，实验报告同时记录本机版本和实际分析参数。
"""

from bisect import bisect_right
from collections import defaultdict
import json
from pathlib import Path
import shutil
import subprocess

from compilation import CompilationError, load_jobs


class ClangAnalysisError(RuntimeError):
    pass


class SourceFiles:
    def __init__(self):
        self.cache = {}

    def data(self, filename):
        path = Path(filename)
        if path not in self.cache:
            data = path.read_bytes()
            starts = [0] + [index + 1 for index, value in enumerate(data) if value == 10]
            self.cache[path] = data, starts
        return self.cache[path]

    def point(self, filename, offset):
        data, starts = self.data(filename)
        if not 0 <= offset <= len(data):
            raise ClangAnalysisError(f"Clang 源位置超出文件范围：{filename}:{offset}")
        line = bisect_right(starts, offset) - 1
        return {"file_path": str(filename), "offset": offset,
                "line_number": line + 1, "column": offset - starts[line] + 1}


def nodes(root):
    yield root
    for child in root.get("inner", []):
        yield from nodes(child)


class TranslationUnit:
    def __init__(self, ast, job, project, sources):
        self.ast = ast
        self.job = job
        self.project = project
        self.sources = sources
        self.tu = Path(job["file"])
        self.relative_tu = self.tu.relative_to(project).as_posix()
        self.repo = self.relative_tu.split("/", 1)[0] if "/" in self.relative_tu else "."
        self.last_file = str(self.tu)
        self._annotate(ast)
        self.all_nodes = list(nodes(ast))
        self.declarations = {node["id"]: node for node in self.all_nodes
                             if node.get("kind", "").endswith("Decl") and node.get("id")}
        self.parents = {}
        for node in self.all_nodes:
            if node.get("kind") == "FunctionDecl" and node.get("id"):
                self._union(node["id"], node.get("previousDecl", node["id"]))

    def _root(self, key):
        self.parents.setdefault(key, key)
        if self.parents[key] != key:
            self.parents[key] = self._root(self.parents[key])
        return self.parents[key]

    def _union(self, first, second):
        self.parents[self._root(first)] = self._root(second)

    def _location(self, raw):
        if not raw:
            return None
        if "spellingLoc" in raw or "expansionLoc" in raw:
            spelling = self._location(raw.get("spellingLoc", {}))
            expansion = self._location(raw.get("expansionLoc", {}))
            primary = expansion or spelling
            return {"point": primary["point"], "macro": True,
                    "spelling": spelling["point"] if spelling else None}
        if "offset" not in raw:
            return None
        if "file" in raw:
            filename = raw["file"]
            if not filename.startswith("<"):
                filename = str((Path(self.job["directory"]) / filename).resolve())
            self.last_file = filename
        return {"point": {"file_path": self.last_file, "offset": raw["offset"],
                          "token_length": raw.get("tokLen", 0),
                          "line_number": raw.get("line"), "column": raw.get("col")},
                "macro": False, "spelling": None}

    def _annotate(self, node):
        # JSON dump 省略重复文件名；按输出的前序顺序恢复，而不是把 includedFrom 当定义文件。
        node["_loc"] = self._location(node.get("loc", {}))
        source_range = node.get("range", {})
        node["_begin"] = self._location(source_range.get("begin", {}))
        node["_end"] = self._location(source_range.get("end", {}))
        for child in node.get("inner", []):
            self._annotate(child)

    def _in_project(self, filename):
        return filename and not filename.startswith("<") and Path(filename).is_relative_to(self.project)

    def _range(self, node):
        begin = node.get("_begin") or node.get("_loc")
        end = node.get("_end") or begin
        if begin is None:
            raise ClangAnalysisError("Clang 节点缺少源位置")
        first, last = begin["point"], end["point"]
        filename = first["file_path"]
        start = first["offset"]
        stop = last["offset"] + last.get("token_length", 0)
        complete = filename == last["file_path"] and stop >= start
        if not complete:
            stop = start + first.get("token_length", 0)
        start_point = self.sources.point(filename, start)
        end_point = self.sources.point(filename, stop)
        source = self.sources.data(filename)[0][start:stop].decode("utf-8")
        return {"file_path": filename, "relative_file_path": Path(filename).relative_to(self.project).as_posix(),
                "line_number": start_point["line_number"], "column": start_point["column"],
                "end_line_number": end_point["line_number"], "end_column": end_point["column"],
                "start_byte": start, "end_byte": stop, "source": source,
                "macro_expansion": begin["macro"] or end["macro"], "single_file_range": complete}

    def _public_point(self, point):
        if point is None:
            return None
        result = dict(point)
        if self._in_project(point["file_path"]):
            result.update(self.sources.point(point["file_path"], point["offset"]))
            result["relative_file_path"] = Path(point["file_path"]).relative_to(self.project).as_posix()
        return result

    def _reference(self, raw):
        decl = self.declarations.get(raw.get("id"), raw)
        location = decl.get("_loc")
        point = location["point"] if location else {}
        filename = point.get("file_path")
        summary = {"kind": raw.get("kind", decl.get("kind")), "name": raw.get("name", decl.get("name")),
                   "type": decl.get("type", raw.get("type", {})).get("qualType"),
                   "linkage_name": decl.get("mangledName", raw.get("name")),
                   "file_path": filename, "line_number": point.get("line_number"),
                   "column": point.get("column"), "is_static": decl.get("storageClass") == "static"}
        if self._in_project(filename):
            fixed = self.sources.point(filename, point["offset"])
            summary.update(line_number=fixed["line_number"], column=fixed["column"],
                           relative_file_path=Path(filename).relative_to(self.project).as_posix())
        if raw.get("kind") == "FunctionDecl" and raw.get("id"):
            group = self.groups.get(self._root(raw["id"]), [])
            summary["is_static"] = any(item.get("storageClass") == "static" for item in group)
        return summary

    @staticmethod
    def _callee_reference(node):
        kind = node.get("kind")
        if kind == "DeclRefExpr":
            return node.get("referencedDecl")
        wrappers = {"ImplicitCastExpr", "ParenExpr", "CStyleCastExpr"}
        if kind in wrappers or (kind == "UnaryOperator" and node.get("opcode") in {"*", "&"}):
            inner = node.get("inner", [])
            return TranslationUnit._callee_reference(inner[0]) if len(inner) == 1 else None
        # 尤其不能进入 CallExpr：factory()() 的外层调用目标不是 factory。
        return None

    def extract(self):
        self.groups = defaultdict(list)
        for decl in self.declarations.values():
            if decl.get("kind") == "FunctionDecl":
                self.groups[self._root(decl["id"])].append(decl)
        records = []
        bodies = {}
        group_definitions = {}
        outside_definitions = set()
        for group, declarations in self.groups.items():
            for decl in declarations:
                body = next((item for item in decl.get("inner", []) if item.get("kind") == "CompoundStmt"), None)
                if body is None:
                    continue
                begin = decl.get("_begin") or decl.get("_loc")
                if begin is None or not self._in_project(begin["point"]["file_path"]):
                    outside_definitions.add(group)
                    continue
                position = self._range(decl)
                name = decl["name"]
                function_id = f"{position['relative_file_path']}::{name}@{position['line_number']}:{position['column']}"
                if position["relative_file_path"] != self.relative_tu:
                    function_id += f"#tu={self.relative_tu}"
                function_type = decl.get("type", {}).get("qualType", "")
                prefix = function_type.split("(", 1)[0].strip()
                return_type = prefix if "(*" not in function_type else None
                parameters = [{"name": child.get("name", ""), "type": child.get("type", {}).get("qualType", "")}
                              for child in decl.get("inner", []) if child.get("kind") == "ParmVarDecl"]
                body_source = self._range(body)["source"]
                record = {key: value for key, value in position.items() if key != "source"}
                record.update({"function_id": function_id, "name": name,
                               "source_code": position["source"], "repo": self.repo,
                               "translation_unit": self.relative_tu,
                               "is_static": any(item.get("storageClass") == "static" for item in declarations),
                               "is_inline": decl.get("inline", False), "function_type": function_type,
                               "return_type": return_type, "parameters": parameters,
                               "linkage_name": decl.get("mangledName", name), "scope": "global",
                               "body": body_source, "callees": [], "call_sites": [], "frontend": "clang"})
                records.append(record)
                bodies[function_id] = body
                group_definitions[group] = function_id
        for record in records:
            for ordinal, call in enumerate(item for item in nodes(bodies[record["function_id"]])
                                            if item.get("kind") == "CallExpr"):
                position = self._range(call)
                children = call.get("inner", [])
                callee = children[0] if children else {}
                raw_reference = self._callee_reference(callee)
                reference = self._reference(raw_reference) if raw_reference else None
                direct = reference is not None and reference["kind"] == "FunctionDecl"
                group = self._root(raw_reference["id"]) if direct else None
                callee_position = self._range(callee) if callee.get("_begin") else position
                site = {key: value for key, value in position.items() if key != "source"}
                site.update({"expression": position["source"], "callee_expression": callee_position["source"],
                             "callee_name": reference["name"] if reference else None,
                             "syntax_kind": callee.get("kind", "unknown"),
                             "call_id": f"{record['function_id']}::call@{position['start_byte']}#{ordinal}",
                             "caller_id": record["function_id"], "repo": self.repo,
                             "translation_unit": self.relative_tu, "frontend": "clang",
                             "binding": None if direct else reference,
                             "compiler_reference": reference,
                             "compiler_target_id": group_definitions.get(group),
                             "definition_outside_project": group in outside_definitions,
                             #已经标记indirect但是还没有实现函数
                             "resolution": "pending" if direct else "indirect",
                             "reason": "clang_function_declaration" if direct else "clang_indirect_call",
                             "callee_id": None, "candidate_ids": [],
                             "location_kind": "macro_expansion_range" if position["macro_expansion"] else "source_range",
                             "spelling_location": self._public_point((call.get("_begin") or {}).get("spelling"))})
                record["call_sites"].append(site)
            record["callees"] = sorted({call["callee_name"] for call in record["call_sites"] if call["callee_name"]})
        return records


class ClangCParser:
    def __init__(self, project_path, clang="clang", compile_commands=None, extra_args=(), timeout=60, source_files=None):
        self.project_path = Path(project_path).resolve()
        self.clang = shutil.which(str(clang))
        if not self.clang:
            raise ClangAnalysisError(f"找不到 Clang：{clang}")
        self.compile_commands = compile_commands
        self.extra_args = list(extra_args)
        self.timeout = timeout
        self.source_files = source_files
        self.metainfo = {"functions": [], "parse_errors": [], "frontend": "clang"}

    def get_metainfo(self):
        return self.metainfo

    def analyze_project(self):
        self.metainfo = {"functions": [], "parse_errors": [], "frontend": "clang"}
        try:
            version = subprocess.run([self.clang, "--version"], text=True, capture_output=True,
                                     check=True, timeout=self.timeout).stdout.splitlines()[0]
        except (OSError, subprocess.SubprocessError) as error:
            raise ClangAnalysisError(f"无法运行 Clang：{error}") from error
        jobs = load_jobs(self.project_path, self.compile_commands, self.extra_args, self.source_files)
        self.metainfo = {"functions": [], "parse_errors": [], "frontend": "clang",
                         "compiler": {"executable": self.clang, "version": version, "jobs": []}}
        completed = {"functions": [], "parse_errors": [], "frontend": "clang",
                     "project_path": str(self.project_path),
                     "compiler": {"executable": self.clang, "version": version, "jobs": []}}
        sources = SourceFiles()
        for job in jobs:
            arguments = [self.clang, *job["arguments"], "-fsyntax-only", "-fno-color-diagnostics",
                         "-Xclang", "-ast-dump=json", job["file"]]
            try:
                result = subprocess.run(arguments, cwd=job["directory"], capture_output=True,
                                        text=True, timeout=self.timeout)
            except (OSError, subprocess.SubprocessError) as error:
                raise ClangAnalysisError(f"Clang 分析失败：{job['file']}\n{error}") from error
            if result.returncode:
                raise ClangAnalysisError(f"Clang 拒绝该编译配置，不使用不完整 AST：{job['file']}\n{result.stderr}")
            try:
                ast = json.loads(result.stdout)
            except ValueError as error:
                raise ClangAnalysisError(f"Clang 未返回有效的 JSON AST：{job['file']}") from error
            completed["functions"].extend(TranslationUnit(ast, job, self.project_path, sources).extract())
            completed["compiler"]["jobs"].append({**job, "executed_arguments": arguments,
                                                 "diagnostics": result.stderr})
        completed["functions"].sort(key=lambda func: func["function_id"])
        self.metainfo = completed
