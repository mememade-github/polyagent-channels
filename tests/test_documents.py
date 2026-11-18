"""Committed operator instructions must match their runtime and Compose owners."""
import runpy
from pathlib import Path


def test_documents_match_definitions():
    location = Path(__file__).resolve()
    root = location.parent.parent
    renderer = runpy.run_path(str(root / 'scripts/render_docs.py'))
    expected = renderer['rendered_documents']()
    assert {path.name: path.read_text(encoding='utf-8') for path in expected} == {
        path.name: content for path, content in expected.items()
    }
