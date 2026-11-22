"""公开 Python 入口：项目路径与配置 → 规范化完整调用图。"""

from pathlib import Path
import math
import os

from build_context import BuildContext, BuildContextError
from call_graph import CallGraphAnalyzer
from clang_frontend import ClangAnalysisError, ClangCParser
from compilation import CompilationError, load_jobs
from graph_export import GraphValidationError, export_call_graph, save_call_graph, validate_call_graph


class AnalysisError(ValueError):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)

    def to_dict(self):
        return {"error": {"code": self.code, "message": self.message}}


def generate_call_graph(project_path, *, frontend="clang", compile_commands=None,
                        build_manifest=None, clang_args=(), clang="clang", timeout=60,
                        project_id=None, revision=None):
    """返回 Graph v1 字典；正常输入/配置失败抛 AnalysisError，不返回伪造的空图。

    project_path 为本地 C 项目目录。build_manifest 可为 JSON 文件路径或清单字典。
    不安装依赖、不下载项目、不执行项目构建脚本；分析结果不等于链接或运行时证明。
    """
    try:
        project = Path(project_path).resolve()
    except (TypeError, ValueError, OSError) as error:
        raise AnalysisError("INVALID_PROJECT", f"项目路径无效：{error}") from error
    if not project.is_dir():
        raise AnalysisError("INVALID_PROJECT", f"项目目录不存在：{project}")
    if frontend not in ("clang", "tree-sitter"):
        raise AnalysisError("INVALID_OPTIONS", "frontend 必须是 clang 或 tree-sitter")
    if not isinstance(clang_args, (list, tuple)) or not all(isinstance(a, str) and a for a in clang_args):
        raise AnalysisError("INVALID_OPTIONS", "clang_args 必须是编译参数字符串列表或元组")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise AnalysisError("INVALID_OPTIONS", "timeout 必须大于 0")
    if frontend == "tree-sitter" and (compile_commands is not None or clang_args):
        raise AnalysisError("INVALID_OPTIONS", "编译数据库和 clang_args 仅适用于 Clang，不能静默忽略")
    if compile_commands is not None and not isinstance(compile_commands, (str, os.PathLike)):
        raise AnalysisError("INVALID_OPTIONS", "compile_commands 必须是文件或目录路径")
    label = (project.name or "project") if project_id is None else project_id
    if not isinstance(label, str) or not label.strip():
        raise AnalysisError("INVALID_OPTIONS", "project_id 必须是非空字符串")
    if revision is not None and (not isinstance(revision, str) or not revision.strip()):
        raise AnalysisError("INVALID_OPTIONS", "revision 必须是非空字符串或 None")
    try:
        context = None
        if build_manifest is not None:
            context = BuildContext(build_manifest) if isinstance(build_manifest, dict) else BuildContext.from_file(build_manifest)
            context.validate_sources(project)
    except (BuildContextError, TypeError, OSError) as error:
        raise AnalysisError("INVALID_BUILD_MANIFEST", str(error)) from error
    selected = context.sources if context is not None else None
    try:
        if frontend == "clang":
            jobs = load_jobs(project, compile_commands, clang_args, selected)
            if not jobs:
                raise AnalysisError("NO_C_SOURCE_FILES", "选定范围内没有 .c 源文件；当前接口仅支持 C")
            parser = ClangCParser(project, clang=clang, compile_commands=compile_commands,
                                  extra_args=clang_args, timeout=timeout, source_files=selected)
        else:
            if selected is None and not any(project.rglob("*.c")):
                raise AnalysisError("NO_C_SOURCE_FILES", "项目中没有 .c 源文件；当前接口仅支持 C")
            # 不调用自动 clone / make；只加载现有 Tree-sitter 及语法库。
            from c_parser import CParser
            parser = CParser(project, source_files=selected)
        parser.analyze_project()
        analyzer = CallGraphAnalyzer(parser.get_metainfo(), build_context=context)
        return export_call_graph(analyzer, project_id=label, revision=revision)
    except AnalysisError:
        raise
    except CompilationError as error:
        raise AnalysisError("COMPILATION_CONFIGURATION_ERROR", str(error)) from error
    except ClangAnalysisError as error:
        raise AnalysisError("CLANG_ANALYSIS_FAILED", str(error)) from error
    except BuildContextError as error:
        raise AnalysisError("INVALID_BUILD_MANIFEST", str(error)) from error
    except GraphValidationError as error:
        raise AnalysisError("INVALID_GRAPH", str(error)) from error
    except (ImportError, OSError) as error:
        raise AnalysisError("FRONTEND_OR_SOURCE_ERROR", str(error)) from error
    except (TypeError, ValueError) as error:
        raise AnalysisError("ANALYSIS_ERROR", str(error)) from error
