"""Iterate AWS pages while rejecting cycles in service continuation tokens."""

from botocore.exceptions import PaginationError


def pages(client, operation, *, token_field='nextToken', **request):
    seen = set()
    for page in client.get_paginator(operation).paginate(**request):
        token = page.get(token_field)
        if token:
            if not isinstance(token, str) or token in seen:
                raise PaginationError(message='The service cursor did not advance.')
            seen.add(token)
        yield page
