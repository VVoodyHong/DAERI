"""The displayed plan includes memory without a separate permission setup."""

from copy import deepcopy

import pytest

from daeri import planning
from daeri.storage import StoreError
from test_planning import NONE, ROOT, Registry, inputs


def configured():
    profile, draft = inputs()
    profile['resources'][0].update(policy=None, target={'server': 'example.invalid'})
    draft['output']['resource_rules'][0].pop('memory')
    return profile, draft


def test_missing_memory_is_derived_before_plan_validation_without_changing_input():
    profile, draft = configured()
    original = deepcopy(draft)
    resolved = planning.resolve_memory_defaults(profile, draft)
    memory = resolved['output']['resource_rules'][0]['memory']
    assert set(memory['categories']) == {'service_rule', 'resource_structure', 'procedure', 'lesson'}
    assert memory['source_fields'] == [ROOT]
    assert memory['context_sources'] == ['request', 'clarification']
    assert memory['retention'] == 'long_term' and memory['allow_model_recall'] is True
    assert planning.validate_plan(profile, resolved, Registry(), []) == ['contract']
    assert draft == original


def test_missing_retained_fields_follow_model_fields_within_existing_restrictions():
    profile, draft = configured()
    field = {'segments': [{'key': 'summary'}]}
    profile['resources'][0]['policy'] = {'allowed_model_fields': [ROOT],
        'retained_fields': [field], 'memory': {'categories': ['procedure'],
        'source_fields': [ROOT], 'context_sources': [], 'retention': 'source_bound',
        'allow_model_recall': True}}
    rule = draft['output']['resource_rules'][0]
    rule['export_fields'] = []
    rule.pop('retained_fields')
    resolved = planning.resolve_memory_defaults(profile, draft)['output']['resource_rules'][0]
    assert resolved['retained_fields'] == [field]
    assert resolved['memory'] == {'categories': ['procedure'], 'source_fields': [field],
        'context_sources': [], 'retention': 'source_bound', 'allow_model_recall': True}


def test_explicit_empty_retention_is_preserved_and_limits_default_memory_sources():
    profile, draft = configured()
    draft['output']['resource_rules'][0]['retained_fields'] = []
    resolved = planning.resolve_memory_defaults(profile, draft)['output']['resource_rules'][0]
    assert resolved['retained_fields'] == []
    assert resolved['memory']['source_fields'] == []


def test_existing_profile_memory_ceiling_constrains_default_recall_and_categories():
    profile, draft = configured()
    profile['memory_policy'] = {'categories': ['lesson'], 'instance_ids': ['resource'],
        'model_allowed': True, 'retain_allowed': True, 'long_term_allowed': False}
    memory = planning.resolve_memory_defaults(profile, draft)['output']['resource_rules'][0]['memory']
    assert memory['categories'] == ['lesson']
    assert memory['retention'] == 'source_bound'
    profile['memory_policy']['model_allowed'] = False
    assert planning.resolve_memory_defaults(profile, draft)['output']['resource_rules'][0]['memory'] == NONE


def test_explicit_exclusion_of_memory_is_kept_in_the_plan():
    profile, draft = configured()
    draft['output']['resource_rules'][0]['memory'] = deepcopy(NONE)
    assert planning.resolve_memory_defaults(profile, draft)['output']['resource_rules'][0]['memory'] == NONE


def test_resolving_defaults_does_not_make_an_unknown_resource_valid():
    profile, draft = configured()
    draft['output']['resource_rules'][0]['instance_id'] = 'other'
    resolved = planning.resolve_memory_defaults(profile, draft)
    with pytest.raises(StoreError, match='PLAN_INVALID'):
        planning.validate_plan(profile, resolved, Registry(), [])


def test_overlapping_array_selectors_keep_the_actual_common_fields():
    profile, draft = configured()
    models = {'segments': [{'each': True}, {'index': 0}]}
    retained = {'segments': [{'index': 0}, {'each': True}]}
    common = {'segments': [{'index': 0}, {'index': 0}]}
    profile['resources'][0]['policy'] = {'allowed_model_fields': [ROOT],
        'export_fields': [ROOT], 'retained_fields': [retained], 'memory': {
            'categories': ['procedure'], 'source_fields': [retained], 'context_sources': [],
            'retention': 'long_term', 'allow_model_recall': True}}
    rule = draft['output']['resource_rules'][0]
    rule['allowed_model_fields'] = [models]
    rule.pop('retained_fields')
    resolved = planning.resolve_memory_defaults(profile, draft)
    assert resolved['output']['resource_rules'][0]['retained_fields'] == [common]
    assert resolved['output']['resource_rules'][0]['memory']['source_fields'] == [common]
    assert planning.validate_plan(profile, resolved, Registry(), []) == ['contract']


def test_memory_candidate_preflight_uses_real_checks_without_persisting_or_indexing(tmp_path):
    from contextlib import closing
    from daeri.memory import MemoryService
    from daeri.storage import Store
    from test_memory import configured_resource, memory_policy, save, source

    with closing(Store(tmp_path)) as store:
        profile = store.put_profile('alpha', resources=[configured_resource()], memory_policy=memory_policy())
        memory = MemoryService(store, auto_load=False)
        observed = source(memory, profile)
        checked = save(memory, profile, sources=[observed], validate_only=True, index=False)
        assert checked['category'] == 'lesson'
        assert store._db.execute('SELECT count(*) FROM memory_heads').fetchone()[0] == 0
        assert store._db.execute('SELECT count(*) FROM memory_vectors').fetchone()[0] == 0
        store.put_profile('alpha', resources=profile['resources'], memory_policy=memory_policy(model_allowed=False))
        with pytest.raises(StoreError, match='ACCESS_DENIED'):
            save(memory, profile, sources=[observed], validate_only=True, index=False)
