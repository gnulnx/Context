import pytest

from bl_context import mcp_registration, retrieval_health, service, storage


def test_service_health_validates_daemon_identity(monkeypatch):
    storage.install()
    manifest = storage.read_manifest(storage.locations())
    monkeypatch.setattr(service, 'verify', lambda: 'service verified')
    monkeypatch.setattr(service, 'identity', lambda _: {'pid': 1234})
    monkeypatch.setattr(
        service.retrieval_cli,
        'call',
        lambda request: {
            'status': 'ok',
            'installation_id': manifest['installation_id'],
            'schema_version': storage.VERSION,
            'running_jobs': 0,
            'authored_updates_pending': 0,
            'index_state': 'ready',
            'query_mode': 'hybrid',
        },
    )

    assert service.health() == (
        'Daemon protocol, private database, and installation identity verified '
        '(PID 1234). Global recall ready.'
    )


def test_service_health_reports_nonblocking_vector_sync(monkeypatch):
    storage.install()
    manifest = storage.read_manifest(storage.locations())
    monkeypatch.setattr(service, 'verify', lambda: 'service verified')
    monkeypatch.setattr(service, 'identity', lambda _: {'pid': 1234})
    monkeypatch.setattr(
        service.retrieval_cli,
        'call',
        lambda request: {
            'status': 'ok',
            'installation_id': manifest['installation_id'],
            'schema_version': storage.VERSION,
            'running_jobs': 1,
            'authored_updates_pending': 2,
            'index_state': 'syncing',
            'query_mode': 'lexical_fallback',
        },
    )

    assert service.health() == (
        'Daemon protocol, private database, and installation identity verified '
        '(PID 1234). Global recall available while semantic vectors sync '
        '(2 updates pending).'
    )


def test_service_health_rejects_another_installation(monkeypatch):
    storage.install()
    monkeypatch.setattr(service, 'verify', lambda: 'service verified')
    monkeypatch.setattr(
        service.retrieval_cli,
        'call',
        lambda request: {
            'status': 'ok',
            'installation_id': 'another-installation',
            'schema_version': storage.VERSION,
            'running_jobs': 0,
            'authored_updates_pending': 0,
            'index_state': 'ready',
            'query_mode': 'hybrid',
        },
    )

    with pytest.raises(RuntimeError, match='does not match'):
        service.health()


def test_mcp_health_validates_tool_status(monkeypatch):
    monkeypatch.setattr(mcp_registration, 'verify', lambda: 'registered')
    monkeypatch.setattr(
        mcp_registration,
        'call_tool',
        lambda name: {'source_count': 0, 'messages': 4, 'chunks': 6, 'coverage_state': 'current'},
    )

    assert mcp_registration.health() == (
        'MCP stdio initialized, 8 tools verified, and daemon round-trip '
        'completed (4 messages).'
    )


def test_retrieval_health_exercises_mcp_search_and_expansion(monkeypatch):
    selected_path = '/sessions/recent.jsonl'
    calls = []
    responses = iter(
        [
            {
                'results': [
                    {
                        'context_id': 'context-1',
                        'messages': [
                            {
                                'text': 'The installer indexes recent sessions.',
                                'source': {'path': selected_path},
                            }
                        ],
                    }
                ]
            },
            {
                'results': [
                    {
                        'context_id': 'context-1',
                        'source': {'path': selected_path},
                    }
                ]
            },
            {'results': [{'context_id': 'context-1', 'text': 'Expanded'}]},
        ]
    )

    def call_tool(name, arguments):
        calls.append((name, arguments))
        return next(responses)

    monkeypatch.setattr(mcp_registration, 'verify', lambda: 'registered')
    monkeypatch.setattr(mcp_registration, 'call_tool', call_tool)
    monkeypatch.setattr(
        retrieval_health.discovery,
        'selected_sessions',
        lambda: ({'path': selected_path},),
    )

    assert retrieval_health.verify() == (
        'Selected history, MCP semantic search, context expansion, and source '
        'provenance verified.'
    )
    assert [name for name, _ in calls] == [
        'recent_context',
        'search_context',
        'get_context',
    ]


def test_retrieval_health_requires_source_provenance(monkeypatch):
    responses = iter(
        [
            {
                'results': [
                    {
                        'context_id': 'context-1',
                        'messages': [
                            {
                                'text': 'searchable text',
                                'source': {'path': '/sessions/recent.jsonl'},
                            }
                        ],
                    }
                ]
            },
            {'results': [{'context_id': 'context-1'}]},
        ]
    )
    monkeypatch.setattr(mcp_registration, 'verify', lambda: 'registered')
    monkeypatch.setattr(
        mcp_registration, 'call_tool', lambda name, arguments: next(responses)
    )
    monkeypatch.setattr(
        retrieval_health.discovery,
        'selected_sessions',
        lambda: ({'path': '/sessions/recent.jsonl'},),
    )

    with pytest.raises(RuntimeError, match='selected recent context'):
        retrieval_health.verify()
