"""
For each yield, find all paths from other yields that lead into it.

    For each path,
        Create a queue of blocks to process, where a block is in the queue only if
        all blocks leading into it have been processed, or the block leading into it
        is a Yield

        Initialize the queue with the blocks following the start yields (that
        is, the blocks following the yields that start a path ending in the
        current end yield).
            Note: there could be a block that immediately follows a yield, but
                  also has a path from another block, that may itself, come
                  from another yield. In this case, the block should only be
                  added to queue *after* the block leading into it has been
                  processed, so even though it's an immediate successor of a
                  yield, it may not go in the initial queue.

        For each block, if a variable is read, check its predecessor:
            If the predecessor is a yield, perform a load from any live
            variables coming into the path into a fresh temporary variable,
            e.g. `a_3 = a`.

            If the predecessor is a basic block, get the latest ssa values of
            assigned variables, use this to set the current state of the ssa
            variable replacer.

            If it has multiple predecessors, insert a phi node to select the
            correct predecessor.  The condition of the phi node (mux) are,
            select this value if the start yield was one of the yields starting
            a path leading into the predecssor and the result of branches match
            the edges along the path in the control flow graph from the start
            yields.

                for each predecessor:
                    conds = []
                    for each path leading into predecessor:
                        conds.append(yield_state == start_yield_id &
                                     (cond_1 == T) & (cond_2 == F) & ...)
                    take predecessor variables if reduce(|, conds)

        Once we reach the end yield, perform a store with the latest assigned
        temporary variables for each live out.
"""
import ast
import astor
import copy

from collections import defaultdict
from silica.cfg.types import BasicBlock, Yield, Branch, HeadBlock, State


def parse_expr(expr):
    return ast.parse(expr).body[0].value



def parse_stmt(statement):
    return ast.parse(statement).body[0]


class SSAReplacer(ast.NodeTransformer):
    def __init__(self, width_table):
        self.width_table = width_table
        self.id_counter = {}
        self.phi_vars = {}
        self.load_store_offset = {}
        self.seen = set()
        self.array_stores = {}
        self.index_map = {}
        self.array_store_processed = set()

    def increment_id(self, key):
        if key not in self.id_counter:
            self.id_counter[key] = 0
        else:
            self.id_counter[key] += 1

    def get_name(self, node):
        if isinstance(node, ast.Subscript):
            return self.get_name(node.value)
        elif isinstance(node, ast.Name):
            return node.id
        else:
            raise NotImplementedError("Found assign to subscript that isn't of the form name[x][y]...[z]")

    def get_index(self, node):
        if isinstance(node.slice, ast.Index):
            index = node.slice.value
        else:
            raise NotImplementedError(node.slice, type(node.slice))
        if isinstance(node.value, ast.Subscript):
            return (index, ) + self.get_index(node.value)
        return (index, )

    def visit_Assign(self, node):
        node.value = self.visit(node.value)
        assert len(node.targets) == 1
        if isinstance(node.targets[0], ast.Subscript):
            assert isinstance(node.targets[0].value, ast.Name)
            node.targets[0].slice = self.visit(node.targets[0].slice)
            store_offset = self.load_store_offset.get(node.targets[0].value.id, 0)
            name = self.get_name(node.targets[0])
            prev_name = name + f"_{self.id_counter[name] - store_offset}"
            name += f"_{self.id_counter[name] + store_offset}"
            index = self.get_index(node.targets[0])
            if (name, index) not in self.array_stores:
                self.array_stores[name, index] = (0, prev_name)
                num = 0
            else:
                val = self.array_stores[name, index]
                num = val[0] + 1
                self.array_stores[name, index] = (num, val[1])
            index_hash = "_".join(ast.dump(i) for i in index)
            if index_hash not in self.index_map:
                self.index_map[index_hash] = len(self.index_map)
            node.targets[0].value.id = f"{name}_si_tmp_val_{num}_i{self.index_map[index_hash]}"
            node.targets[0] = node.targets[0].value
            node.targets[0].ctx = ast.Store()
            # self.increment_id(name)
            # if name not in self.seen:
            #     self.increment_id(name)
            #     self.seen.add(name)
            # node.targets[0].value.id += f"_{self.id_counter[name] + store_offset}"
        else:
            node.targets[0] = self.visit(node.targets[0])
        return node

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            # phi_args = []
            # for block, _ in self.curr_block.incoming_edges:
            #     phi_args.append(ast.Name(f"{node.id}_{block.id_counter[node.id]}", ast.Load()))
            # if len(phi_args):
            #     return ast.Call(ast.Name("phi", ast.Load()), phi_args, [])
            if node.id in self.id_counter:
                load_offset = self.load_store_offset.get(node.id, 0)
                node.id += f"_{self.id_counter[node.id] - load_offset}"
            return node
        else:
            self.increment_id(node.id)
            # if node.id not in self.seen:
            #     self.increment_id(node.id)
            #     self.seen.add(node.id)
            store_offset = self.load_store_offset.get(node.id, 0)
            self.width_table[f"{node.id}_{self.id_counter[node.id]}"] = self.width_table[node.id]
            return ast.Name(f"{node.id}_{self.id_counter[node.id] + store_offset}", ast.Store())


