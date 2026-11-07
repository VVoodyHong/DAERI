"""Publish verified harness releases backed by independently owned shared Memory."""

import json

from daeri.core.errors import StoreError
from daeri.core.projects import Project
from daeri.infrastructure.project_catalog import SsmProjectCatalog


_STRATEGIES = (
    ('ProjectFacts', 'SEMANTIC', '/shared/facts/'),
    ('ProjectEpisodes', 'EPISODIC', '/shared/experiences/'),
    ('SessionSummaries', 'SUMMARIZATION', '/summaries/{actorId}/{sessionId}/'),
)


def validate_project_memory(memory, tags, project_id, catalog, *, name=None):
    """One sharing and retention contract for publication and reconstruction targets."""
    valid = memory.get('status') == 'ACTIVE' and (name is None or memory.get('name') == name)
    valid = valid and memory.get('eventExpiryDuration') == 365 and not memory.get('managedByResourceArn')
    valid = valid and tags.get('daeri:project') == project_id and tags.get('daeri:catalog') == catalog
    strategies = memory.get('strategies', [])
    valid = valid and len(strategies) == len(_STRATEGIES)
    for strategy_name, kind, namespace in _STRATEGIES:
        matches = [item for item in strategies if item.get('name') == strategy_name]
        if len(matches) != 1:
            valid = False
            continue
        strategy = matches[0]
        valid = valid and strategy.get('type') == kind and strategy.get('status') == 'ACTIVE'
        valid = valid and strategy.get('namespaces') == [namespace]
        valid = valid and not strategy.get('recordExpiryDurationDays')
        if kind == 'EPISODIC':
            reflection = strategy.get('configuration', {}).get('reflection', {}).get('episodicReflectionConfiguration', {})
            valid = valid and reflection.get('namespaces') == [namespace]
    if not valid:
        raise StoreError('PROJECT_NOT_READY', 'The project memory ownership, shared namespaces or retention are invalid.')


def verify_project(control, settings, check_harness):
    """Resolve the declared release and verify project-owned shared Memory."""
    version = settings.harness_version
    project = Project(settings.project_id, settings.name, settings.harness_arn, 'RELEASE_V' + version, version)
    try:
        harness = check_harness(project)
        memory = control.get_memory(memoryId=settings.memory_id)['memory']
        arn_prefix = settings.harness_arn.split(':harness/', 1)[0]
        if memory['id'] != settings.memory_id or memory['arn'] != arn_prefix + ':memory/' + settings.memory_id:
            raise StoreError('PROJECT_NOT_READY', 'The declared Memory does not match the project release account and region.')
        tags = control.list_tags_for_resource(resourceArn=memory['arn']).get('tags', {})
        validate_project_memory(memory, tags, settings.project_id, settings.project_parameter_prefix,
                                name=settings.memory_id.rsplit('-', 1)[0])

        binding = harness.get('memory', {}).get('agentCoreMemoryConfiguration', {})
        if binding.get('arn') != memory['arn'] or 'actorId' in binding:
            raise StoreError('PROJECT_NOT_READY', 'The project release needs its shared Memory and real user identity.')
        retrieval = binding.get('retrievalConfig')
        if retrieval is not None:
            strategy_ids = {item['name']: item['strategyId'] for item in memory['strategies']}
            expected = {namespace: strategy_ids[name] for name, _, namespace in _STRATEGIES}
            if set(retrieval) != set(expected) or any(
                retrieval[namespace].get('strategyId') != strategy_id or retrieval[namespace].get('topK', 10) <= 0
                for namespace, strategy_id in expected.items()
            ):
                raise StoreError('PROJECT_NOT_READY', 'The release Memory retrieval does not match the project namespaces.')
        return project, harness
    except StoreError:
        raise
    except Exception:
        raise StoreError('PROJECT_NOT_READY', 'The declared project release could not be verified.') from None


class ProjectDeployment:
    def __init__(self, control, ssm, check_harness):
        self.control, self.ssm, self.check_harness = control, ssm, check_harness

    def check(self, settings):
        project, _ = verify_project(self.control, settings, self.check_harness)
        if SsmProjectCatalog(self.ssm, settings.project_parameter_prefix).get(settings.project_id) != project:
            raise StoreError('PROJECT_NOT_READY', 'The published project does not match the declared release.')
        return self._metadata(project)

    def apply(self, settings):
        project, _ = verify_project(self.control, settings, self.check_harness)
        metadata = self._metadata(project)
        try:
            self.ssm.put_parameter(Name=settings.project_parameter_prefix + project.id, Type='String',
                                   Value=json.dumps(metadata, ensure_ascii=False), Overwrite=True)
        except Exception:
            raise StoreError('PROJECT_NOT_READY', 'The verified release could not be published to the project catalog.') from None
        return metadata

    @staticmethod
    def _metadata(project):
        return {'name': project.name, 'harness_arn': project.harness_arn,
                'harness_endpoint': project.harness_endpoint, 'harness_version': project.harness_version}
