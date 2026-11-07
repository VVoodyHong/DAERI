"""Bind fake AWS operations to the pagination models used by real clients."""

from botocore import xform_name
from botocore.paginate import Paginator
from botocore.session import get_session


class PaginatedClient:
    def get_paginator(self, operation):
        session = get_session()
        service = session.get_service_model(self.aws_service)
        name = next(name for name in service.operation_names if xform_name(name) == operation)
        config = session.get_paginator_model(self.aws_service).get_paginator(name)
        return Paginator(getattr(self, operation), config, service.operation_model(name))
