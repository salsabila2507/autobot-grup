"""Static guards for whole classes of bug that only showed up at runtime.

Most of the defects found in this project were not logic errors. They were typos and
blind text substitutions: a rename that missed one of nine call sites, a blanket
replacement that injected an argument into a function that never had it. The test suite
passed straight through all of them because none of them is a logic error at all.

These checks read the source instead of running it, so a mistake like that fails here
rather than in the group.
"""

import ast
import builtins
from pathlib import Path

BUILTINS = set(dir(builtins))
ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIRS = ("engine", "services", "sim")

# Names that a blanket msg.reply -> reply_cmd replacement could have injected.
INJECTABLE = {"group", "circle", "msg", "parts", "intent", "handle", "tweet_id"}


def _py_files():
    for d in SOURCE_DIRS:
        for path in sorted((ROOT / d).rglob("*.py")):
            yield path.relative_to(ROOT), path


def _text_exports():
    tree = ast.parse((ROOT / "engine" / "text.py").read_text())
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            names.add(node.targets[0].id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
    return names


def _imports_text_module(path) -> bool:
    """True only when `text` in this file means engine.text.

    A local variable called `text` shadows nothing, and flagging text.split on one
    produced failures on a file that never imported the module at all.
    """
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module == "engine":
            if any(a.name == "text" for a in node.names):
                return True
        if isinstance(node, ast.Import):
            if any(a.name == "engine.text" for a in node.names):
                return True
    return False


def test_every_text_reference_exists():
    """A renamed constant that missed one call site is an AttributeError at runtime."""
    exported = _text_exports()
    missing = []
    for rel, path in _py_files():
        if not _imports_text_module(path):
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id == "text" and node.attr not in exported):
                missing.append(f"{rel}:{node.lineno} text.{node.attr}")
    assert not missing, "text.* that does not exist:\n  " + "\n  ".join(sorted(set(missing)))


def test_no_function_uses_a_name_it_never_defines():
    """Catches an argument or variable that was spliced into a function body.

    Nested functions and closures are handled properly: a nested def may read names
    from the scope around it, and a name defined only as a parameter of a nested def
    must not be reported against the enclosing function. Getting this wrong produces
    false alarms, and "fixing" those would break working code.
    """
    module_globals = set()

    def collect_module_globals(path):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    module_globals.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                module_globals.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        module_globals.add(t.id)

    for _rel, _path in _py_files():
        collect_module_globals(_path)

    def enclosing_scopes(fn, all_fns):
        """Names visible from the scopes wrapped around this function."""
        visible = set(module_globals)
        for outer in all_fns:
            if outer is fn or outer.lineno > fn.lineno:
                continue
            if outer.end_lineno and outer.end_lineno < fn.lineno:
                visible |= own_names(outer)
        return visible

    def own_names(fn):
        names = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
        if fn.args.vararg:
            names.add(fn.args.vararg.arg)
        if fn.args.kwarg:
            names.add(fn.args.kwarg.arg)
        for node in ast.walk(fn):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names.add(node.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    names.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, ast.ExceptHandler) and node.name:
                names.add(node.name)
        return names

    problems = []
    for rel, path in _py_files():
        tree = ast.parse(path.read_text())
        all_fns = [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for fn in all_fns:
            defined = own_names(fn) | enclosing_scopes(fn, all_fns)
            used = {n.id for n in ast.walk(fn)
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
            for name in sorted((used - defined - BUILTINS) & INJECTABLE):
                problems.append(f"{rel}:{fn.lineno} {fn.name}() uses {name!r} but never defines it")
    assert not problems, "undefined names:\n  " + "\n  ".join(problems)


def test_every_reply_cmd_call_matches_its_signature():
    """reply_cmd dropped link_preview, and a caller still passed it."""
    tree = ast.parse((ROOT / "services" / "queue_bot.py").read_text())
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "QueueBot")
    fn = next(n for n in cls.body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "reply_cmd")
    allowed = {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}
    bad = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "reply_cmd"):
            extra = {k.arg for k in node.keywords if k.arg} - allowed
            if extra:
                bad.append(f"line {node.lineno}: unexpected {sorted(extra)}")
    assert not bad, "reply_cmd called with unsupported arguments: " + "; ".join(bad)


