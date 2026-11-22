"""一个选定构建目标的源码/依赖范围；提供候选边界，不声称复现链接器。"""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath


class BuildContextError(ValueError):
    pass


def source_name(value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise BuildContextError("sources 必须使用相对项目目录的 POSIX 路径")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.suffix != ".c":
        raise BuildContextError(f"构建源码必须是项目内的相对 .c 路径：{value}")
    return path.as_posix()


class BuildContext:
    def __init__(self, data):
        if not isinstance(data, dict):
            raise BuildContextError("构建清单必须是 JSON 对象")
        self.data = deepcopy(data)
        self.root = data.get("root")
        self.targets = data.get("targets")
        if not isinstance(self.targets, dict) or not self.targets:
            raise BuildContextError("targets 必须是非空对象")
        if not isinstance(self.root, str) or self.root not in self.targets:
            raise BuildContextError("root 必须指定一个存在的构建目标")
        if "configuration" in data and not isinstance(data["configuration"], str):
            raise BuildContextError("configuration 标签必须是字符串")
        for name, target in self.targets.items():
            if not isinstance(name, str) or not name or not isinstance(target, dict):
                raise BuildContextError("构建目标名称和记录无效")
            if target.get("kind") not in {"executable", "static_library"}:
                raise BuildContextError(f"当前仅支持 executable/static_library 构建目标：{name}")
            sources = target.get("sources")
            dependencies = target.get("dependencies")
            if not isinstance(sources, list) or not sources:
                raise BuildContextError(f"目标缺少非空 sources 数组：{name}")
            normalized = [source_name(value) for value in sources]
            if len(normalized) != len(set(normalized)):
                raise BuildContextError(f"同一目标存在重复源码：{name}")
            if not isinstance(dependencies, list) or not all(isinstance(d, str) for d in dependencies):
                raise BuildContextError(f"dependencies 必须是字符串数组：{name}")
            if len(dependencies) != len(set(dependencies)):
                raise BuildContextError(f"重复的依赖目标：{name}")
            if any(d not in self.targets for d in dependencies):
                raise BuildContextError(f"依赖目标不存在：{name}")
            target = self.data["targets"][name]
            target["sources"] = sorted(normalized)
            target["dependencies"] = sorted(dependencies)
        self.targets = self.data["targets"]
        selected, visiting = set(), set()

        def visit(name):
            if name in visiting:
                raise BuildContextError("当前构建清单不支持循环库依赖；请先提供展开的构建范围")
            if name in selected:
                return
            visiting.add(name)
            for dependency in self.targets[name]["dependencies"]:
                visit(dependency)
            visiting.remove(name)
            selected.add(name)

        visit(self.root)
        self.selected_targets = tuple(sorted(selected))
        self.source_owners = {}
        for name in self.selected_targets:
            for source in self.targets[name]["sources"]:
                if source in self.source_owners:
                    raise BuildContextError(f"同一源码属于多个已选目标，请拆分配置：{source}")
                self.source_owners[source] = name
        self.sources = tuple(sorted(self.source_owners))
        self.configuration_id = None

    @classmethod
    def from_file(cls, path):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise BuildContextError(f"无法读取构建清单：{error}") from error
        return cls(data)

    def validate_sources(self, project):
        project = Path(project).resolve()
        for source in self.sources:
            path = (project / source).resolve()
            if not path.is_relative_to(project) or not path.is_file():
                raise BuildContextError(f"已选构建源码不存在或越出项目目录：{source}")

    def owner(self, function):
        # 头文件定义属于包含它的 TU，而不是按头文件路径划分所属构建目标。
        return self.source_owners.get(function.get("translation_unit", function["relative_file_path"]))

    @staticmethod
    def _project(meta):
        if meta.get("project_path"):
            return Path(meta["project_path"]).resolve()
        for function in meta.get("functions", []):
            absolute = Path(function["file_path"]).resolve()
            relative = PurePosixPath(function["relative_file_path"])
            return absolute.parents[len(relative.parts) - 1]
        return None

    def apply(self, meta):
        """给已选函数及调用增加配置/目标身份，更新所有引用 ID；输入已由调用方复制。"""
        functions = [f for f in meta.get("functions", []) if self.owner(f) is not None]
        project = self._project(meta)
        jobs = meta.get("compiler", {}).get("jobs", [])
        normalized_jobs = []
        for job in jobs:
            filename = os.path.relpath(job["file"], project) if project else job["file"]
            if filename not in self.source_owners:
                continue
            normalized_jobs.append({"file": filename, "arguments": job["arguments"],
                                    "directory": os.path.relpath(job["directory"], project) if project else job["directory"]})
        normalized_jobs.sort(key=lambda job: job["file"])
        if meta.get("frontend") == "clang" and {j["file"] for j in normalized_jobs} != set(self.sources):
            raise BuildContextError("Clang 已分析的编译单元与构建清单不一致；请提供完整且选定的编译配置")
        selected = {name: self.targets[name] for name in self.selected_targets}
        identity = {"root": self.root, "targets": selected, "configuration": self.data.get("configuration"),
                    "compilation": normalized_jobs, "frontend": meta.get("frontend", "tree-sitter")}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()
        self.configuration_id = digest[:20]
        suffix = f"#build={self.configuration_id}"
        old_ids = {f["function_id"]: f["function_id"] + suffix for f in functions}
        for function in functions:
            original = function["function_id"]
            function.update(function_id=old_ids[original], source_function_id=original,
                            build_configuration_id=self.configuration_id, build_target=self.owner(function))
            for call in function.get("call_sites", []):
                call["caller_id"] = old_ids[original]
                call["call_id"] += suffix
                if call.get("compiler_target_id") in old_ids:
                    call["compiler_target_id"] = old_ids[call["compiler_target_id"]]
                call.update(build_configuration_id=self.configuration_id,
                            caller_target=function["build_target"], build_root_target=self.root,
                            linker_verified=False)
        meta["functions"] = functions
        meta["build_context"] = self.summary()

    def summary(self):
        return {"configuration_id": self.configuration_id, "configuration_label": self.data.get("configuration"),
                "root": self.root, "selected_targets": list(self.selected_targets),
                "sources": list(self.sources), "source_owners": dict(self.source_owners),
                "matching_scope": "selected_root_dependency_closure", "linker_verified": False}
