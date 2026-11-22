# VRA-C-callgraph
C function call graph generator with JSON export and an interactive viewer.

输入本地 C 项目，输出记录具体函数、调用关系和源码位置的 Graph JSON。下游可以按函数 ID 查询路径，也可以在浏览器里查看同一张图。

## 快速试用

在仓库根目录运行。默认的 Clang 分析和 HTML 导出使用 Python 3.10+ 与本机的 `clang`，无需安装 Python 第三方包。

```bash
python3 -B run.py --project-path docs/examples/graph_demo --output results/demo.json
python3 -B visualize_graph.py results/demo.json -o results/demo.html
```

用浏览器打开 `results/demo.html`，可以看到两个函数和一条 `process → target` 的边。点击边可看到源码中的两次调用。直接打开 [graph_viewer.html](graph_viewer.html) 也可以选择已有的 Graph JSON。

分析自己的项目时，把 `--project-path` 换成本地源码目录。项目依赖特定头文件、宏或编译参数时，传入对应的 `--compile-commands`。需要跨仓库匹配时，通过 `--build-manifest` 指定目标和依赖：

```bash
python3 -B run.py \
  --project-path tests/fixtures/cross_repo \
  --build-manifest tests/fixtures/cross_repo/build.json \
  --output results/cross_repo.json
```

预期路径为 `main → app_entry → api_a → api_b`，含两条跨仓库边。仓库中的 [最小示例图](docs/examples/call_graph_v1.json) 和 [跨仓库示例图](docs/examples/cross_repo.json) 可直接用于联调。示例保留图结构和构建范围，省略生成机器的编译器路径与命令记录。

## 给可达性分析同学

- `nodes[].id` 标识图里的具体函数，函数名可以重复。
- `edges[].source → target` 是调用者到被调用者。用 `call_site_ids` 在 `call_sites` 中查源码位置。
- [数据结构](docs/CALL_GRAPH_SCHEMA.md)、[JSON Schema](schemas/call-graph-v1.schema.json) 和 [Python / CLI 接口](docs/PROJECT_API.md) 说明字段及使用方式。

本版生成的是所选构建配置下的静态图。未解析的函数指针、回调或歧义调用保留在 `call_sites`，只有已匹配的调用进入 `edges`。跨仓库边可来自构建范围内的唯一候选，最终链接和运行时条件还需独立验证。Tree-sitter 前端作为对照保留，使用时需要兼容的 Python 绑定和预先准备好的 C 语法库。

## 上游

本项目基于 [HoloScope-Buaa/VRA](https://github.com/HoloScope-Buaa/VRA) 开发，增加具体函数身份、Clang 分析、跨仓库候选匹配、Graph JSON 导出和交互查看器。
