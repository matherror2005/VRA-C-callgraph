# 整项目调用图接口

已实现两种调用方式：公开 Python 函数和命令行 JSON 导出。两者调用相同解析器、匹配器和图转换器。不需要指定要查询的函数名，也不需要运行 HTTP 服务。

## 1. 最小演示

在仓库根目录运行：

```bash
python3 -B run.py \
  --project-path docs/examples/graph_demo \
  --output results/graph_demo.json
```

示例源码中 process 调用 target 两次。输出有 2 个节点、1 条 process → target 关系边、2 个调用点。此文件是公开接口真正生成的完整结果，不是手写的预期答案。

## 2. 老师提供一个 C 项目

先把项目下载/解压到本地，然后：

```bash
python -B run.py \
  --project-path "/path/to/teacher-project" \
  --output results/teacher-project.json
```

新模式默认 Clang。项目需要相应的头文件和编译参数；不能保证任意未配置的项目都能直接编译分析。复杂项目使用其选定的编译数据库：

```bash
python -B run.py \
  --project-path "/path/to/teacher-project" \
  --compile-commands "/path/to/teacher-project/build/compile_commands.json" \
  --output results/teacher-project.json
```

`--compile-commands` 可为文件或包含它的目录。它决定分析哪些 `.c` 和采用哪些宏、包含路径；未提供时扫描分析目录中的 `.c`，使用 C11 和显式 `--clang-arg`。不会自动运行 CMake/Make 生成数据库。

输入是本地目录，不是 GitHub URL、ZIP 文件或上传表单。当前只支持 C，不会自动分析 `.cpp`、Python 或 Java。接口不下载项目、不安装依赖、不调用项目构建脚本。

## 3. 多 Repo / 构建范围

沿用 v3 的输入布局：分析根目录的第一层源码子目录标识 Repo，根目录的 `.c` 属于 `.`。跨 Repo 外部符号匹配需要清单；project_id 只是输出标签，不会更改 Repo 边界。

```bash
python -B run.py \
  --project-path tests/fixtures/cross_repo \
  --build-manifest tests/fixtures/cross_repo/build.json \
  --output results/cross_repo.json
```

得到 4 个节点、3 条边，其中两条跨 Repo：main → app_entry → api_a → api_b。没有被选中的 lib_other 不参与图。清单和编译数据库可以一起提供。

如果一个项目的源码分布在多个首层目录，且需要匹配这些目录之间的外部引用，可以提供包含这些源码的一个构建范围清单。没有清单时，其他 Repo 的候选会保留为诊断，但不会自动连边。

## 4. Python 接口

在仓库根目录，或将该目录加入 Python 模块搜索路径后：

```python
from api import AnalysisError, generate_call_graph
from graph_export import save_call_graph, validate_call_graph

try:
    graph = generate_call_graph(
        "/path/to/project",
        frontend="clang",
        compile_commands="/path/to/project/build/compile_commands.json",
        # build_manifest="/path/to/selected-build.json",  # 需要跨 Repo 时提供
        project_id="teacher-project",
        revision="supplied-commit",  # 可选；由调用方提供，不自动猜测
    )
    validate_call_graph(graph)  # generate_call_graph 已经做过同样校验
    save_call_graph(graph, "call_graph.json")
    print(len(graph["nodes"]), len(graph["edges"]))
except AnalysisError as error:
    print(error.to_dict())
```

签名：

```python
generate_call_graph(
    project_path,
    *,
    frontend="clang",
    compile_commands=None,
    build_manifest=None,     # JSON 文件路径，或清单字典
    clang_args=(),
    clang="clang",
    timeout=60,              # 每次 Clang 调用的超时，不是整个项目的总超时
    project_id=None,
    revision=None,
) -> dict
```

成功返回 Graph v1 字典，不打印查询报告、不写文件。错误抛出 AnalysisError，包含 code、message 和 to_dict()。save_call_graph 是独立文件写入函数，可能抛出 GraphValidationError、ValueError 或 OSError；调用方需要时另行处理。

## 5. CLI 输入输出约定

- 成功退出码为 0。省略 `--output` 或使用 `--output -` 时，stdout 是单份完整 Graph JSON，可直接重定向。
- 指定 `.json` 输出文件时，stdout 留空，简短完成说明写到 stderr；需要的父目录自动创建。
- 输出先校验、再原子替换；已有结果可被成功的新结果替换。分析失败不会覆盖旧结果或留下部分 JSON。
- 不能将输出设为本次输入的构建清单或编译数据库。
- 正常分析/配置错误退出码为 2，stdout 留空，stderr 返回错误 JSON。
- 未知命令行选项、互斥选项等 argparse 用法错误仍使用标准 usage 文本和退出码 2。
- `-api` 与 `--output` 互斥。指定 `-api` 时保留以前的人工阅读查询输出。

错误示例：

```json
{
  "error": {
    "code": "INVALID_PROJECT",
    "message": "项目目录不存在：/path/to/missing"
  }
}
```

| code | 含义 |
|---|---|
| INVALID_PROJECT | 项目路径无效或不存在 |
| INVALID_OPTIONS | 前端、参数、超时等选项不合法 |
| INVALID_BUILD_MANIFEST | 构建清单无效或与已分析编译单元不一致 |
| NO_C_SOURCE_FILES | 选定范围没有 C 源文件，不伪装成成功的空图 |
| COMPILATION_CONFIGURATION_ERROR | 编译数据库无效或缺少选定源码配置 |
| CLANG_ANALYSIS_FAILED | Clang 不存在、无法运行、超时或拒绝源码配置 |
| FRONTEND_OR_SOURCE_ERROR | Tree-sitter 包/语法库或源码 I/O 失败 |
| INVALID_GRAPH | 转换结果违反数据契约 |
| ANALYSIS_ERROR | 其他已识别的参数或分析数据错误 |
| INVALID_OUTPUT / OUTPUT_ERROR | 输出路径无效或文件写入失败 |

存在 `.c` 但没有函数定义（例如只有声明）时，可返回合法空图，并附 NO_FUNCTION_DEFINITIONS 诊断。未知回调或外部定义缺失不会让整个项目失败：这些调用保留在 call_sites，其目标为 null，不创建虚构节点。

## 6. 返回内容和限制

格式见 [数据契约](CALL_GRAPH_SCHEMA.md) 与 [JSON Schema](../schemas/call-graph-v1.schema.json)。

- nodes 包含具体函数定义，包括没有调用边的函数；同名 static 和头文件不同 TU 实例不会合并。
- edges 按 source、target、kind 去重；所有 matched 调用点各属于一条关系边。
- call_sites 保留每次调用及 matched / unresolved / ambiguous / indirect 状态，不丢失宏展开产生的多次调用。
- diagnostics 保留语法错误、编译器警告、重复符号候选等问题。Tree-sitter 可以返回附带语法错误诊断的部分结果；Clang 拒绝配置时直接返回错误。
- metadata.attributes.summary 提供数量和状态统计；compiler/build_context 记录所用配置，limitations 明确当前分析边界。

仍未实现函数指针目标分析、运行时路径可行性分析和最终链接验证。matched 可表示构建范围内的唯一候选，不保证最终程序采用该实现。可达性结论只针对当前图中的已匹配边。

## 7. 联调示例

[最小调用图](examples/call_graph_v1.json)和[跨仓库调用图](examples/cross_repo.json)可直接读入可达性分析工具，也可用浏览器打开仓库根目录的 `graph_viewer.html` 查看。示例省略生成机器的编译器路径和构建命令，图中的节点、边和调用点与当次分析结果一致。
