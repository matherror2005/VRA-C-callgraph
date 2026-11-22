"""读取编译参数；只运行语法分析，不执行构建脚本、不产生对象/依赖文件。"""

import json
from pathlib import Path
import shlex


class CompilationError(ValueError):
    pass


def analysis_arguments(arguments, source, directory):
    result = []
    index = 0
    paired_outputs = {"-o", "-MF", "-MT", "-MQ", "-MJ", "--serialize-diagnostics"}
    switches = {"-c", "-S", "-E", "-emit-llvm", "-M", "-MM", "-MD", "-MMD", "-MP", "-MG"}
    while index < len(arguments):
        arg = arguments[index]
        if arg.startswith("@"):
            raise CompilationError("本实验不展开编译器响应文件 @file；请提供展开后的 arguments")
        if arg in paired_outputs:
            if index + 1 >= len(arguments):
                raise CompilationError(f"编译参数 {arg} 缺少值")
            index += 2
            continue
        if arg in switches or arg.startswith(("-save-temps", "--save-temps", "-ftime-trace")):
            index += 1
            continue
        if any(arg.startswith(prefix) and arg != prefix for prefix in ("-MF", "-MT", "-MQ", "-MJ")):
            index += 1
            continue
        if arg.startswith("-o") and len(arg) > 2:
            index += 1
            continue
        if arg.startswith("-fplugin") or arg in {"-load", "-plugin", "-add-plugin"}:
            raise CompilationError("本实验不加载编译器插件")
        if arg in {"-ast-dump", "-ast-dump=json"}:
            raise CompilationError("请不要在编译数据库中提供 AST 输出选项")
        if not arg.startswith("-") and (directory / arg).resolve() == source:
            index += 1
            continue
        result.append(arg)
        index += 1
    return result


def compiler_arguments(entry):
    if "arguments" in entry:
        args = entry["arguments"]
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise CompilationError("arguments 必须是字符串数组")
    elif "command" in entry:
        args = shlex.split(entry["command"])
    else:
        raise CompilationError("编译命令缺少 arguments 或 command")
    if not args:
        raise CompilationError("编译命令为空")
    # 实际始终使用显式选择的 Clang，而不是执行数据库里的编译器/构建脚本。
    if Path(args[0]).name in {"ccache", "sccache", "distcc"}:
        args = args[1:]
    if not args:
        raise CompilationError("编译器名称缺失")
    return args[1:]


def load_jobs(project_path, compile_commands=None, extra_args=(), source_files=None):
    project = Path(project_path).resolve()
    if not project.is_dir():
        raise CompilationError(f"分析目录不存在：{project}")
    selected = None
    if source_files is not None:
        selected = {(project / source).resolve() for source in source_files}
        if not selected or any(not p.is_relative_to(project) or not p.is_file() or p.suffix != ".c" for p in selected):
            raise CompilationError("所选编译单元必须是项目目录内存在的 .c 文件")
    if compile_commands is None:
        return [{"file": str(source), "directory": str(project),
                 "arguments": ["-std=c11"] + analysis_arguments(list(extra_args), source, project),
                 "configuration_source": "directory_scan_with_explicit_args"}
                for source in sorted(selected if selected is not None else project.rglob("*.c"))]

    database = Path(compile_commands).resolve()
    if database.is_dir():
        database /= "compile_commands.json"
    try:
        entries = json.loads(database.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CompilationError(f"无法读取编译数据库：{error}") from error
    if not isinstance(entries, list):
        raise CompilationError("编译数据库必须是数组")
    jobs = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("file"), str):
            raise CompilationError("每条编译记录必须包含 file")
        directory = Path(entry.get("directory", str(database.parent)))
        if not directory.is_absolute():
            directory = database.parent / directory
        directory = directory.resolve()
        source = (directory / entry["file"]).resolve()
        if source.suffix != ".c" or not source.is_relative_to(project):
            continue
        if selected is not None and source not in selected:
            continue
        if not source.is_file() or not directory.is_dir():
            raise CompilationError(f"编译记录指向不存在的路径：{source}")
        args = analysis_arguments(compiler_arguments(entry) + list(extra_args), source, directory)
        job = {"file": str(source), "directory": str(directory), "arguments": args,
               "configuration_source": str(database)}
        if source in jobs and jobs[source]["arguments"] != args:
            raise CompilationError(f"同一源文件有多个编译配置，请先选择一个配置：{source}")
        jobs[source] = job
    if not jobs:
        raise CompilationError("编译数据库中没有属于分析目录的 C 文件")
    if selected is not None and selected - jobs.keys():
        missing = sorted(str(path.relative_to(project)) for path in selected - jobs.keys())
        raise CompilationError(f"编译数据库缺少构建清单选定的源码：{missing}")
    return [jobs[source] for source in sorted(jobs)]
