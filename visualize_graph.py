"""Export a VRA Graph JSON as a standalone interactive HTML file."""

import argparse
import json
from pathlib import Path

from graph_export import validate_call_graph


VIEWER = Path(__file__).with_name("graph_viewer.html")
DATA_SLOT = '<script type="application/json" id="embedded-graph">null</script>'


def main():
    parser = argparse.ArgumentParser(description="将 VRA 调用图 JSON 转成可离线打开的交互式 HTML")
    parser.add_argument("graph", type=Path, help="VRA 导出的 Graph JSON")
    parser.add_argument("-o", "--output", type=Path, help="输出 HTML，默认与输入文件同名")
    args = parser.parse_args()
    output = args.output or args.graph.with_suffix(".html")
    if output.suffix.lower() != ".html" or output.resolve() in {args.graph.resolve(), VIEWER.resolve()}:
        parser.error("输出必须是单独的 .html 文件，不能覆盖输入或查看器")

    try:
        graph = json.loads(args.graph.read_text(encoding="utf-8"))
        validate_call_graph(graph)
        # Escape '<' so strings from source code cannot end the script data block.
        data = json.dumps(graph, separators=(",", ":"), allow_nan=False).replace("<", "\\u003c")
        html = VIEWER.read_text(encoding="utf-8")
        embedded = DATA_SLOT.replace(">null<", f">{data}<", 1)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(html.replace(DATA_SLOT, embedded, 1), encoding="utf-8")
    except (OSError, ValueError) as error:
        parser.error(str(error))

    print(output.resolve())


if __name__ == "__main__":
    main()
