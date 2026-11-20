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



def test_git_filter_notices_follow_runtime_text(monkeypatch):
    from polyagent_channels import codex_git

    root = Path(__file__).resolve().parents[1]
    renderer = runpy.run_path(str(root / 'scripts/render_docs.py'))
    names = ('LOCAL_FILTER', 'VISIBLE', 'FOREIGN_FILTER', 'TRACKED_ATTRIBUTES', 'TRACKED_IGNORE',
             'INDEX_REGISTRATION', 'LEGACY_REWRITE')
    replacements = {name: f'notice from {name}' for name in names}
    for name, value in replacements.items():
        monkeypatch.setattr(codex_git, name, value)
    reference = renderer['rendered_documents']()[root / 'REFERENCE.md']
    assert {name for name, value in replacements.items() if value in reference} == set(names)
