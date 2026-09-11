"""Read-only end-to-end verification of indexed historical retrieval."""

from . import discovery, mcp_registration


def verify():
    mcp_registration.verify()
    selected = {item['path'] for item in discovery.selected_sessions()}
    recent = mcp_registration.call_tool(
        'recent_context',
        {'since': '1970-01-01T00:00:00Z', 'limit': 50},
    )
    recent_results = recent.get('results')
    if not isinstance(recent_results, list):
        raise RuntimeError('MCP recent history returned an invalid payload')
    target = None
    for turn in recent_results:
        context_id = turn.get('context_id')
        for message in turn.get('messages', []):
            source = message.get('source')
            text = message.get('text')
            if (
                isinstance(context_id, str)
                and isinstance(source, dict)
                and source.get('path') in selected
                and isinstance(text, str)
                and text.strip()
            ):
                target = (context_id, source['path'], text)
                break
        if target:
            break
    if not target:
        raise RuntimeError('No retrievable turn belongs to the five selected sessions')

    target_context, target_path, text = target
    search = mcp_registration.call_tool(
        'search_context',
        {'query': text[:2000], 'limit': 10},
    )
    matches = search.get('results')
    if not isinstance(matches, list) or not matches:
        raise RuntimeError('Semantic search returned no historical matches')
    match = next(
        (
            item
            for item in matches
            if item.get('context_id') == target_context
            and isinstance(item.get('source'), dict)
            and item['source'].get('path') == target_path
        ),
        None,
    )
    if match is None:
        raise RuntimeError('Semantic search did not return the selected recent context')
    context_id = match.get('context_id')
    source = match.get('source')
    if (
        not isinstance(context_id, str)
        or not context_id
        or not isinstance(source, dict)
        or not isinstance(source.get('path'), str)
    ):
        raise RuntimeError('Semantic search result is missing source provenance')

    expanded = mcp_registration.call_tool(
        'get_context',
        {'context_id': context_id, 'limit': 1},
    )
    expanded_results = expanded.get('results')
    if not isinstance(expanded_results, list) or not expanded_results:
        raise RuntimeError('Retrieved historical context could not be expanded')
    if expanded_results[0].get('context_id') != context_id:
        raise RuntimeError('Expanded historical context identity does not match search')
    return 'Selected history, MCP semantic search, context expansion, and source provenance verified.'
