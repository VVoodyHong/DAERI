"""Explicit local preparation, profile, credential, and diagnostic commands."""

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import re
import sqlite3
import sys


def data_directory(value=None):
    if value or os.environ.get("DAERI_DATA_DIR"):
        return Path(value or os.environ["DAERI_DATA_DIR"]).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "DAERI"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "DAERI"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "daeri"


def check_environment():
    import mcp  # noqa: F401

    required = importlib.metadata.metadata("daeri-agent")["Requires-Python"]
    supported = True
    for constraint in required.split(","):
        match = re.fullmatch(r"\s*(>=|<)(\d+)\.(\d+)\s*", constraint)
        if not match:
            raise ValueError("Unsupported Python requirement expression")
        boundary = (int(match[2]), int(match[3]))
        supported &= sys.version_info[:2] >= boundary if match[1] == ">=" else sys.version_info[:2] < boundary
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE VIRTUAL TABLE probe USING fts5(value)")
        db.execute("INSERT INTO probe VALUES ('daeri')")
        fts5 = db.execute("SELECT count(*) FROM probe WHERE probe MATCH 'daeri'").fetchone()[0] == 1
    return {
        "ready": bool(supported and fts5),
        "python": sys.version.split()[0],
        "requires_python": required,
        "mcp": importlib.metadata.version("mcp"),
        "sqlite": sqlite3.sqlite_version,
        "sqlite_fts5": fts5,
        "vector_search_ready": False,
        "resource_execution_ready": False,
    }



def _error(code='INVALID_INPUT'):
    return {'status': 'error', 'data': None, 'error': {
        'code': code, 'message': 'Local preparation or configuration could not be completed.',
        'next_action': 'stop'}}


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps(_error()))
        raise SystemExit(2)


def _parser():
    parser = SafeParser(prog='daeri')
    parser.add_argument('--data-dir', type=Path)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('check', help='Check the installed runtime without connecting resources')
    profile = commands.add_parser('profile').add_subparsers(dest='profile_command', required=True)
    create = profile.add_parser('create')
    create.add_argument('name')
    create.add_argument('--context', action='append', default=[])
    create.add_argument('--retention-days', type=int, default=30)
    create.add_argument('--long-term-memory-days', type=int, default=365)
    profile.add_parser('list')
    for action in ('show', 'export'):
        profile.add_parser(action).add_argument('name_or_id')
    profile.add_parser('import').add_argument('config_file', type=Path)
    resource = commands.add_parser('resource').add_subparsers(dest='resource_command', required=True)
    resource.add_parser('catalog')
    template = resource.add_parser('template')
    template.add_argument('recipe')
    template.add_argument('--profile', required=True)
    template.add_argument('--instance', required=True)
    for action in ('prepare', 'diagnose'):
        operation = resource.add_parser(action)
        operation.add_argument('profile')
        operation.add_argument('instance')
        if action == 'prepare': operation.add_argument('--recipe', required=True)
    login = resource.add_parser('login', help='Explicit provider device authorization; does not approve resource work')
    login.add_argument('profile')
    login.add_argument('instance')
    login.add_argument('--start-url')
    login.add_argument('--sso-region')
    login.add_argument('--role')
    login.add_argument('--no-browser', action='store_true')
    memory = commands.add_parser('memory').add_subparsers(dest='memory_command', required=True)
    memory.add_parser('prepare')
    for action in ('status', 'reindex'):
        memory.add_parser(action).add_argument('--profile')
    auth = commands.add_parser('auth').add_subparsers(dest='auth_command', required=True)
    for action in ('set', 'status', 'delete'):
        operation = auth.add_parser(action)
        operation.add_argument('reference')
        if action == 'set': operation.add_argument('--stdin', action='store_true')
    commands.add_parser('cleanup', help='Apply configured expiry without querying resources')
    return parser


