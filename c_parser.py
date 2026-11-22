"""v1：收集函数定义、文件内身份和逐次调用的位置。"""

import os
from pathlib import Path

from tree_sitter import Language, Parser

from call_sites import declaration_bindings, definition_name, extract_call_sites, file_scope_items, has_static
from config import C_LIB_NAME, C_LIB_PATH, WORKSPACE_PATH
from util import get_node_text, parse_function_definition, read_file


class CParser:
    def __init__(self, project_path, source_files=None):
        self.project_path = Path(project_path).resolve()
        self.current_file = ""
        self.language = Language(C_LIB_PATH / C_LIB_NAME, "c")
        self.parser = Parser()
        self.parser.set_language(self.language)
        self.metainfo = {"functions": [], "parse_errors": []}
        self._contexts = {}
        self.source_files = source_files

    def get_metainfo(self):
        return self.metainfo

    def parse_code_to_ast(self, code):
        return self.parser.parse(code.encode("utf-8"))

    def analyze_project(self):
        if not self.project_path.is_dir():
            raise FileNotFoundError(f"分析目录不存在：{self.project_path}")
        self.metainfo = {"functions": [], "parse_errors": [], "project_path": str(self.project_path)}
        if self.source_files is not None:
            selected = sorted({(self.project_path / source).resolve() for source in self.source_files})
            if not selected or any(not p.is_relative_to(self.project_path) or not p.is_file() or p.suffix != ".c" for p in selected):
                raise ValueError("所选源码必须是项目目录内存在的 .c 文件")
            for source in selected:
                self._analyze_file(str(source))
            return
        for root, directories, files in os.walk(self.project_path):
            directories.sort()
            for filename in sorted(files):
                if filename.endswith(".c"):
                    self._analyze_file(str(Path(root) / filename))

    def _analyze_file(self, file_path):
        code = read_file(file_path)
        if not code:
            return
        ast = self.parse_code_to_ast(code)
        self.current_file = file_path
        if ast.root_node.has_error:
            self.metainfo["parse_errors"].append(self._relative_path())
        self._contexts = self._file_contexts(ast.root_node)
        functions = self._extract_functions(ast.root_node)
        self.metainfo["functions"].extend(self._build_function_info(functions))

    def _file_contexts(self, root_node):
        bindings = {}
        static_names = set()
        contexts = {}
        for node in file_scope_items(root_node):
            if node.type == "function_definition":
                name = definition_name(node)
                if name:
                    if has_static(node):
                        static_names.add(name)
                    bindings[name] = {"kind": "function", "origin": "file",
                                      "line_number": node.start_point[0] + 1,
                                      "is_static": has_static(node) or name in static_names}
                contexts[node.start_byte] = {
                    "bindings": dict(bindings),
                    "is_static": has_static(node) or name in static_names,
                }
            else:
                additions = declaration_bindings(node, "file")
                for name, binding in additions.items():
                    if binding["kind"] == "function" and name in static_names:
                        binding["is_static"] = True
                bindings.update(additions)
                if has_static(node):
                    static_names.update(name for name, binding in additions.items()
                                        if binding["kind"] == "function")
        return contexts

    def _extract_functions(self, root_node):
        return self._find_nodes_by_type(root_node, ["function_definition"])

    def _find_nodes_by_type(self, root_node, node_types):
        result = []

        def traverse(node):
            if node.type in node_types:
                result.append(node)
            for child in node.named_children:
                traverse(child)

        traverse(root_node)
        return result

    def _build_function_info(self, function_nodes):
        records = []
        relative_path = self._relative_path()
        for node in function_nodes:
            info = {**self._get_base_info(node), **parse_function_definition(node)}
            name = definition_name(node) or info.get("name", "<unknown>")
            function_id = f"{relative_path}::{name}@{info['line_number']}:{info['column']}"
            context = self._contexts.get(node.start_byte, {"bindings": {}, "is_static": has_static(node)})
            calls = extract_call_sites(node, context["bindings"])
            for call in calls:
                call.update({
                    "call_id": f"{function_id}::call@{call['start_byte']}",
                    "caller_id": function_id,
                    "file_path": self.current_file,
                    "relative_file_path": relative_path,
                    "repo": info["repo"],
                })
            info.update({"function_id": function_id, "name": name,
                         "is_static": context["is_static"], "call_sites": calls})
            # callees 保留旧格式名称摘要；图的身份匹配只使用 call_sites。
            records.append(info)
        return records

    def _get_base_info(self, node):
        return {
            "file_path": self.current_file,
            "relative_file_path": self._relative_path(),
            "source_code": get_node_text(node),
            "line_number": node.start_point[0] + 1,
            "column": node.start_point[1] + 1,
            "end_line_number": node.end_point[0] + 1,
            "end_column": node.end_point[1] + 1,
            "repo": self._extract_repo_name(),
        }

    def _relative_path(self):
        return Path(self.current_file).relative_to(self.project_path).as_posix()

    def _extract_repo_name(self):
        parts = Path(self._relative_path()).parts
        return parts[0] if len(parts) > 1 else "."


if __name__ == "__main__":
    import json
    parser = CParser(WORKSPACE_PATH)
    parser.analyze_project()
    print(json.dumps(parser.get_metainfo(), ensure_ascii=False, indent=2))
