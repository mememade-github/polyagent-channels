"""Topics remembered by each installed project and client, independently of session state."""
import json

from polyagent_channels import exchange, identity
from polyagent_channels.setup import atomic_write


def read_memberships(root):
    path = root / 'joined-topics.json'
    if not path.exists():
        return {}
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
        validate_memberships(document)
        return document
    except (OSError, ValueError, TypeError):
        raise ValueError('session_membership_invalid') from None


def validate_memberships(document):
    if not isinstance(document, dict):
        raise ValueError('Invalid joined topics')
    for project, clients in document.items():
        validate_project(project, clients)
        validate_clients(clients)


def validate_project(project, clients):
    if not project or not isinstance(clients, dict):
        raise ValueError('Invalid joined project')


def validate_clients(clients):
    for client, labels in clients.items():
        validate_client(client)
        identity.validate_topics(labels)


def validate_client(client):
    if client not in ('claude', 'codex'):
        raise ValueError('Invalid joined client')


def topics(root, project, client):
    if project is None:
        return None
    return project_topics(read_memberships(root), project, client)


def project_topics(document, project, client):
    clients = document.get(str(project), {})
    return clients.get(client, ['general/main'])[:]


def remember(root, project, client, label):
    if project is None:
        return
    with exchange.locked_state(root / 'joined-topics.json'):
        document = read_memberships(root)
        labels = project_topics(document, project, client)
        document.setdefault(str(project), {})[client] = added_topic(labels, label)
        save_topics(root, document)


def added_topic(labels, label):
    if label in labels:
        return labels
    if len(labels) >= identity.MAX_SESSION_TOPICS:
        raise ValueError('session_topic_limit')
    return [*labels, label]


def forget(root, project, client, label):
    if project is None:
        return
    with exchange.locked_state(root / 'joined-topics.json'):
        document = read_memberships(root)
        labels = project_topics(document, project, client)
        document.setdefault(str(project), {})[client] = [topic for topic in labels if topic != label]
        save_topics(root, document)


def save_topics(root, document):
    validate_memberships(document)
    atomic_write(root / 'joined-topics.json', (json.dumps(document, indent=2) + '\n').encode(), mode=0o600)