class Replacer(ast.NodeTransformer):
    def __init__(self, var_to_curr_id_map, stores, width_table):
        super().__init__()
        self.var_to_curr_id_map = var_to_curr_id_map
        self.stores = stores
        self.width_table = width_table

    def visit_Assign(self, node):
        node.value = self.visit(node.value)
        node.targets = [self.visit(target) for target in node.targets]
        return node

    def visit_Name(self, node):
        if node.id in ["uint"]:
            return node
        orig_id = node.id
        if isinstance(node.ctx, ast.Store):
            self.var_to_curr_id_map[orig_id] += 1
        node.id += f"_{self.var_to_curr_id_map[node.id]}"
        if isinstance(node.ctx, ast.Store):
            self.stores[orig_id] = node.id
            self.width_table[node.id] = self.width_table[orig_id]
        return node


def get_conds_up_to(path, predecessor):
    conds = []
    for i, block in enumerate(path):
        if isinstance(block, Yield):
            conds.append(f"yield_state == {block.yield_id}")
        elif isinstance(block, HeadBlock):
            conds.append(f"yield_state == 0")
        elif isinstance(block, Branch):
            cond = block.cond
            if path[i + 1] is block.false_edge:
                cond = ast.UnaryOp(ast.Invert(), cond)
            conds.append(astor.to_source(cond).rstrip())
        if block == predecessor:
            break

    result = conds[0]
    for cond in conds[1:]:
        result = f"({result}) & ({cond})"
    return "(" + result + ")"
    # return " & ".join(conds)


def convert_to_ssa(cfg):
    replacer = SSAReplacer(cfg.width_table)
    yield_to_paths_map = defaultdict(lambda: [])
    for path in cfg.paths:
        yield_to_paths_map[path[-1]].append(path)

    processed = set()
    var_to_curr_id_map = defaultdict(lambda: 0)
    for end_yield, paths in yield_to_paths_map.items():
        blocks_to_process = []
        for path in paths:
            if isinstance(path[0], HeadBlock) and path[0] not in blocks_to_process:
                blocks_to_process.append(path[0])
            elif all(isinstance(edge, Yield) or edge in blocks_to_process for edge, _ in path[1].incoming_edges):
                if path[1] not in blocks_to_process:
                    blocks_to_process.append(path[1])
        while blocks_to_process:
            block = blocks_to_process.pop(0)
            processed.add(block)
            loads = []

            block._ssa_stores = {}
            phi_vars = set()
            if len(block.incoming_edges) == 1:
                predecessor, _ = next(iter(block.incoming_edges))
                if isinstance(predecessor, (HeadBlock, Yield)):
                    for var in block.live_ins:
                        var_to_curr_id_map[var] += 1
                        ssa_var = f"{var}_{var_to_curr_id_map[var]}"
                        cfg.width_table[ssa_var] = cfg.width_table[var]
                        loads.append(parse_stmt(f"{ssa_var} = {var}"))
                        block._ssa_stores[var] = ssa_var
                else:
                    for store, value in predecessor._ssa_stores.items():
                        if store not in block._ssa_stores:
                            block._ssa_stores[store] = value
            elif len(block.incoming_edges) > 1:
                for var in block.live_ins:
                    to_mux = []
                    phi_values = []
                    phi_conds = []
                    for predecessor, _ in block.incoming_edges:
                        if var in predecessor.live_outs:
                            phi_vars.add(var)
                            if isinstance(predecessor, (HeadBlock, Yield)):
                                var_to_curr_id_map[var] += 1
                                ssa_var = f"{var}_{var_to_curr_id_map[var]}"
                                loads.append(parse_stmt(f"{ssa_var} = {var}"))
                                cfg.width_table[ssa_var] = cfg.width_table[var]
                                block._ssa_stores[var] = ssa_var
                                phi_values.append(f"{ssa_var}")
                            else:
                                to_mux.append(predecessor)
                                phi_values.append(f"{predecessor._ssa_stores[var]}")
                            conds = []
                            for path in cfg.paths:
                                if predecessor in path and block in path and path.index(predecessor) == path.index(block) - 1:
                                    conds.append(get_conds_up_to(path, predecessor))
                            # phi_conds.append(" | ".join(conds))
                            result = conds[0]
                            for cond in conds[1:]:
                                result = f"({result}) | ({cond})"
                            phi_conds.append(result)

                    var_to_curr_id_map[var] += 1
                    ssa_var = f"{var}_{var_to_curr_id_map[var]}"
                    loads.append(parse_stmt(f"{ssa_var} = phi([{', '.join(phi_conds)}], [{', '.join(phi_values)}])"))
                    block._ssa_stores[var] = ssa_var
                    cfg.width_table[ssa_var] = cfg.width_table[var]
                    for predecessor, _ in block.incoming_edges:
                        if not isinstance(predecessor, (HeadBlock, Yield)):
                            for store, value in predecessor._ssa_stores.items():
                                if store not in block._ssa_stores:
                                    block._ssa_stores[store] = value

            replacer = Replacer(var_to_curr_id_map, block._ssa_stores, cfg.width_table)
            if isinstance(block, BasicBlock):
                for statement in block.statements:
                    replacer.visit(statement)
                block.statements = loads + block.statements
            elif isinstance(block, Branch):
                block.cond = replacer.visit(block.cond)

            for successor, _ in block.outgoing_edges:
                if successor in processed or successor in blocks_to_process:
                    continue
                if all(isinstance(edge, Yield) or edge in processed for edge, _ in successor.incoming_edges):
                    blocks_to_process.append(successor)
                if isinstance(successor, Yield):
                    assert len(block.outgoing_edges) == 1
                    assert isinstance(block, (BasicBlock, HeadBlock)), block
                    for var in successor.live_ins:
                        ssa_var = f"{var}_{var_to_curr_id_map[var]}"
                        block.statements.append(parse_stmt(f"{var} = {ssa_var}"))
    return var_to_curr_id_map
