import pytest

from polyagent_channels import membership


def test_project_without_membership_starts_in_general(tmp_path):
    assert membership.topics(tmp_path, tmp_path / 'project', 'claude') == ['general/main']


def test_remember_and_forget_topics(tmp_path):
    project = tmp_path / 'project'
    membership.remember(tmp_path, project, 'claude', 'review/design')
    membership.forget(tmp_path, project, 'claude', 'general/main')
    assert membership.topics(tmp_path, project, 'claude') == ['review/design']


def test_other_projects_and_clients_have_separate_membership(tmp_path):
    project = tmp_path / 'project'
    membership.remember(tmp_path, project, 'claude', 'review/design')
    assert (membership.topics(tmp_path, project, 'claude'), membership.topics(tmp_path, project, 'codex'),
            membership.topics(tmp_path, tmp_path / 'another', 'claude')) == (
        ['general/main', 'review/design'], ['general/main'], ['general/main'])


def test_uninstalled_session_remembers_nothing(tmp_path):
    membership.remember(tmp_path, None, 'cli', 'review/design')
    assert (membership.topics(tmp_path, None, 'cli'), list(tmp_path.iterdir())) == (None, [])


@pytest.mark.parametrize('content', ['broken', '[]', '{"project": []}', '{"project": {"codex": ["bad"]}}'])
def test_invalid_membership_fails_explicitly(tmp_path, content):
    (tmp_path / 'joined-topics.json').write_text(content)
    with pytest.raises(ValueError, match='session_membership_invalid'):
        membership.topics(tmp_path, tmp_path, 'codex')
