"""v3：通过构建清单选择依赖范围，按定义身份构建跨 Repo 调用图。"""

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

from call_graph import AmbiguousFunctionError, CallGraphAnalyzer, UnknownFunctionError
from config import BUGS_PATH, WORKSPACE_PATH
from clang_frontend import ClangAnalysisError, ClangCParser
from compilation import CompilationError
from build_context import BuildContext, BuildContextError


def analyze_bug_impact(api_name, call_graph, bugs_path=BUGS_PATH):
    try:
        bugs = json.loads(Path(bugs_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"无法读取 bug 信息：{error}")
        return
    target = call_graph["target"]
    name = target["name"] if target else api_name
    matches = []
    if isinstance(bugs, list):
        for entry in bugs:
            if not isinstance(entry, dict) or entry.get("api") != name:
                continue
            if entry.get("function_id"):
                if target is None or entry["function_id"] != target["function_id"]:
                    continue
            if entry.get("bug"):
                matches.append(entry)
    if not matches:
        print(f"未找到 API '{name}' 的 bug 信息")
        return
    print(f"\n风险说明：{name}")
    for entry in matches:
        mode = "定义 ID 匹配" if entry.get("function_id") else "名称匹配，未验证适用于此定义"
        print(f"  [{mode}] {entry['bug']}")
    print("已匹配到所选定义的直接调用者：")
    found = False
    for repo, data in call_graph["by_repo"].items():
        for caller in data["callers"]:
            found = True
            print(f"  {repo}: {caller['function_id']}")
    if not found:
        print("  无已匹配的直接调用者；这不能排除未解析或间接调用。")
    if call_graph["unmatched_references"]:
        print("未匹配引用（不能据此确认调用了该定义）：")
        for call in call_graph["unmatched_references"]:
            print(f"  {call['relative_file_path']}:{call['line_number']}:{call['column']} "
                  f"{call['expression']} [{call['resolution']}]")


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("必须大于 0")
    return number


def export_project(args):
    """无 -api 时导出整张图；stdout 保持纯 JSON，失败使用结构化 stderr。"""
    from api import AnalysisError, generate_call_graph
    from graph_export import save_call_graph
    try:
        output = args.output if args.output is not None and str(args.output) != "-" else None
        if output is not None:
            if output.suffix.lower() != ".json":
                raise AnalysisError("INVALID_OUTPUT", "输出文件必须使用 .json 扩展名；或用 --output - 输出到 stdout")
            protected = []
            if args.build_manifest is not None:
                protected.append(args.build_manifest.resolve())
            if args.compile_commands is not None:
                database = args.compile_commands
                protected.append((database / "compile_commands.json" if database.is_dir() else database).resolve())
            if output.resolve() in protected:
                raise AnalysisError("INVALID_OUTPUT", "输出不能覆盖输入的构建清单或编译数据库")
        graph = generate_call_graph(args.project_path, frontend=args.frontend,
                                    compile_commands=args.compile_commands, build_manifest=args.build_manifest,
                                    clang_args=args.clang_arg, clang=args.clang,
                                    project_id=args.project_id, revision=args.revision)
        if output is None:
            print(json.dumps(graph, ensure_ascii=False, indent=2))
        else:
            try:
                save_call_graph(graph, output)
            except (OSError, ValueError, TypeError) as error:
                raise AnalysisError("OUTPUT_ERROR", str(error)) from error
            print(f"已导出：{output.resolve()}；节点 {len(graph['nodes'])}，"
                  f"关系边 {len(graph['edges'])}，调用点 {len(graph['call_sites'])}", file=sys.stderr)
        return 0
    except AnalysisError as error:
        print(json.dumps(error.to_dict(), ensure_ascii=False), file=sys.stderr)
        return 2


def main():
    parser = argparse.ArgumentParser(description="VRA v3：整项目 Graph JSON 导出，或使用 -api 查询目标函数")
    parser.add_argument("--frontend", choices=("tree-sitter", "clang"),
                        help="整图导出默认 clang；-api 查询默认 tree-sitter")
    parser.add_argument("--clang", default="clang", help="本机 Clang 可执行文件；不自动安装")
    parser.add_argument("--compile-commands", type=Path, help="Clang 编译数据库路径（文件或目录）")
    parser.add_argument("--build-manifest", type=Path,
                        help="构建目标与依赖 JSON；源码路径相对 --project-path，限定跨 Repo 候选范围")
    parser.add_argument("--clang-arg", action="append", default=[],
                        help="额外编译参数，例 --clang-arg=-DFEATURE_ENABLED；可重复")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("-api", help="目标名称或完整 function_id；省略则导出整个项目")
    mode.add_argument("--output", "-o", type=Path, help="整图 JSON 输出路径；省略或为 - 时输出到 stdout")
    parser.add_argument("--project-id", help="整图输出的项目标识，默认取项目目录名")
    parser.add_argument("--revision", help="调用方提供的源码版本/commit；不自动猜测")
    parser.add_argument("--project-path", type=Path, default=WORKSPACE_PATH,
                        help="待分析目录；默认 workspace。首层子目录作为仓库边界")
    parser.add_argument("--bugs-path", type=Path, default=BUGS_PATH, help="风险说明 JSON 路径")
    parser.add_argument("--max-paths", type=positive_int, default=10,
                        help="最多展示的调用路径数量；不限制相关函数集合")
    parser.add_argument("--show-bug", action="store_true")
    args = parser.parse_args()
    args.frontend = args.frontend or ("tree-sitter" if args.api else "clang")
    if args.show_bug and not args.api:
        parser.error("--show-bug 必须同时指定 -api")
    if args.api and (args.project_id is not None or args.revision is not None):
        parser.error("--project-id 和 --revision 用于整图导出，不与 -api 查询混用")
    if not args.api:
        return export_project(args)
    if not args.project_path.is_dir():
        parser.error(f"分析目录不存在：{args.project_path}")
    if args.frontend == "tree-sitter" and (args.compile_commands or args.clang_arg):
        parser.error("编译数据库和 --clang-arg 仅用于 --frontend clang，不能静默忽略")

    try:
        context = BuildContext.from_file(args.build_manifest) if args.build_manifest else None
        if context is not None:
            context.validate_sources(args.project_path)
        selected = context.sources if context is not None else None
        if args.frontend == "clang":
            c_parser = ClangCParser(args.project_path, args.clang, args.compile_commands, args.clang_arg,
                                   source_files=selected)
        else:
            from c_parser import CParser
            from util import build_tree_sitter_c
            build_tree_sitter_c()
            c_parser = CParser(args.project_path, source_files=selected)
        c_parser.analyze_project()
        analyzer = CallGraphAnalyzer(c_parser.get_metainfo(), build_context=context)
    except (ClangAnalysisError, CompilationError, BuildContextError, OSError) as error:
        print(str(error))
        return 2
    counts = Counter(call["resolution"] for call in analyzer.call_sites)
    print(f"正在分析：{args.api}；前端：{args.frontend}")
    print(f"函数定义：{len(analyzer.functions_by_id)}；调用点：{len(analyzer.call_sites)}")
    print("调用匹配状态：" + json.dumps(dict(sorted(counts.items())), ensure_ascii=False))
    if analyzer.build_context is not None:
        context_info = analyzer.metainfo["build_context"]
        print(f"构建目标：{context_info['root']}；配置 ID：{context_info['configuration_id']}")
        print("所选目标及依赖：" + ", ".join(context_info["selected_targets"]))
        print("跨 Repo 匹配依据是构建范围内的唯一外部符号候选；尚未验证最终链接结果。")
    else:
        print("未提供构建清单：不自动生成跨 Repo 边；其他 Repo 的候选仅作诊断保留。")
    if args.frontend == "clang":
        print("Clang 按当前配置处理预处理和编译单元内声明引用；跨单元仍为候选匹配。")
        print("未进行函数指针指向分析、控制流可达性分析或最终链接验证。")
        compiler = analyzer.metainfo["compiler"]
        print(f"{compiler['version']}；编译单元：{len(compiler['jobs'])}")
        if analyzer.link_warnings:
            print("多个外部定义候选（尚未执行链接检查）：")
            print(json.dumps(analyzer.link_warnings, ensure_ascii=False, indent=2))
    else:
        print("匹配依据为文件/名称规则，不是编译器符号解析；未处理宏展开和条件编译。")
    if analyzer.metainfo.get("parse_errors"):
        print("存在语法树错误的文件：" + json.dumps(analyzer.metainfo["parse_errors"], ensure_ascii=False))
    try:
        graph = analyzer.get_call_graph(args.api)
    except AmbiguousFunctionError as error:
        print(str(error))
        print(json.dumps({"query": args.api, "candidates": error.candidates}, ensure_ascii=False, indent=2))
        print("请把 -api 的值替换为上面某个完整 function_id（使用引号包住）。")
        return 2

    print("\n基本调用关系（含调用位置）：")
    print(json.dumps(graph, ensure_ascii=False, indent=2))
    if graph["target"] is not None:
        paths = analyzer.get_caller_paths(args.api, args.max_paths)
        print(f"\n调用路径（仅沿已匹配的边，最多 {args.max_paths} 条）：")
        print(json.dumps(paths, ensure_ascii=False, indent=2))
        related = sorted(analyzer.get_related_functions(args.api))
        print(f"\n相关函数（{len(related)} 个；不受路径条数限制）：")
        print(json.dumps(related, ensure_ascii=False, indent=2))
    else:
        print("没有匹配到函数定义；仅展示未匹配引用，不生成虚构的单点函数路径。")
    if args.show_bug:
        analyze_bug_impact(args.api, graph, args.bugs_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
