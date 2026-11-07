"""Project members reuse connections; managers change them through host forms."""

import asyncio

from daeri.core.errors import StoreError
from daeri.core.projects import valid_project_key


class ConnectionService:
    def __init__(self, catalog, backend):
        self.catalog, self.backend = catalog, backend

    def connectors(self, user, project_id):
        user.manage(project_id)
        self.catalog.get(project_id)
        return self.backend.describe(project_id)

    def connections(self, user, project_id):
        user.context(project_id)
        self.catalog.get(project_id)
        return {'project_id': project_id, 'connections': self.backend.list(project_id)}

    async def configure(self, user, project_id, connection_id, connector_id, ask):
        if not (valid_project_key(connection_id) and valid_project_key(connector_id)):
            raise StoreError('INVALID_INPUT', 'Supply stable connector and connection identifiers.')
        def describe():
            user.manage(project_id)
            self.catalog.get(project_id)
            return self.backend.describe(project_id, connector_id)

        form = await asyncio.to_thread(describe)
        inputs = await ask(form)
        user.manage(project_id)
        if inputs is None:
            return {'id': connection_id, 'state': 'cancelled'}
        if not isinstance(inputs, dict):
            raise StoreError('INVALID_INPUT', 'Complete the connection form before applying it.')

        def apply():
            if describe() != form:
                raise StoreError('CONNECTION_CHANGED', 'The available connection settings changed; open the form again.')
            user.manage(project_id)
            return self.backend.connect(project_id, connection_id, connector_id, inputs,
                                        expected_revision=form.get('revision'))

        return await asyncio.to_thread(apply)
