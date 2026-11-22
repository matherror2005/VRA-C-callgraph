"""按定义 ID 构图；名字和文件范围只用于有限候选匹配。"""

from collections import defaultdict
from copy import deepcopy

from build_context import BuildContext


class AmbiguousFunctionError(ValueError):
    def __init__(self, query, candidates):
        self.candidates = candidates
        super().__init__(f"{query!r} 对应多个定义，请使用完整 function_id 查询")


class UnknownFunctionError(ValueError):
    pass


class CallGraphAnalyzer:
    def __init__(self, metainfo, build_context=None):
        # 匹配结果属于分析器，不修改调用方刚获得的原始 metainfo。
        self.metainfo = deepcopy(metainfo)
        self.build_context = (deepcopy(build_context) if isinstance(build_context, BuildContext)
                              else BuildContext(build_context) if build_context is not None else None)
        if self.build_context is not None:
            self.build_context.apply(self.metainfo)
        self.frontend = self.metainfo.get("frontend", "tree-sitter")
        self.functions_by_id = {}
        self._by_name = defaultdict(list)
        self._by_repo_name = defaultdict(list)
        self._by_file_name = defaultdict(list)
        self._external_symbols = defaultdict(list)
        self._workspace_symbols = defaultdict(list)
        for func in self.metainfo.get("functions", []):
            function_id = func["function_id"]
            if function_id in self.functions_by_id:
                raise ValueError(f"重复的 function_id：{function_id}")
            self.functions_by_id[function_id] = func
            self._by_name[func["name"]].append(function_id)
            self._by_repo_name[(func["repo"], func["name"])].append(function_id)
            self._by_file_name[(func["relative_file_path"], func["name"])].append(function_id)
            if not func["is_static"]:
                symbol = func.get("linkage_name", func["name"])
                self._external_symbols[(func["repo"], symbol)].append(function_id)
                self._workspace_symbols[symbol].append(function_id)
        for index in (self._by_name, self._by_repo_name, self._by_file_name):
            for values in index.values():
                values.sort()
        self.link_warnings = [
            {"repo": repo, "linkage_name": symbol, "candidate_ids": sorted(values)}
            for (repo, symbol), values in self._external_symbols.items() if len(values) > 1
        ]
        if self.build_context is not None:
            self.link_warnings = [
                {"build_root": self.build_context.root, "linkage_name": symbol, "candidate_ids": sorted(values)}
                for symbol, values in self._workspace_symbols.items() if len(values) > 1
            ]
        self._build_call_maps()

    def _build_call_maps(self):
        self.call_map = {function_id: [] for function_id in self.functions_by_id}
        self.reverse_map = {function_id: [] for function_id in self.functions_by_id}
        self.call_sites = []
        self.edges = []
        invalid_files = set(self.metainfo.get("parse_errors", []))
        for function_id in sorted(self.functions_by_id):
            func = self.functions_by_id[function_id]
            for call in func.get("call_sites", []):
                self._match_call(call, invalid_files)
                call["is_cross_repo"] = (call["resolution"] == "matched"
                                         and func["repo"] != self.functions_by_id[call["callee_id"]]["repo"])
                call["linker_verified"] = False
                if self.build_context is not None and call["resolution"] == "matched":
                    call["callee_target"] = self.functions_by_id[call["callee_id"]]["build_target"]
                self.call_sites.append(call)
                if call["resolution"] != "matched":
                    continue
                callee_id = call["callee_id"]
                self.edges.append(call)
                if callee_id not in self.call_map[function_id]:
                    self.call_map[function_id].append(callee_id)
                if function_id not in self.reverse_map[callee_id]:
                    self.reverse_map[callee_id].append(function_id)

    def _match_call(self, call, invalid_files):
        call["callee_id"] = None
        call["candidate_ids"] = []
        #todo：间接调用，还没有分析最终调用哪个函数；当前不做指针目标分析，先标记为indirect，跳过后续匹配
        if call["resolution"] == "indirect":
            return
        if call.get("frontend") == "clang":
            self._match_clang_call(call)
            return
        if call["relative_file_path"] in invalid_files:
            call.update(resolution="unresolved", reason="file_has_parse_errors")
            return
        name = call["callee_name"]
        same_file = self._by_file_name.get((call["relative_file_path"], name), [])
        if any(self.functions_by_id[item]["is_static"] for item in same_file):
            candidates = same_file
            reason = "same_file_static"
        else:
            if (call.get("binding") or {}).get("is_static"):
                call.update(resolution="unresolved", reason="internal_declaration_without_definition")
                return
            if self.build_context is not None:
                self._match_build_symbol(call, name)
                return
            candidates = [function_id for function_id in self._by_repo_name.get((call["repo"], name), [])
                          if not self.functions_by_id[function_id]["is_static"]]
            reason = "unique_non_static_in_repo"
        call["candidate_ids"] = list(candidates)
        if len(candidates) == 1:
            target = self.functions_by_id[candidates[0]]
            if not target["is_static"] and target["relative_file_path"] == call["relative_file_path"]:
                reason = "same_file_definition"
            call.update(resolution="matched", reason=reason, callee_id=candidates[0])
        elif candidates:
            call.update(resolution="ambiguous", reason="multiple_candidate_definitions")
        else:
            call.update(resolution="unresolved", reason="no_definition_in_repo_scope")
            self._workspace_diagnostics(call, name)

    def _workspace_diagnostics(self, call, symbol):
        # 没有构建证据时可报告其他 Repo 的候选，但不能仅凭全局唯一就建边。
        candidates = sorted(fid for fid in self._workspace_symbols.get(symbol, [])
                            if self.functions_by_id[fid]["repo"] != call["repo"])
        if candidates:
            call.update(candidate_ids=candidates, reason="cross_repo_requires_build_context",
                        evidence="workspace_candidates_without_build_scope")

    def _match_build_symbol(self, call, symbol):
        candidates = sorted(self._workspace_symbols.get(symbol, []))
        call.update(candidate_ids=candidates, match_scope="selected_root_dependency_closure",
                    evidence="build_scope_symbol_candidate_not_linker_verified")
        if len(candidates) == 1:
            call.update(resolution="matched", reason="build_scope_external_symbol_candidate", callee_id=candidates[0])
        elif candidates:
            call.update(resolution="ambiguous", reason="multiple_build_scope_symbol_candidates")
        else:
            call.update(resolution="unresolved", reason="no_external_definition_in_build_scope")

    def _match_clang_call(self, call):
        """编译单元内引用按 Clang 链接；外部声明仍保留为候选匹配。"""
        target_id = call.get("compiler_target_id")
        if target_id is not None:
            if target_id not in self.functions_by_id:
                call.update(resolution="unresolved", reason="compiler_definition_not_in_analysis")
                return
            call.update(resolution="matched", reason="clang_decl_reference", callee_id=target_id,
                        candidate_ids=[target_id], evidence="compiler_reference_in_translation_unit")
            return
        reference = call.get("compiler_reference") or {}
        if call.get("definition_outside_project"):
            call.update(resolution="unresolved", reason="definition_outside_project")
            return
        if reference.get("is_static"):
            call.update(resolution="unresolved", reason="internal_declaration_without_definition")
            return
        symbol = reference.get("linkage_name", call["callee_name"])
        if self.build_context is not None:
            self._match_build_symbol(call, symbol)
            return
        candidates = sorted(self._external_symbols.get((call["repo"], symbol), []))
        call["candidate_ids"] = candidates
        if len(candidates) == 1:
            call.update(resolution="matched", reason="external_symbol_candidate", callee_id=candidates[0],
                        evidence="cross_translation_unit_candidate_not_linker_verified")
        elif candidates:
            call.update(resolution="ambiguous", reason="multiple_external_symbol_candidates")
        else:
            call.update(resolution="unresolved", reason="external_declaration_without_definition")
            self._workspace_diagnostics(call, symbol)

    def function_summary(self, function_id):
        func = self.functions_by_id[function_id]
        result = {key: func[key] for key in (
            "function_id", "name", "relative_file_path", "line_number", "column", "repo", "is_static"
        )}
        for key in ("translation_unit", "frontend", "function_type", "build_target", "build_configuration_id"):
            if key in func:
                result[key] = func[key]
        return result

    def _select_function(self, query):
        if query in self.functions_by_id:
            return query
        candidates = self._by_name.get(query, [])
        if not candidates:
            raise UnknownFunctionError(f"没有找到函数定义：{query}")
        if len(candidates) > 1:
            raise AmbiguousFunctionError(query, [self.function_summary(item) for item in candidates])
        return candidates[0]

    def _unmatched_references(self, name, target_id=None):
        result = []
        target = self.functions_by_id.get(target_id)
        for call in self.call_sites:
            if call["resolution"] == "matched" or call["callee_name"] != name:
                continue
            if target is not None:
                if call["repo"] != target["repo"] and not self.build_context and not call["candidate_ids"]:
                    continue
                if call["candidate_ids"] and target_id not in call["candidate_ids"]:
                    continue
            result.append(call)
        return result

    def get_call_graph(self, api_name):
        try:
            target_id = self._select_function(api_name)
        except UnknownFunctionError:
            references = self._unmatched_references(api_name)
            return {"query": api_name, "frontend": self.frontend,
                    "status": "unresolved" if references else "not_found",
                    "target": None, "by_repo": {}, "incoming_calls": [], "outgoing_calls": [],
                    "unmatched_references": references}
        target = self.functions_by_id[target_id]
        by_repo = {target["repo"]: {"callers": [], "callees": []}}
        for key, function_ids in (("callers", self.reverse_map[target_id]),
                                  ("callees", self.call_map[target_id])):
            for function_id in function_ids:
                func = self.functions_by_id[function_id]
                by_repo.setdefault(func["repo"], {"callers": [], "callees": []})[key].append(
                    self.function_summary(function_id))
        return {
            "query": api_name, "frontend": self.frontend, "status": "matched", "target": self.function_summary(target_id),
            "build_context": self.metainfo.get("build_context"),
            "by_repo": by_repo,
            "incoming_calls": [call for call in self.edges if call["callee_id"] == target_id],
            "outgoing_calls": target.get("call_sites", []),
            "unmatched_references": self._unmatched_references(target["name"], target_id),
        }

    def get_caller_paths(self, api_name, max_paths=10):
        target_id = self._select_function(api_name)
        paths = {}

        def dfs(current, path, visited):
            if len(paths) >= max_paths:
                return
            path = path + [current]
            visited = visited | {current}
            if not self.reverse_map[current]:
                paths[f"path_{len(paths)}"] = list(reversed(path))
            else:
                for caller in self.reverse_map[current]:
                    if caller not in visited:
                        dfs(caller, path, visited)

        dfs(target_id, [], set())
        return paths

    def get_callee_paths(self, api_name, max_paths=10):
        target_id = self._select_function(api_name)
        paths = {}

        def dfs(current, path, visited):
            if len(paths) >= max_paths:
                return
            path = path + [current]
            visited = visited | {current}
            if not self.call_map[current]:
                paths[f"path_{len(paths)}"] = path
            else:
                for callee in self.call_map[current]:
                    if callee not in visited:
                        dfs(callee, path, visited)

        dfs(target_id, [], set())
        return paths

    def get_related_functions(self, api_name):
        """收集已匹配图中所有反向可达的定义 ID，包含目标自身。"""
        target_id = self._select_function(api_name)
        related = set()
        pending = [target_id]
        while pending:
            current = pending.pop()
            if current in related:
                continue
            related.add(current)
            pending.extend(self.reverse_map[current])
        return related
