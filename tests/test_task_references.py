"""No fire-and-forget task may be left unreferenced.

asyncio keeps only a weak reference to a running task: one created as a bare
statement can be garbage-collected mid-run, and its exception is reported at
collection time if at all. Field review (2026-10-15) found eleven — the worst,
in cfsolver, could strand a host in its in-flight set so it was never solved
again until a restart. Hold the task (a set plus a done-callback) instead.
"""

import ast
import pathlib
import unittest

APP = pathlib.Path(__file__).resolve().parents[1] / "app"
_SPAWNERS = {"create_task", "ensure_future"}


def _discarded_spawns(path: pathlib.Path) -> list[int]:
    tree = ast.parse(path.read_text(), filename=str(path))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name in _SPAWNERS:
            out.append(node.lineno)
    return out


class TaskReferenceTests(unittest.TestCase):
    def test_no_task_is_created_and_dropped(self):
        found = {str(p.relative_to(APP.parent)): lines
                 for p in sorted(APP.rglob("*.py"))
                 if (lines := _discarded_spawns(p))}
        self.assertEqual({}, found,
                         "keep a reference to every background task")


if __name__ == "__main__":
    unittest.main()
