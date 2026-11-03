"""The packaged server command validates configuration and owns service shutdown."""

from types import SimpleNamespace

import pytest

from daeri.__main__ import main


def test_cli_starts_the_composed_app_and_closes_services(monkeypatch):
    from daeri.config import server
    import uvicorn

    events = []
    class Services:
        mcp = SimpleNamespace(streamable_http_app=lambda: 'asgi-application')
        def __enter__(self):
            events.append('open')
            return self
        def __exit__(self, *args):
            events.append('close')
    monkeypatch.setattr(server.ServerSettings, 'from_yaml', lambda path: ('settings', path))
    monkeypatch.setattr(server, 'build_server', lambda settings: Services())
    monkeypatch.setattr(uvicorn, 'run', lambda app, **options: events.append((app, options)))
    main(['--config', 'server.yaml', '--host', '0.0.0.0', '--port', '9000'])
    assert events == ['open', ('asgi-application', {'host': '0.0.0.0', 'port': 9000}), 'close']


def test_invalid_port_is_rejected_before_reading_settings(monkeypatch):
    from daeri.config import server
    monkeypatch.setattr(server.ServerSettings, 'from_yaml', lambda path: pytest.fail('Invalid port read configuration'))
    with pytest.raises(SystemExit) as error:
        main(['--config', 'server.yaml', '--port', '70000'])
    assert error.value.code == 2


def test_container_configuration_is_read_from_environment_and_missing_config_fails_closed(monkeypatch):
    from daeri.config import server
    import uvicorn

    payload = '{"runtime":"non-secret-config"}'
    monkeypatch.setenv('DAERI_SERVER_CONFIG', payload)
    monkeypatch.setattr(server.ServerSettings, 'model_validate_json', lambda value: ('settings', value))
    opened = []
    class Services:
        mcp = SimpleNamespace(streamable_http_app=lambda: 'runtime-asgi')
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(server, 'build_server', lambda settings: opened.append(settings) or Services())
    monkeypatch.setattr(uvicorn, 'run', lambda app, **options: None)
    main(['--host', '0.0.0.0'])
    assert opened == [('settings', payload)]


def test_missing_runtime_configuration_does_not_start_service(monkeypatch):
    from daeri.config import server
    monkeypatch.delenv('DAERI_SERVER_CONFIG', raising=False)
    monkeypatch.setattr(server, 'build_server', lambda settings: pytest.fail('Missing configuration started services'))
    with pytest.raises(SystemExit) as error:
        main([])
    assert error.value.code == 2
