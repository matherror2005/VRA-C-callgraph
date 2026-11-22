"""提取调用点和可识别的词法绑定；不做函数指针指向分析。"""


def text(node):
    return node.text.decode("utf-8")


def declarator_chain(node):
    """从名字向外返回声明器链，区分函数与函数指针变量。"""
    if node is None:
        return []
    if node.type in {"identifier", "field_identifier"}:
        return [node]
    child = node.child_by_field_name("declarator")
    if child is None:
        child = next((item for item in node.named_children
                      if item.type in {"identifier", "field_identifier"}
                      or item.type.endswith("declarator")), None)
    return declarator_chain(child) + [node] if child is not None else []


def declared_name_and_kind(node):
    chain = declarator_chain(node)
    if not chain:
        return None, "unknown"
    operation = next((part.type for part in chain[1:]
                      if part.type in {"function_declarator", "pointer_declarator", "array_declarator"}), None)
    return text(chain[0]), "function" if operation == "function_declarator" else "object"


def definition_name(node):
    name, _ = declared_name_and_kind(node.child_by_field_name("declarator"))
    return name


def has_static(node):
    return any(child.type == "storage_class_specifier" and text(child) == "static"
               for child in node.named_children)


def file_scope_items(root):
    """遍历文件级声明（包括预处理分支），不进入函数体和结构体字段。"""
    for child in root.named_children:
        if child.type in {"function_definition", "declaration", "type_definition"}:
            yield child
        elif child.type.startswith("preproc_"):
            yield from file_scope_items(child)


def declaration_bindings(node, origin):
    result = {}
    for declarator in node.children_by_field_name("declarator"):
        name, kind = declared_name_and_kind(declarator)
        if name:
            result[name] = {
                "kind": "typedef" if node.type == "type_definition" else kind,
                "origin": origin,
                "line_number": declarator.start_point[0] + 1,
                "is_static": kind == "function" and has_static(node),
            }
    return result


def parameter_bindings(function_node):
    chain = declarator_chain(function_node.child_by_field_name("declarator"))
    declarator = next((part for part in chain if part.type == "function_declarator"), None)
    if declarator is None:
        return {}
    parameters = declarator.child_by_field_name("parameters")
    result = {}
    if parameters is not None:
        for parameter in parameters.named_children:
            if parameter.type == "parameter_declaration":
                name, _ = declared_name_and_kind(parameter.child_by_field_name("declarator"))
                if name:
                    result[name] = {"kind": "object", "origin": "parameter",
                                    "line_number": parameter.start_point[0] + 1}
    return result


def extract_call_sites(function_node, file_bindings):
    """每次调用生成一条记录；列号是从 1 开始的 UTF-8 字节列。"""
    body = function_node.child_by_field_name("body")
    if body is None:
        return []
    scopes = [file_bindings, parameter_bindings(function_node)]
    calls = []

    def binding_for(name):
        for scope in reversed(scopes):
            if name in scope:
                return scope[name]
        return None

    def walk(node):
        if node.type == "function_definition":
            return
        if node.type in {"compound_statement", "for_statement"}:
            scopes.append({})
            for child in node.named_children:
                walk(child)
            scopes.pop()
            return
        if node.type in {"declaration", "type_definition"}:
            declarators = node.children_by_field_name("declarator")
            ranges = {(item.start_byte, item.end_byte) for item in declarators}
            for child in node.named_children:
                if (child.start_byte, child.end_byte) in ranges:
                    name, kind = declared_name_and_kind(child)
                    if name:
                        inherited = binding_for(name)
                        scopes[-1][name] = {
                            "kind": "typedef" if node.type == "type_definition" else kind,
                            "origin": "local", "line_number": child.start_point[0] + 1,
                            "is_static": kind == "function" and (has_static(node)
                                          or bool(inherited and inherited.get("is_static"))),
                        }
                walk(child)
            return
        if node.type == "call_expression":
            callee = node.child_by_field_name("function")
            name = text(callee) if callee is not None and callee.type == "identifier" else None
            binding = binding_for(name) if name else None
            indirect = name is None or (binding is not None and binding["kind"] != "function")
            calls.append({
                "expression": text(node),
                "callee_expression": text(callee) if callee is not None else "",
                "callee_name": name,
                "syntax_kind": callee.type if callee is not None else "unknown",
                "line_number": node.start_point[0] + 1,
                "column": node.start_point[1] + 1,
                "end_line_number": node.end_point[0] + 1,
                "end_column": node.end_point[1] + 1,
                "start_byte": node.start_byte,
                "end_byte": node.end_byte,
                "binding": dict(binding) if binding is not None else None,
                "resolution": "indirect" if indirect else "pending",
                "reason": ("non_identifier_callee" if name is None else
                           "visible_non_function_binding" if indirect else "not_matched_yet"),
                "callee_id": None,
                "candidate_ids": [],
            })
        for child in node.named_children:
            walk(child)

    walk(body)
    return sorted(calls, key=lambda call: call["start_byte"])
