"""
项目配置文件 - 定义所有路径常量和配置信息
"""
from pathlib import Path

# 基础路径配置
PROJECT_PATH = Path(__file__).resolve().parent

# Tree-sitter C 解析器配置
LIB_PATH = PROJECT_PATH / "build"
C_LIB_NAME = "libtree-sitter-c.so"
C_LIB_PATH = LIB_PATH / "tree-sitter-c"
TREE_SITTER_C_URL = "git@github.com:tree-sitter/tree-sitter-c.git"

# 工作目录配置
WORKSPACE_PATH = PROJECT_PATH / "workspace"

# 数据文件配置
BUGS_PATH = PROJECT_PATH / "bugs.json"
