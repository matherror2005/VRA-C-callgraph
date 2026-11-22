"""
工具函数模块 - 提供 Tree-sitter 构建、文件操作和 AST 节点处理功能
"""
import logging
import os
import subprocess
from typing import Dict, List, Optional, Set

import tree_sitter

from config import *

#todo：函数返回判断是根据目录是否存在；但是目录存在！=目录不为空！=库已构建
def build_tree_sitter_c() -> None: #不返回分析结果，主要通过创建目录、下载和编译完成工作
    """
    构建 tree-sitter C 解析器库
    如果库不存在，自动克隆仓库并编译
    """
    if not os.path.exists(C_LIB_PATH): #检查C_LIB_PATH 是否存在 VRA-original/build
        print(f"正在构建 tree-sitter-c 库...")
        try:
            os.makedirs(LIB_PATH, exist_ok=True) #创建build/目录：exist_ok表示目录已存在时不报错
            subprocess.run( #启动外部命令，下载C语法库源码
                ["git", "clone", TREE_SITTER_C_URL],
                cwd=LIB_PATH, #在build/目录中执行命令，通常会生成build/tree-sitter-c/
                check=True #等待命令结束；如果推出码非零 就抛出
            )
            #进入刚下载的tree-sitter-c/目录，执行make
            subprocess.run(["make"], cwd=C_LIB_PATH, check=True)
            #没有检查文件是否实际生成，前面两条check=True就返回print
            print("tree-sitter-c 库构建完成")
        #如果git clone或make返回非零推出码
        except subprocess.CalledProcessError as e:
            error_msg = f"构建 tree-sitter 失败: {e}" #生成错误
            print(error_msg) #在终端打印错误
            logging.error(error_msg)#在日志记录错误
            raise #重新抛出异常，让上层知道构建失败


def read_file(file_path: str) -> Optional[str]:
    """
    读取文件内容

    Args:
        file_path: 文件路径

    Returns:
        文件内容字符串，读取失败返回 None
    """
    try:
        with open(file_path, "r", encoding="utf-8") as file:
            return file.read()
    except Exception as e:
        error_msg = f"无法读取文件 {file_path}: {e}"
        logging.error(error_msg)
        return None


def get_node_text(node: tree_sitter.Node) -> str:
    """
    获取 AST 节点的源代码文本

    Args:
        node: Tree-sitter AST 节点

    Returns:
        节点对应的源代码字符串
    """
    return node.text.decode("utf-8") if hasattr(node, "text") else ""


def extract_function_calls(node: tree_sitter.Node) -> List[str]:
    """
    从函数体中提取所有被调用的函数名

    Args:
        node: 函数体 AST 节点

    Returns:
        被调用函数名列表（已去重）
    """
    callees: Set[str] = set()

    def walk_node(n: tree_sitter.Node) -> None:
        """递归遍历节点查找函数调用"""
        if n.type == "call_expression":
            func_node = n.child_by_field_name("function")
            if func_node and func_node.type == "identifier":
                callees.add(get_node_text(func_node))

        for child in n.children:
            walk_node(child)

    walk_node(node)
    return list(callees)


def parse_function_declarator(node: tree_sitter.Node) -> Dict[str, any]:
    """
    解析函数声明器，提取函数信息

    Args:
        node: function_declarator AST 节点

    Returns:
        包含函数名、返回类型、参数的字典
    """
    if node.type != "function_declarator":
        return {}

    # 获取返回类型（从前一个兄弟节点）
    return_type = ""
    prev_sibling = node.prev_sibling
    if prev_sibling and prev_sibling.type == "*":
        prev_sibling = prev_sibling.prev_sibling

    if prev_sibling and prev_sibling.type in {
        "primitive_type", "type_identifier", "sized_type_specifier"
    }:
        return_type = get_node_text(prev_sibling)

    # 解析函数名和参数
    function_name = ""
    parameters = []

    for child in node.children:
        if child.type in {"identifier", "field_identifier"}:
            function_name = get_node_text(child)
        elif child.type == "parameter_list":
            parameters = _parse_parameters(child)

    return {
        "name": function_name,
        "return_type": return_type,
        "parameters": parameters,
    }


def parse_function_definition(node: tree_sitter.Node) -> Dict[str, any]:
    """
    解析函数定义，提取完整函数信息

    Args:
        node: function_definition AST 节点

    Returns:
        包含函数完整信息的字典
    """
    if node.type != "function_definition":
        return {}

    function_info = {}
    function_body = ""
    callees = []

    # 解析子节点
    for child in node.children:
        if child.type == "function_declarator":
            function_info = parse_function_declarator(child)
        elif child.type == "pointer_declarator":
            # 处理指针函数
            for grandchild in child.children:
                if grandchild.type == "function_declarator":
                    function_info = parse_function_declarator(grandchild)
                    break
        elif child.type == "compound_statement":
            function_body = get_node_text(child)
            callees = extract_function_calls(child)

    # 处理返回类型为空的情况（可能是宏定义）
    if not function_info.get("return_type"):
        prev_node = node.prev_sibling
        if prev_node:
            function_info["return_type"] = get_node_text(prev_node)

    return {
        **function_info,
        "body": function_body,
        "callees": callees,
        "scope": _get_scope(node),
    }


def _parse_parameters(param_list_node: tree_sitter.Node) -> List[Dict[str, str]]:
    """
    解析参数列表

    Args:
        param_list_node: parameter_list AST 节点

    Returns:
        参数信息列表
    """
    parameters = []

    for param_decl in param_list_node.children:
        if param_decl.type == "parameter_declaration":
            param_info = {}
            for param_child in param_decl.children:
                if param_child.type in {
                    "primitive_type", "type_identifier",
                    "sized_type_specifier", "qualified_identifier"
                }:
                    param_info["type"] = get_node_text(param_child)
                elif param_child.type in {
                    "identifier", "field_identifier", "reference_declarator"
                }:
                    param_info["name"] = get_node_text(param_child)

            if param_info:
                parameters.append(param_info)

    return parameters


def _get_scope(node: tree_sitter.Node) -> str:
    """
    获取函数所属的作用域（类/结构体名或全局）

    Args:
        node: 函数定义节点

    Returns:
        作用域名称
    """
    parent = node.parent
    while parent:
        if parent.type in {"struct_specifier", "class_specifier"}:
            name_node = parent.child_by_field_name("name")
            if name_node:
                return get_node_text(name_node)
            break
        parent = parent.parent

    return "global"