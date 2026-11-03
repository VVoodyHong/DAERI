"""Publish verified harness releases backed by independently owned shared Memory."""

from hashlib import sha256
import json
import time

from daeri.core.errors import StoreError
from daeri.core.projects import Project
from daeri.infrastructure.project_catalog import SsmProjectCatalog


_STRATEGIES = (
    ('semanticMemoryStrategy', 'ProjectFacts', 'SEMANTIC', '/shared/facts/'),
    ('episodicMemoryStrategy', 'ProjectEpisodes', 'EPISODIC', '/shared/experiences/'),
    ('summaryMemoryStrategy', 'SessionSummaries', 'SUMMARIZATION', '/summaries/{actorId}/{sessionId}/'),
)


def memory_strategies():
    result = []
    for key, name, kind, namespace in _STRATEGIES:
        strategy = {'name': name, 'namespaces': [namespace]}
        if kind == 'EPISODIC':
            strategy['reflectionConfiguration'] = {'namespaces': [namespace]}
        result.append({key: strategy})
    return result


def _missing(error):
    return getattr(error, 'response', {}).get('Error', {}).get('Code') == 'ResourceNotFoundException'


def _wait(fetch, field):
    for attempt in range(60):
        value = fetch()[field]
        if value.get('status') in {'ACTIVE', 'READY'}:
            return value
        if value.get('status') not in {'CREATING', 'UPDATING'}:
            break
        if attempt < 59:
            time.sleep(2)
    raise StoreError('PROJECT_NOT_READY', 'The project resource is not ready; check deployment status before retrying.')


