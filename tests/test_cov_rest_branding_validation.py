"""Brand copy, paths and decorative CSS remain bounded plain presentation."""
from __future__ import annotations

import pytest

from hubzoid.ui import branding_css as css
from hubzoid.ui import branding_models as brand


@pytest.mark.parametrize('patch,pointer,message', [
    ({'identity': {'name': ' '}}, '/identity/name', 'needs some text'),
    ({'identity': {'name': 'x' * 81}}, '/identity/name', 'at most 80'),
    ({'identity': {'description': '\x00'}}, '/identity/description', 'control characters'),
    ({'assets': {'logo_dark': '../logo.svg'}}, '/assets/logo_dark', 'relative paths'),
    ({'copy': {'en': {'suggestions': ['x'] * 13}}}, '/copy/en/suggestions', 'at most 12'),
    ({'links': {'help': '//evil.example.org'}}, '/links/help', 'links are https'),
    ({'announcement': {'id': 'bad id', 'text_by_locale': {'en': 'Hi'}}}, '/announcement/id', 'announcement id'),
    ({'announcement': {'id': 'ok', 'text_by_locale': {'en': ' '}}}, '/announcement/text_by_locale', 'English text'),
    ({'presentation': {'hidden_features': ['decisions']}}, '/presentation/hidden_features', 'entries are among'),
    ({'presentation': {'decorative_css': 'branding/../escape.css'}}, '/presentation/decorative_css', '.css path'),
    ({'legal': {'require_acceptance': True}}, '', 'require_acceptance needs'),
])
def test_invalid_brand_fields_name_exact_pointer(patch, pointer, message):
    with pytest.raises(brand.BrandError) as exc:
        brand.validate({'version': 1, **patch})
    assert exc.value.code == 'validation_failed' and exc.value.extra['pointer'] == pointer
    assert message in exc.value.message


def test_normalized_copy_and_asset_references():
    out = brand.validate({'version': 1, 'copy': {'en': {'welcome_title': 'Hi', 'welcome_body': 'Welcome',
        'suggestions': ['Ask a question']}}, 'assets': {'logo_light': None},
        'legal': {'version': 'v1'}, 'presentation': {'response_watermark': 'Internal',
        'hidden_features': ['voice', 'voice'], 'decorative_css': 'branding/decoration.css'}})
    assert out['copy']['en']['welcome_title'] == 'Hi' and out['presentation']['hidden_features'] == ['voice']
    assert brand.referenced_paths(out) == {'decorative_css': 'branding/decoration.css'}
    assert brand._pointer(('copy_', 'a/b~c')) == '/copy/a~1b~0c'


@pytest.mark.parametrize('declaration,message', [
    ('border-radius: auto', 'border-radius is 0 to 24px'),
    ('padding: 33px', 'padding is one to four'), ('padding: 1px 2px 3px 4px 5px', 'padding is one to four'),
    ('border-color: red', 'six-digit hex'), ('padding: 1px !important', '!important is not allowed'),
    ('missing-colon', 'not a declaration'), ('display: none', 'display is not allowed'),
])
def test_decorative_declarations_cannot_hide_controls(declaration, message):
    with pytest.raises(brand.BrandError) as exc:
        css.parse('[data-brand-slot="brand.welcome"] {' + declaration + ';}')
    assert exc.value.code == 'unsafe_style' and message in exc.value.message


@pytest.mark.parametrize('text,message', [
    ('p {} [data-brand-slot="brand.welcome"] {padding: 0}', 'only rules for the brand slots'),
    ('[data-brand-slot="brand.unknown"] {padding: 0}', 'not a brand slot'),
    ('[data-brand-slot="brand.welcome"] {padding: 0} body {}', 'only rules for the brand slots'),
])
def test_selector_boundary_is_exact(text, message):
    with pytest.raises(brand.BrandError, match=message):
        css.parse(text)


def test_decoration_size_and_canonical_rendering():
    with pytest.raises(brand.BrandError) as exc:
        css.parse(' ' * (css.MAX_BYTES + 1))
    assert exc.value.status == 413 and exc.value.code == 'asset_too_large'
    rules = css.parse('/*comment*/ [data-brand-slot="brand.welcome"] {padding: 0 32px; border-radius: 24px;}')
    assert css.render(rules) == '[data-brand-slot="brand.welcome"] { border-radius: 24px; padding: 0 32px; }\n'
    assert css.render({}) == ''
