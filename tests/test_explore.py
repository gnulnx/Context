import json
from click.testing import CliRunner
from bl_context.cli import main


def test_explore_read_only_and_pagination(tmp_path):
    root = tmp_path / 'codex'
    folder = root / 'sessions'
    folder.mkdir(parents=True)
    path = folder / 'session.jsonl'
    rows = [
        {'type': 'session_meta', 'payload': {'id': 'example', 'cwd': '/project'}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': 'hello'}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'content': 'answer'}},
    ]
    path.write_text('\n'.join(json.dumps(row) for row in rows) + '\n{broken')
    before = path.read_bytes()
    runner = CliRunner()
    listed = runner.invoke(main, ['explore', '--root', str(root), '--json'])
    assert listed.exit_code == 0
    assert json.loads(listed.output)['total_files'] == 1
    first = runner.invoke(main, ['explore', str(path), '--view', 'messages', '--limit', '1', '--json'])
    data = json.loads(first.output)
    assert data['records'][0]['line'] == 2
    assert data['next_line'] == 3
    rest = runner.invoke(main, ['--json', 'explore', str(path), '--view', 'messages', '--start-line', '3'])
    assert len(json.loads(rest.output)['records']) == 2
    raw = runner.invoke(main, ['explore', str(path), '--view', 'raw', '--json'])
    assert json.loads(raw.output)['records'][0]['record'] == rows[0]
    assert 'parse_error' in json.loads(raw.output)['records'][-1]['record']
    assert path.read_bytes() == before
    assert list(folder.iterdir()) == [path]


def test_missing_source_fails_without_creating(tmp_path):
    path = tmp_path / 'missing'
    result = CliRunner().invoke(main, ['explore', '--root', str(path)])
    assert result.exit_code == 1
    assert not path.exists()


def test_preview_classification_and_duplicate_provenance(tmp_path):
    path = tmp_path / 'sample.jsonl'
    def message(role, text, phase=None):
        return {'type': 'response_item', 'payload': {'type': 'message', 'role': role,
                'phase': phase, 'content': [{'type': 'input_text', 'text': text}]}}
    rows = [
        {'type': 'session_meta', 'payload': {'id': 'session', 'cwd': '/project'}},
        {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'turn1'}},
        message('user', '<environment_context>setup</environment_context>'),
        message('user', 'Run tests'),
        {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'Run tests'}},
        message('assistant', 'Working', 'commentary'),
        message('assistant', 'Passed', 'final_answer'),
        message('assistant', 'Unknown phase'),
        message('user', 'Run tests'),
        {'type': 'compacted', 'payload': {}},
        {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'turn2'}},
        message('user', 'Run tests'),
    ]
    path.write_text('\n'.join(map(json.dumps, rows)))
    before = path.read_bytes()
    result = CliRunner().invoke(main, ['explore', str(path), '--view', 'preview', '--details', '--json'])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    records = {r['line']: r for t in data['turns'] for r in t['records']}
    assert records[3]['decision'] == 'excluded'
    assert records[4]['decision'] == 'index'
    assert records[5]['duplicate_of_line'] == 4
    assert records[6]['decision'] == 'index'
    assert records[7]['decision'] == 'index'
    assert records[8]['decision'] == 'unclassified'
    assert records[9]['decision'] == records[12]['decision'] == 'index'
    assert records[10]['decision'] == 'excluded'
    page = CliRunner().invoke(main, ['explore', str(path), '--view', 'preview', '--details', '--start-line', '5', '--limit', '1', '--json'])
    data = json.loads(page.output)
    assert data['turns'][0]['turn_id'] == 'turn1'
    assert data['turns'][0]['records'][3]['duplicate_of_line'] == 4
    assert data['next_line'] == 11
    assert path.read_bytes() == before


def test_conversation_first_nested_messages_and_noise(tmp_path):
    path = tmp_path / 'nested.jsonl'
    rows = [{'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 't'}}]
    rows += [{'type': 'response_item', 'payload': {'type': 'reasoning'}}] * 120
    rows += [
        {'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {'type': 'UserMessage', 'content': [{'type': 'text', 'text': 'Actual request'}]}}},
        {'type': 'event_msg', 'payload': {'type': 'item_completed', 'item': {'type': 'AgentMessage', 'phase': 'final_answer', 'content': [{'type': 'Text', 'text': 'Actual answer'}]}}},
    ]
    path.write_text('\n'.join(map(json.dumps, rows)))
    result = CliRunner().invoke(main, ['explore', str(path), '--limit', '1'])
    assert result.exit_code == 0, result.output
    assert 'Actual request' in result.output and 'Actual answer' in result.output
    assert 'excluded=120' in result.output
    assert 'byte_offset' not in result.output
    data = json.loads(CliRunner().invoke(main, ['explore', str(path), '--limit', '1', '--json']).output)
    assert data['limit_unit'] == 'turns'
    assert [m['text'] for m in data['turns'][0]['messages']] == ['Actual request', 'Actual answer']
    assert 'records' not in data['turns'][0]
    assert not data['has_more']