class ProjectDeployment:
    def __init__(self, control, ssm, check_harness):
        self.control, self.ssm, self.check_harness = control, ssm, check_harness

    def _memory(self, settings):
        request = {'maxResults': 100}
        while True:
            result = self.control.list_memories(**request)
            for item in result.get('memories', []):
                if item.get('id', '').startswith(settings.memory_name + '-'):
                    return self.control.get_memory(memoryId=item['id'])['memory']
            token = result.get('nextToken')
            if not token:
                return None
            request['nextToken'] = token

    def _validate_memory(self, settings, memory):
        valid = memory.get('status') == 'ACTIVE' and memory.get('name') == settings.memory_name
        valid = valid and memory.get('eventExpiryDuration') == 365 and not memory.get('managedByResourceArn')
        for key in ('memoryExecutionRoleArn', 'encryptionKeyArn'):
            configured = getattr(settings, {'memoryExecutionRoleArn': 'memory_execution_role_arn',
                                            'encryptionKeyArn': 'encryption_key_arn'}[key])
            if configured is not None:
                valid = valid and memory.get(key) == configured
        tags = self.control.list_tags_for_resource(resourceArn=memory['arn']).get('tags', {})
        valid = valid and tags.get('daeri:project') == settings.project_id
        valid = valid and tags.get('daeri:catalog') == settings.project_parameter_prefix
        strategies = memory.get('strategies', [])
        valid = valid and len(strategies) == len(_STRATEGIES)
        for _, name, kind, namespace in _STRATEGIES:
            matches = [item for item in strategies if item.get('name') == name]
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

    @staticmethod
    def _attached(harness, memory):
        config = harness.get('memory', {}).get('agentCoreMemoryConfiguration', {})
        if config.get('arn') != memory['arn'] or 'actorId' in config:
            return False
        retrieval = config.get('retrievalConfig')
        if retrieval is None:
            return True
        expected = {namespace: next(item['strategyId'] for item in memory['strategies'] if item['name'] == name)
                    for _, name, _, namespace in _STRATEGIES}
        return set(retrieval) == set(expected) and all(
            retrieval[key].get('strategyId') == value and retrieval[key].get('topK', 10) > 0
            for key, value in expected.items())

    def check(self, settings):
        try:
            project = SsmProjectCatalog(self.ssm, settings.project_parameter_prefix).get(settings.project_id)
            if project.harness_arn != settings.harness_arn or project.name != settings.name:
                raise StoreError('PROJECT_NOT_READY', 'The published project does not match the deployment settings.')
            harness = self.control.get_harness(harnessId=settings.harness_arn.rsplit('/', 1)[1],
                                               harnessVersion=project.harness_version)['harness']
            memory = self._memory(settings)
            if memory is None:
                raise StoreError('PROJECT_NOT_READY', 'The project memory has not been created.')
            self._validate_memory(settings, memory)
            if not self._attached(harness, memory):
                raise StoreError('PROJECT_NOT_READY', 'The project harness needs its shared memory connection.')
            self.check_harness(project)
            return self._metadata(project)
        except StoreError:
            raise
        except Exception:
            raise StoreError('PROJECT_NOT_READY', 'The project deployment could not be verified.') from None

    def apply(self, settings):
        try:
            memory = self._memory(settings)
            harness_id = settings.harness_arn.rsplit('/', 1)[1]
            harness = _wait(lambda: self.control.get_harness(harnessId=harness_id), 'harness')
            existing_arn = harness.get('memory', {}).get('agentCoreMemoryConfiguration', {}).get('arn')
            if existing_arn and (memory is None or existing_arn != memory['arn']):
                raise StoreError('PROJECT_NOT_READY',
                                 'The existing project memory needs an explicit migration before publishing.')
            if memory is None:
                request = {'name': settings.memory_name, 'eventExpiryDuration': 365,
                           'memoryStrategies': memory_strategies(),
                           'tags': {'daeri:project': settings.project_id, 'daeri:catalog': settings.project_parameter_prefix},
                           'clientToken': sha256(settings.memory_name.encode()).hexdigest()}
                if settings.memory_execution_role_arn:
                    request['memoryExecutionRoleArn'] = settings.memory_execution_role_arn
                if settings.encryption_key_arn:
                    request['encryptionKeyArn'] = settings.encryption_key_arn
                memory = self.control.create_memory(**request)['memory']
            memory = _wait(lambda: self.control.get_memory(memoryId=memory['id']), 'memory')
            self._validate_memory(settings, memory)
            harness = _wait(lambda: self.control.get_harness(harnessId=harness_id), 'harness')
            if not self._attached(harness, memory):
                updated = self.control.update_harness(harnessId=harness_id,
                    memory={'optionalValue': {'agentCoreMemoryConfiguration': {'arn': memory['arn']}}})['harness']
                version = updated['harnessVersion']
                harness = _wait(lambda: self.control.get_harness(harnessId=harness_id,
                                                                harnessVersion=version), 'harness')
            if not self._attached(harness, memory):
                raise StoreError('PROJECT_NOT_READY', 'The project harness memory configuration was not applied.')
            version = harness['harnessVersion']
            endpoint_name = 'RELEASE_V' + version
            try:
                self.control.get_harness_endpoint(harnessId=harness_id, endpointName=endpoint_name)
            except Exception as error:
                if not _missing(error):
                    raise
                self.control.create_harness_endpoint(harnessId=harness_id, endpointName=endpoint_name,
                                                     targetVersion=version)
            endpoint = _wait(lambda: self.control.get_harness_endpoint(harnessId=harness_id,
                                                                      endpointName=endpoint_name), 'endpoint')
            if endpoint.get('liveVersion') != version or endpoint.get('targetVersion') != version:
                raise StoreError('PROJECT_NOT_READY', 'The project release endpoint points to a different version.')
            project = Project(settings.project_id, settings.name, settings.harness_arn, endpoint_name, version)
            self.check_harness(project)
            metadata = self._metadata(project)
            self.ssm.put_parameter(Name=settings.project_parameter_prefix + settings.project_id,
                Type='String', Value=json.dumps(metadata, ensure_ascii=False), Overwrite=True)
            return metadata
        except StoreError:
            raise
        except Exception:
            raise StoreError('PROJECT_NOT_READY', 'The project deployment could not be completed; inspect its resource status.') from None

    @staticmethod
    def _metadata(project):
        return {'name': project.name, 'harness_arn': project.harness_arn,
                'harness_endpoint': project.harness_endpoint, 'harness_version': project.harness_version}
