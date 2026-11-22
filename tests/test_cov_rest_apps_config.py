"""Card manifests, schema validation, HTML review pins and pointer edits."""
from __future__ import annotations

import hashlib
import json

import pytest
import yaml
from pydantic import ValidationError

from hubzoid.apps import config


@pytest.fixture
def manifest(tmp_path):
    (tmp_path / 'cards').mkdir()
    return tmp_path, {
        'version': 1, 'id': 'sample', 'title': 'Sample',
        'binding': {'tool': 'lookup'}, 'resource': {'builtin': 'table'},
    }


def load_manifest(manifest, document, **kw):
    hub, card = manifest
    (hub / 'cards' / 'one.yaml').write_text(yaml.safe_dump(card))
    return config.load(hub, apps_doc=document, **kw)


@pytest.mark.parametrize('text,pointer', [('version: [', None), ('version: 1\nextra: true', '/extra')])
@pytest.mark.slow
def test_invalid_app_document_names_source(tmp_path, text, pointer):
    (tmp_path / config.FILE).write_text(text)
    out = config.load(tmp_path)
    assert not out.valid and out.cards == {}
    assert out.problems[0].file == config.FILE and out.problems[0].pointer == pointer


@pytest.mark.parametrize('patch,code,pointer', [
    ({'server_id': 'remote'}, 'config_invalid', '/server_id'),
    ({'binding': {'tool': 'absent'}}, 'tool_unknown', '/binding/tool'),
    ({'allowed_tools': ['absent']}, 'tool_unknown', '/allowed_tools/0'),
    ({'allowed_resources': ['file:///etc/passwd']}, 'config_invalid', '/allowed_resources'),
    ({'resource': {}}, 'config_invalid', '/resource'),
    ({'resource': {'builtin': 'table', 'file': 'page.html'}}, 'config_invalid', '/resource'),
    ({'resource': {'builtin': 'table', 'sha256': '0' * 64}}, 'config_invalid', '/resource/sha256'),
    ({'resource': {'builtin': 'plan'}}, 'card_unavailable', '/resource/builtin'),
    ({'resource': {'builtin': 'missing'}}, 'config_invalid', '/resource/builtin'),
    ({'resource': {'builtin': 'table', 'sha256': 123}}, 'config_invalid', '/resource/sha256'),
])
def test_manifest_refusals(manifest, patch, code, pointer):
    manifest[1].update(patch)
    out = load_manifest(manifest, {'version': 1}, tool_names={'lookup'})
    assert out.cards == {} and not out.valid
    assert [(p.code, p.pointer) for p in out.problems] == [(code, pointer)]
    assert out.problems[0].file == 'cards/one.yaml'


@pytest.mark.parametrize('path', ['missing.yaml', '../outside.yaml', '/absolute.yaml'])
def test_missing_and_escaping_manifests(manifest, path):
    out = load_manifest(manifest, {'version': 1, 'card_manifests': [path]})
    assert out.cards == {} and out.problems[0].message == 'The card file is missing or outside the hub.'
    assert out.problems[0].file == path


def test_broken_manifest_yaml(manifest):
    hub, _ = manifest
    (hub / 'cards' / 'bad.yaml').write_text('binding: [')
    out = config.load(hub, apps_doc={'version': 1})
    assert not out.valid and out.problems[0].file == 'cards/bad.yaml'
    assert out.problems[0].code == 'config_invalid' and out.problems[0].line == 1


@pytest.mark.parametrize('same_id,message', [(True, 'Two cards use the id'), (False, 'a tool shows one card')])
def test_duplicate_card_id_or_tool(manifest, same_id, message):
    hub, card = manifest
    other = dict(card, id='sample' if same_id else 'another')
    (hub / 'cards' / 'two.yaml').write_text(yaml.safe_dump(other))
    out = load_manifest(manifest, {'version': 1})
    assert len(out.cards) == 1 and len(out.problems) == 1
    assert message in out.problems[0].message


@pytest.mark.parametrize('schema,code,message', [
    (None, 'output_schema_mismatch', 'missing or outside'),
    ('not json', 'output_schema_mismatch', 'not a valid JSON Schema'),
    ('{"type": "impossible"}', 'output_schema_mismatch', 'not a valid JSON Schema'),
    ('true', 'output_schema_mismatch', 'must describe a JSON object'),
    ('{"type": "array"}', 'output_schema_mismatch', 'must describe a JSON object'),
])
def test_schema_errors(manifest, schema, code, message):
    hub, card = manifest
    card['binding']['output_schema'] = 'schema.json'
    if schema is not None:
        (hub / 'schema.json').write_text(schema)
    out = load_manifest(manifest, {'version': 1})
    assert out.cards == {} and len(out.problems) == 1
    assert out.problems[0].code == code and message in out.problems[0].message


@pytest.mark.parametrize('data,pin,limit,message', [
    (None, None, 1024, 'missing or outside'),
    (b'x' * 1025, None, 1024, 'larger than 1024 bytes'),
    (b'<html>safe</html>', None, 1024, 'Pin the reviewed file'),
    (b'<html>safe</html>', '0' * 64, 1024, 'changed since it was reviewed'),
    (b'\xff', 'correct', 1024, 'not UTF-8'),
    (b'<script>bad</script>', 'correct', 1024, 'standalone HTML document'),
])
def test_custom_resource_refusals(manifest, data, pin, limit, message):
    hub, card = manifest
    if data is not None:
        (hub / 'page.html').write_bytes(data)
    if pin == 'correct':
        pin = hashlib.sha256(data).hexdigest()
    card['resource'] = {'file': 'page.html', 'sha256': pin}
    out = load_manifest(manifest, {'version': 1, 'limits': {'html_bytes': limit}})
    assert out.cards == {} and len(out.problems) == 1
    assert message in out.problems[0].message


def test_custom_schema_and_stored_round_trip(manifest):
    hub, card = manifest
    html = '<!DOCTYPE html><p>Reviewed</p>'
    (hub / 'page.html').write_text(html)
    schema = {'type': 'object', 'required': ['count']}
    (hub / 'schema.json').write_text(json.dumps(schema))
    card.update(resource={'file': 'page.html', 'sha256': config.sha256_text(html)},
                visibility=['app', 'app'], allowed_tools=['lookup', 'lookup'])
    card['binding']['output_schema'] = 'schema.json'
    out = load_manifest(manifest, {'version': 1}, tool_names={'lookup'})
    assert out.valid and out.cards['sample'].schema == schema
    assert out.cards['sample'].visibility == ['app'] and out.cards['sample'].allowed_tools == ['lookup']
    restored = config.from_stored(out.stored(), lambda sha: html)
    assert restored.digest() == out.digest() and restored.cards['sample'].html == html
    assert restored.cards['sample'].public('desk')['output_schema'] is True


def test_pointer_edits_validate_before_writing(tmp_path):
    assert config.set_pointer(tmp_path, '/limits/calls_per_minute', 2) == config.FILE
    before = (tmp_path / config.FILE).read_text()
    assert yaml.safe_load(before) == {'version': 1, 'limits': {'calls_per_minute': 2}}
    for pointer in ('enabled', '/'):
        with pytest.raises(ValueError, match='Use a JSON Pointer'):
            config.set_pointer(tmp_path, pointer, False)
    with pytest.raises(ValueError, match='not a mapping'):
        config.set_pointer(tmp_path, '/version/x', 1)
    with pytest.raises(ValidationError):
        config.set_pointer(tmp_path, '/limits/calls_per_minute', 100)
    assert (tmp_path / config.FILE).read_text() == before
