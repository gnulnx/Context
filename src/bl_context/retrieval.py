"""Small, deterministic evidence packages, budgeted after JSON serialization."""

import json
import re
from bisect import bisect_right

RESPONSE_BYTES = 24000
EXCERPT_CHARS = 1200
EXPANSION_CHARS = 12000
RETENTION_DAYS = 7


def serialized(value):
    return json.dumps(value, ensure_ascii=True, separators=(',', ':'))


def size(value):
    return len(serialized(value).encode())


def excerpt(document, *, char_offset=0, terms=(), expansion=False, handoff=False):
    """Keep original evidence and exact offsets; exclude internal/duplicate payloads."""
    fields = ('id', 'context_id', 'context_session_id', 'source_session_id',
              'source_type', 'agent', 'project', 'timestamp', 'role', 'phase',
              'kind', 'authorship', 'tags', 'score', 'retrieval_mode', 'duplicate_of')
    result = {key: document[key] for key in fields if document.get(key) is not None}
    result['source'] = document.get('source', {})
    for key in ('source_references', 'source_references_truncated', 'message_offset'):
        if key in document:
            result[key] = document[key]
    # Metadata is also untrusted input. Retain the context ID for exact expansion.
    for key, value in list(result.items()):
        if size(value) > 3000:
            result.pop(key)
            result['metadata_omitted'] = True
    text = document.get('text', '')
    width = EXPANSION_CHARS if expansion else 4000 if handoff else EXCERPT_CHARS
    start = char_offset
    if terms and not expansion and len(text) > width:
        hits = sorted(match.start() for term in terms
                      for match in re.finditer(re.escape(term), text, re.IGNORECASE))
        if hits:
            # The densest matching window keeps the answer near its subject.
            best = hits[max(range(len(hits)), key=lambda i: bisect_right(hits, hits[i] + width - 160) - i)]
            start = max(0, best - 160)
    end = min(len(text), start + width)
    result.update(text=text[start:end], text_truncated=start > 0 or end < len(text),
                  text_char_start=start, text_char_end=end,
                  next_char_offset=end if end < len(text) else None)
    return result


def page(items, *, offset, more, coverage, **metadata):
    """Bound the entire object, with pagination over returned top-level items."""
    result = dict(results=[], has_more=more, next_offset=None, coverage=coverage,
                  response_truncated=False, response_budget_bytes=RESPONSE_BYTES, **metadata)
    # Leave room for final counters, continuation fields, and escaped metadata.
    for item in items:
        result['results'].append(item)
        if size(result) > RESPONSE_BYTES - 512:
            result['results'].pop()
            if not result['results']:
                # One large message must still make progress without empty records.
                item = json.loads(serialized(item))
                while size(item) > RESPONSE_BYTES // 2:
                    texts = [item] if 'text' in item else item.get('messages', item.get('items', []))
                    target = max(texts, key=lambda doc: len(doc.get('text', '')), default=None)
                    if target and len(target.get('text', '')) > 1:
                        target['text'] = target['text'][:max(1, len(target['text']) // 2)]
                        target['text_char_end'] = target['text_char_start'] + len(target['text'])
                        target.update(text_truncated=True, next_char_offset=target['text_char_end'])
                    else:
                        raise ValueError('Evidence metadata exceeds the response budget')
                result['results'].append(item)
            result['response_truncated'] = True
            break
    count = len(result['results'])
    result['has_more'] = more or count < len(items)
    result['next_offset'] = offset + count if result['has_more'] else None
    result['returned'] = count
    result['coverage_incomplete'] = (
        result['has_more'] or coverage.get('coverage_state') != 'current'
        or any(item.get('messages_truncated') or item.get('items_omitted') for item in result['results'])
    )
    assert size(result) <= RESPONSE_BYTES
    return result
