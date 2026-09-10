"""Conservative, inspectable ingestion proposals. Never persists transcript data."""
from collections import Counter
import json
import hashlib
import os


def classify_transcript(path):
    turns = []
    current = None
    metadata = {}
    seen = {}
    counts = Counter()
    more = False
    next_line = None

    def new_turn(identifier, line, inferred=False):
        nonlocal current, seen
        current = {'turn_id': identifier, 'inferred': inferred, 'start_line': line,
                   'project': metadata.get('cwd'), 'records': []}
        turns.append(current)
        seen = {}

    with path.open('rb') as stream:
        boundary = os.fstat(stream.fileno()).st_size
        digest = hashlib.sha256()
        number = 0
        while stream.tell() < boundary:
            offset = stream.tell()
            raw = stream.readline(boundary - offset)
            digest.update(raw)
            number += 1
            try:
                r = json.loads(raw)
                if not isinstance(r, dict):
                    raise ValueError('Expected an object')
            except (ValueError, UnicodeError):
                r = {'type': 'invalid', 'payload': {}}
            p = r.get('payload', {})
            p = p if isinstance(p, dict) else {}
            kind = r.get('type')
            subtype = p.get('type')
            if kind == 'session_meta':
                metadata.update({k: p[k] for k in ('id', 'session_id', 'cwd', 'cli_version', 'source') if k in p})
            turn_id = p.get('turn_id') if kind == 'turn_context' or (kind == 'event_msg' and subtype == 'task_started') else None
            if turn_id and (current is None or current['turn_id'] != turn_id):
                new_turn(turn_id, number)
            if current is None:
                new_turn('unassigned', number, True)
            if kind == 'turn_context' and p.get('cwd'):
                current['project'] = p['cwd']
                metadata['cwd'] = p['cwd']

            role, phase, text = None, None, None
            family = None
            if kind == 'response_item' and subtype == 'message':
                role, phase = p.get('role'), p.get('phase')
                content = p.get('content', [])
                if isinstance(content, list):
                    text = '\n'.join(c['text'] for c in content if isinstance(c, dict) and isinstance(c.get('text'), str))
                family = 'response'
            elif kind == 'event_msg' and subtype in ('user_message', 'agent_message'):
                role = 'user' if subtype == 'user_message' else 'assistant'
                phase, text, family = p.get('phase'), p.get('message'), 'event'
            elif kind == 'event_msg' and subtype == 'item_completed':
                item = p.get('item', {})
                if isinstance(item, dict) and item.get('type') in ('UserMessage', 'AgentMessage'):
                    role = 'user' if item['type'] == 'UserMessage' else 'assistant'
                    phase, family = item.get('phase'), 'completed'
                    content = item.get('content', [])
                    text = '\n'.join(c['text'] for c in content if isinstance(c, dict) and isinstance(c.get('text'), str)) if isinstance(content, list) else None

            decision, reason = 'unclassified', 'Unknown record shape; review in raw view'
            duplicate_of = None
            if role:
                if role in ('system', 'developer'):
                    decision, reason = 'excluded', 'System/developer instructions'
                elif not isinstance(text, str) or not text:
                    reason = 'Message has no recognized text; inspect raw content/attachments'
                elif role == 'user' and text.lstrip().startswith(('<environment_context', '<codex_internal_context', '# AGENTS.md', '<user_instructions>')):
                    decision, reason = 'excluded', 'Recognized injected-context prefix; heuristic, reviewable'
                elif role == 'user':
                    decision, reason = 'index', 'Candidate user input; not yet approved for import'
                elif role == 'assistant' and phase == 'final_answer':
                    decision, reason = 'index', 'Assistant final answer; a reported claim, not verified fact'
                elif role == 'assistant' and phase == 'commentary':
                    decision, reason = 'context_only', 'Progress commentary; available when expanding a turn'
                else:
                    reason = 'Missing or unknown assistant phase'
                source = metadata.get('source')
                if isinstance(source, dict) and 'subagent' in source and decision == 'index':
                    decision, reason = 'context_only', 'Subagent conversation; excluded from default search'
                if isinstance(text, str) and text:
                    key = (role, text.rstrip("\n"))
                    candidates = seen.setdefault(key, [])
                    # Pair occurrences across representations, never deduplicate
                    # repeated same-family messages solely because text matches.
                    prior = next((c for c in candidates if family not in c['families']), None)
                    if prior:
                        duplicate_of = prior['line']
                        prior['families'].add(family)
                        decision, reason = 'duplicate', 'Exact text in another representation in this turn; proposed match'
                    else:
                        candidates.append({'line': number, 'families': {family}})
            elif kind in ('session_meta', 'turn_context'):
                decision, reason = 'metadata', 'Session/turn provenance and project filters'
            elif kind == 'compacted':
                decision, reason = 'excluded', 'Derived compaction history; not original conversation'
            elif kind == 'response_item' and subtype in ('reasoning', 'function_call', 'function_call_output', 'custom_tool_call', 'custom_tool_call_output'):
                decision, reason = 'excluded', 'Reasoning or tool traffic; not default searchable conversation'
            elif kind == 'event_msg' and subtype in ('task_started', 'task_complete', 'token_count', 'context_compacted', 'turn_aborted'):
                decision, reason = 'metadata', 'Lifecycle/usage event; not an additional message'
            elif kind == 'event_msg' and subtype == 'item_completed':
                item = p.get('item', {})
                if isinstance(item, dict) and item.get('type') in ('Reasoning', 'CommandExecution', 'FileChange', 'ContextCompaction', 'SubAgentActivity', 'Plan'):
                    decision, reason = 'excluded', 'Completed tool/reasoning/activity record; not searchable conversation'
            elif kind in ('world_state', 'token_usage_record', 'inter_agent_communication_metadata'):
                decision, reason = 'metadata', 'Runtime bookkeeping; not searchable conversation'
            elif kind == 'event_msg' and subtype in ('thread_settings_applied', 'thread_goal_updated', 'sub_agent_activity', 'patch_apply_end'):
                decision, reason = 'metadata', 'Runtime event; not an additional conversation message'
            elif kind == 'invalid':
                reason = 'Invalid JSON, incomplete line, or non-object record'

            record = {'line': number, 'byte_offset': offset, 'timestamp': r.get('timestamp'),
                      'type': kind, 'subtype': subtype, 'role': role, 'phase': phase,
                      'decision': decision, 'reason': reason, 'duplicate_of_line': duplicate_of}
            if isinstance(text, str):
                record['text'] = text
            current['records'].append(record)
            counts[decision] += 1
    return {'path': str(path), 'view': 'preview', 'proposal_only': True,
            'snapshot_bytes': boundary, 'source_digest': digest.hexdigest(), 'metadata': metadata,
            'counts': dict(counts), 'counts_scope': 'displayed source records',
            'turns': [t for t in turns if t['records']], 'has_more': more, 'next_line': next_line}