def test_command_handlers_receive_group():
    """Every admin handler that answers must be handed the group it belongs to."""
    tree = ast.parse((ROOT / "services" / "queue_bot.py").read_text())
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "QueueBot")
    handlers = [n for n in cls.body
                if isinstance(n, ast.AsyncFunctionDef) and n.name.startswith("cmd_")]
    missing = []
    for fn in handlers:
        body = ast.dump(ast.Module(body=fn.body, type_ignores=[]))
        uses_reply = "reply_cmd" in body or "'reply'" in body
        has_group = "group" in {a.arg for a in fn.args.args}
        if uses_reply and not has_group:
            missing.append(fn.name)
    assert not missing, f"handlers that answer but never receive group: {missing}"


def test_every_command_has_a_handler_or_is_known_ignored():
    """A command with no handler used to vanish without a word."""
    tree = ast.parse((ROOT / "services" / "queue_bot.py").read_text())
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "QueueBot")
    dispatch = next(n for n in cls.body
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "on_command")
    has_unknown_branch = any(
        isinstance(node, ast.If) and node.orelse for node in ast.walk(dispatch)
    )
    assert has_unknown_branch, "on_command has no unknown-command branch"
    body = ast.dump(ast.Module(body=dispatch.body, type_ignores=[]))
    for handler in ("cmd_del", "cmd_label", "cmd_vip", "cmd_cleared",
                    "cmd_shift", "cmd_health", "cmd_alt", "cmd_check"):
        assert f"'{handler}'" in body, f"{handler} is never dispatched"


def test_no_module_imports_a_name_that_is_gone():
    """A renamed or deleted function would break at import time on the next restart."""
    missing = []
    for rel, path in _py_files():
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            target = ROOT / (node.module.replace(".", "/") + ".py")
            if not target.exists():
                continue
            exported = set()
            for n in ast.walk(ast.parse(target.read_text())):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    exported.add(n.name)
                elif isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name):
                    exported.add(n.targets[0].id)
                elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
                    exported.add(n.target.id)
            for alias in node.names:
                if alias.name != "*" and alias.name not in exported:
                    missing.append(f"{rel}:{node.lineno} {node.module}.{alias.name}")
    assert not missing, "imports that do not resolve:\n  " + "\n  ".join(sorted(set(missing)))


def test_health_only_reports_services_that_exist():
    """x_reader was removed because its GraphQL path could not authenticate, yet
    /health kept listing it, so it always showed DOWN and read as a live fault."""
    import re

    src = (ROOT / "services" / "queue_bot.py").read_text()
    listed = re.findall(r'for svc in \(([^)]*)\)', src)
    assert listed, "the health service list was not found"
    for entry in listed:
        names = set(re.findall(r'"(\w+)"', entry))
        assert "x_reader" not in names, f"/health still lists the removed service: {names}"

    written = set()
    for rel, path in _py_files():
        if "services" not in str(rel):
            continue
        tree = ast.parse(path.read_text())
        # f"hb:{svc}" is a read, and an f-string splits into literal pieces, so those
        # constants have to be excluded or they look like a heartbeat being written.
        inside_fstring = set()
        for joined in ast.walk(tree):
            if isinstance(joined, ast.JoinedStr):
                inside_fstring.update(id(c) for c in ast.walk(joined)
                                      if isinstance(c, ast.Constant))
        for node in ast.walk(tree):
            if id(node) in inside_fstring:
                continue
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
               and node.value.startswith("hb:") and len(node.value) > 3:
                written.add(node.value[3:])

    assert written == {"queue_bot", "userbot", "scheduler"}, written
