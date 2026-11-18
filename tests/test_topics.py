import pytest

from polyagent_channels import identity


def test_topic_label_builds_channel_and_conversation():
    assert identity.topic_label('review', 'main') == 'review/main'


@pytest.mark.parametrize('label, valid', [('general/main', True), ('a_1/b-2', True), ('a', False),
                                       ('a/b/c', False), ('/main', False), ('a/한글', False), (None, False)])
def test_topic_label_check(label, valid):
    assert identity.is_topic_label(label) is valid


def test_topic_label_builder_rejects_invalid_identifier():
    with pytest.raises(ValueError, match='Invalid topic label'):
        identity.topic_label('has/slash', 'main')