def preview(path, start, limit, details=False):
    """Page complete proposed turns; classification is shared with future ingestion.

    The whole captured file is classified so duplicate reconciliation and turn
    context are independent of the requested page. No persistence occurs.
    """
    data = classify_transcript(path)
    all_turns = data['turns']
    for turn in all_turns:
        by_line = {r['line']: r for r in turn['records']}
        for record in turn['records']:
            original = by_line.get(record['duplicate_of_line'])
            if original and original['decision'] == 'unclassified' and record['phase'] in ('final_answer', 'commentary'):
                original['phase'] = record['phase']
                original['decision'] = 'index' if record['phase'] == 'final_answer' else 'context_only'
                if isinstance(data['metadata'].get('source'), dict) and 'subagent' in data['metadata']['source']:
                    original['decision'] = 'context_only'
                original['reason'] = 'Phase corroborated by duplicate representation'
    data['counts'] = dict(Counter(r['decision'] for t in all_turns for r in t['records']))
    selected = []
    candidates = []
    for turn in all_turns:
        turn['messages'] = [r for r in turn['records'] if r['decision'] == 'index']
        for message in turn['messages']:
            message['duplicate_sources'] = [
                {'line': r['line'], 'byte_offset': r['byte_offset']}
                for r in turn['records'] if r['duplicate_of_line'] == message['line']
            ]
        turn['warnings'] = []
        if turn['messages'] and not any(r['role'] == 'user' for r in turn['messages']):
            turn['warnings'].append('No selected user request; this is not a complete exchange.')
        if turn['messages'] and not any(r['role'] == 'assistant' for r in turn['messages']):
            turn['warnings'].append('No selected final answer; turn may be incomplete or need classification.')
        if turn['inferred']:
            turn['warnings'].append('No explicit turn ID; grouping is unassigned.')
        turn['counts'] = dict(Counter(r['decision'] for r in turn['records']))
        turn['review'] = [
            {'line': r['line'], 'type': r['type'], 'subtype': r['subtype'], 'reason': r['reason']}
            for r in turn['records'] if r['decision'] == 'unclassified'
        ]
        end = max(r['line'] for r in turn['records'])
        if end >= start and (turn['messages'] or details):
            candidates.append(turn)
    selected = candidates[:limit]
    next_line = candidates[limit]['start_line'] if len(candidates) > limit else None
    data.update(turns=selected, has_more=next_line is not None, next_line=next_line,
                counts_scope='entire captured file', total_searchable_turns=sum(bool(t['messages']) for t in all_turns),
                limit_unit='turns', policy_version=1,
                excluded_summary=dict(Counter(r['reason'] for t in all_turns for r in t['records'] if r['decision'] not in ('index', 'duplicate'))))
    if not details:
        for turn in selected:
            del turn['records']
    return data