def _auth(args):
    from .credentials import Credentials
    from .storage import StoreError
    credentials = Credentials()
    if args.auth_command == 'set':
        if args.stdin:
            value = sys.stdin.read(16385)
            if value.endswith('\n'): value = value[:-1]
            if value.endswith('\r'): value = value[:-1]
        else:
            if not sys.stdin.isatty():
                raise StoreError('AUTHENTICATION_REQUIRED', 'Use an actual local terminal or explicit secure standard input.')
            import getpass
            import warnings
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter('error', getpass.GetPassWarning)
                    value = getpass.getpass('DAERI credential (hidden): ')
            except (getpass.GetPassWarning, EOFError, OSError):
                raise StoreError('AUTHENTICATION_REQUIRED', 'A terminal with hidden input is required.') from None
        if not value or len(value) > 16384 or any(char in value for char in '\r\n'):
            raise StoreError('INVALID_INPUT', 'A bounded single-line credential is required.')
        credentials.set(args.reference, value)
        return {'reference': args.reference, 'stored': True}
    if args.auth_command == 'delete':
        credentials.delete(args.reference)
        return {'reference': args.reference, 'deleted': True}
    try:
        credentials.get(args.reference)
    except StoreError as error:
        if error.code != 'AUTHENTICATION_REQUIRED': raise
        return {'reference': args.reference, 'available': False}
    return {'reference': args.reference, 'available': True}


def _with_store(args):
    from .storage import Store
    from .security import redact
    from .setup import export_profile, import_profile, login_resource, prepare_resource
    store = Store(data_directory(args.data_dir))
    try:
        if args.command == 'profile':
            if args.profile_command == 'create':
                return store.put_profile(args.name, core_context=redact(args.context),
                                         retention_days=args.retention_days,
                                         long_term_memory_days=args.long_term_memory_days)
            if args.profile_command == 'list': return store.list_profiles()
            if args.profile_command == 'import': return import_profile(store, args.config_file)
            if args.profile_command == 'export': return export_profile(store, args.name_or_id)
            return store.get_profile(args.name_or_id)
        if args.command == 'resource':
            if args.resource_command == 'login':
                return login_resource(store, args.profile, args.instance, start_url=args.start_url,
                                      sso_region=args.sso_region, role=args.role, no_browser=args.no_browser)
            if args.resource_command == 'prepare':
                return prepare_resource(store, args.profile, args.instance, args.recipe)
            from .resources import ResourceRegistry
            from .credentials import Credentials
            return ResourceRegistry(store, Credentials().get).diagnose(store.get_profile(args.profile), args.instance)
        from .memory import LocalEmbedder, MemoryService
        if args.command == 'cleanup':
            return {'records': store.cleanup(), 'memory': MemoryService(store, auto_load=False).cleanup()}
        if args.memory_command == 'prepare':
            embedder = LocalEmbedder.prepare(store.data_dir / 'models')
            embedder.embed_query('DAERI local readiness')
            service = MemoryService(store, embedder=embedder)
            return {'model_prepared': True, 'model_id': embedder.model_id,
                    'vector_ready': service.vector_ready, 'index': service.reindex()}
        service = MemoryService(store)
        selected = store.get_profile(args.profile)['profile_id'] if args.profile else None
        if args.memory_command == 'reindex':
            return {'model_prepared': service.embedder is not None,
                    'vector_ready': service.vector_ready, 'index': service.reindex(selected)}
        return {'model_prepared': service.embedder is not None, 'vector_ready': service.vector_ready,
                'model_id': service.embedder.model_id if service.embedder else None,
                'profile_id': selected}
    finally:
        store.close()


def main():
    args = _parser().parse_args()
    try:
        if args.command == 'check':
            result = check_environment()
            code = 0 if result['ready'] else 1
        elif args.command == 'auth':
            result, code = _auth(args), 0
        elif args.command == 'resource' and args.resource_command in {'catalog', 'template'}:
            from types import SimpleNamespace
            from .setup import load_catalog, profile_template
            if args.resource_command == 'catalog': result = load_catalog()
            else:
                result = profile_template(SimpleNamespace(data_dir=data_directory(args.data_dir)),
                                          args.recipe, args.profile, args.instance)
            code = 0
        else:
            result, code = _with_store(args), 0
    except Exception as exc:
        result, code = _error(getattr(exc, 'code', 'STORE_UNAVAILABLE')), 1
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
