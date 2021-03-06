import ast
from .width import get_width
import silica.ast_utils as ast_utils


class CollectInitialWidthsAndTypes(ast.NodeVisitor):
    def __init__(self, width_table, type_table, func_locals, func_globals):
        self.width_table = width_table
        self.type_table = type_table
        self.func_locals = func_locals
        self.func_globals = func_globals

    def visit_Assign(self, node):
        if len(node.targets) == 1:
            if isinstance(node.targets[0], ast.Name):
                if isinstance(node.value, ast.Yield):
                    pass  # width specified at compile time
                elif isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute) and \
                     node.value.func.attr == "send":
                    pass
                elif node.targets[0].id not in self.width_table:
                    self.width_table[node.targets[0].id] = get_width(node.value, self.width_table, self.func_locals, self.func_globals)
                    if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) and \
                       node.value.func.id in {"bits", "uint", "bit"}:
                        self.type_table[node.targets[0].id] = node.value.func.id
                    elif isinstance(node.value, ast.NameConstant) and node.value.value in [True, False]:
                        self.type_table[node.targets[0].id] = "bit"
                    elif isinstance(node.value, ast.Name) and node.value.id in self.type_table:
                        self.type_table[node.targets[0].id] = self.type_table[node.value.id]


class SubCoroutineCollector(ast.NodeVisitor):
    def __init__(self):
        self.sub_coroutines = {}

    def visit_Assign(self, node):
        if ast_utils.is_call(node.value) and ast_utils.is_name(node.value.func) and node.value.func.id == "coroutine_create":
            assert len(node.value.args[0].id)
            self.sub_coroutines[node.targets[0].id] = node.value.args[0].id

def collect_sub_coroutines(tree):
    collector = SubCoroutineCollector()
    collector.visit(tree)
    return collector.sub_coroutines
