import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_trace_view_lives_in_runtime_so_the_exporter_can_import_it():
    assert (REPO / "app/runtime/trace_view.py").is_file()
    assert not (REPO / "app/web/trace_view.py").exists()


def test_runtime_trace_view_imports_no_web_module():
    source = (REPO / "app/runtime/trace_view.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("app.web"), node.module
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("app.web"), alias.name
