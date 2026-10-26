"""Exercise the real DAERI callback with controlled synchronous work, without resources."""

import asyncio
import json
from pathlib import Path
import sys
from time import monotonic, sleep

from mcp.server.stdio import stdio_server

from daeri.server import create_server


class ControlledEngine:
    def __init__(self):
        self.completed = 0

    def call(self, name, arguments):
        if arguments['request_id'] == 'cancelled':
            (control / 'started').touch()
            deadline = monotonic() + 10
            while not (control / 'release').exists():
                if monotonic() >= deadline:
                    raise TimeoutError('The test did not release its synchronous work.')
                sleep(0.005)
            self.completed += 1
            recorded = control / 'completion.json'
            recorded.write_text(json.dumps({'count': self.completed, 'outcome': outcome}))
            recorded.replace(control / 'completed')
            if outcome == 'error':
                raise RuntimeError('Controlled engine failure')
        return {'status': 'ok', 'data': {'completed': self.completed}, 'error': None}


async def main():
    server = create_server(ControlledEngine())
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


control, outcome = Path(sys.argv[1]), sys.argv[2]
asyncio.run(main())
