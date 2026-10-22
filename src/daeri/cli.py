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
    if value:
        return Path(value).expanduser().resolve()
    if os.environ.get("DAERI_DATA_DIR"):
        configured = os.environ["DAERI_DATA_DIR"]
        directory = Path(configured)
        if configured == '~' or configured.startswith('~/') or (os.name == 'nt' and configured.startswith('~\\')):
            directory = directory.expanduser()
        if not directory.is_absolute():
            from .storage import StoreError
            raise StoreError('INVALID_INPUT', 'DAERI_DATA_DIR must be an absolute path or a current-user home path.')
        return directory.resolve()
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
    commands.add_parser('check', help='Check the installed runtime and local storage features')
    setup = commands.add_parser('setup', help='Generate, validate and apply a selected-resource YAML draft').add_subparsers(dest='setup_command', required=True)
    setup.add_parser('catalog')
    setup.add_parser('status', help='Inspect draft availability without creating user data').add_argument('profile')
    draft = setup.add_parser('draft')
    draft.add_argument('profile')
    draft.add_argument('--resource', action='append', default=[], metavar='INSTANCE=RECIPE')
    draft.add_argument('--embedding', choices=('local', 'openai_compatible', 'keyword_only'))
    draft.add_argument('--refresh', action='store_true')
    setup.add_parser('validate').add_argument('profile')
    apply = setup.add_parser('apply')
    apply.add_argument('profile')
    apply.add_argument('--confirm', required=True)
    profile = commands.add_parser('profile').add_subparsers(dest='profile_command', required=True)
    create = profile.add_parser('create')
    create.add_argument('name')
    create.add_argument('--context', action='append', default=[])
    create.add_argument('--retention-days',
                        type=lambda value: None if value == 'unlimited' else int(value), default=None,
                        help='Positive retention days or unlimited (default)')
    create.add_argument('--long-term-memory-days',
                        type=lambda value: None if value == 'unlimited' else int(value), default=None,
                        help='Positive retention days or unlimited (default)')
    profile.add_parser('list')
    for action in ('show', 'export'):
        profile.add_parser(action).add_argument('name_or_id')
    profile.add_parser('import').add_argument('config_file', type=Path)
    delete = profile.add_parser('delete', help='Preview a profile cascade; apply only with its confirmation token')
    delete.add_argument('name_or_id')
    delete.add_argument('--confirm', metavar='DELETION_TOKEN')
    resource = commands.add_parser('resource').add_subparsers(dest='resource_command', required=True)
    resource.add_parser('catalog')
    resource.add_parser('setup-schema').add_argument('recipe')
    validate = resource.add_parser('validate-setup', help='Read-only validation of partial public setup values')
    validate.add_argument('recipe')
    validate.add_argument('values_file', type=Path)
    configure = resource.add_parser('configure', help='Merge validated answers into one resource of an inspected profile')
    configure.add_argument('recipe')
    configure.add_argument('--profile', required=True)
    configure.add_argument('--instance', required=True)
    configure.add_argument('--values', dest='values_file', type=Path, required=True)
    configure.add_argument('--expected-revision', type=int, required=True)
    template = resource.add_parser('template')
    template.add_argument('recipe')
    template.add_argument('--profile', required=True)
    template.add_argument('--instance', required=True)
    for action in ('prepare', 'diagnose'):
        operation = resource.add_parser(action)
        operation.add_argument('profile')
        operation.add_argument('instance')
        if action == 'prepare': operation.add_argument('--recipe', required=True)
    login = resource.add_parser('login', help='Explicit provider device authorization for a selected connection')
    login.add_argument('profile')
    login.add_argument('instance')
    login.add_argument('--start-url')
    login.add_argument('--sso-region')
    login.add_argument('--role')
    login.add_argument('--no-browser', action='store_true')
    memory = commands.add_parser('memory').add_subparsers(dest='memory_command', required=True)
    memory.add_parser('prepare').add_argument('--profile')
    configure_memory = memory.add_parser('configure', help='Select embedding settings in an inspected profile')
    configure_memory.add_argument('profile')
    configure_memory.add_argument('--config', dest='config_file', type=Path, required=True)
    configure_memory.add_argument('--expected-revision', type=int, required=True)
    memory.add_parser('diagnose').add_argument('--profile', required=True)
    for action in ('status', 'reindex'):
        memory.add_parser(action).add_argument('--profile')
    auth = commands.add_parser('auth').add_subparsers(dest='auth_command', required=True)
    for action in ('set', 'status', 'delete'):
        operation = auth.add_parser(action)
        operation.add_argument('reference')
        if action == 'set': operation.add_argument('--stdin', action='store_true')
    commands.add_parser('cleanup', help='Clean up expired local records and indexes')
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
        if args.command == 'setup':
            from .onboarding import create_draft, validate_draft, apply_draft
            if args.setup_command == 'draft':
                from .storage import StoreError
                selected = {}
                for value in args.resource:
                    if value.count('=') != 1 or value.split('=')[0] in selected:
                        raise StoreError('INVALID_INPUT', 'Select unique resource names and registered products.')
                    instance, recipe = value.split('=')
                    selected[instance] = recipe
                return create_draft(store, args.profile, selected, embedding=args.embedding, refresh=args.refresh)
            if args.setup_command == 'validate':
                return validate_draft(store, args.profile)
            return apply_draft(store, args.profile, args.confirm)
        if args.command == 'profile':
            if args.profile_command == 'delete':
                from .setup import delete_profile, preview_profile_deletion
                return (delete_profile(store, args.name_or_id, args.confirm) if args.confirm is not None
                        else preview_profile_deletion(store, args.name_or_id))
            if args.profile_command == 'create':
                return store.put_profile(args.name, create_only=True, core_context=redact(args.context),
                                         retention_days=args.retention_days,
                                         long_term_memory_days=args.long_term_memory_days)
            if args.profile_command == 'list': return store.list_profiles()
            if args.profile_command == 'import': return import_profile(store, args.config_file)
            if args.profile_command == 'export': return export_profile(store, args.name_or_id)
            return store.get_profile(args.name_or_id)
        if args.command == 'resource':
            if args.resource_command == 'configure':
                from .setup import configure_resource
                return configure_resource(store, args.recipe, args.profile, args.instance,
                                          args.values_file, expected_revision=args.expected_revision)
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
        if args.memory_command == 'configure':
            from .setup import configure_embedding
            return configure_embedding(store, args.profile, args.config_file, expected_revision=args.expected_revision)
        selected = store.get_profile(args.profile) if args.profile else None
        if args.memory_command == 'prepare':
            from .storage import StoreError
            if selected and selected['embedding']['provider'] != 'local':
                raise StoreError('INVALID_INPUT', 'Only a local embedding profile can prepare a local model.')
            embedder = LocalEmbedder.prepare(store.data_dir / 'models')
            embedder.embed_query('DAERI local readiness')
            service = MemoryService(store, embedder=embedder)
            profiles = [selected] if selected else [p for p in store.list_profiles() if p['embedding']['provider'] == 'local']
            indexes = [service.reindex(p['profile_id']) for p in profiles]
            return {'model_prepared': True, 'model_id': embedder.model_id,
                    'vector_ready': service.vector_ready,
                    'index': {key: sum(item[key] for item in indexes) for key in ('ready', 'pending')}}
        service = MemoryService(store)
        if args.memory_command == 'diagnose':
            return service.diagnose(selected['profile_id'])
        if args.memory_command == 'reindex':
            index = service.reindex(selected['profile_id'] if selected else None)
            return {**service.status(selected['profile_id'] if selected else None), 'index': index}
        return service.status(selected['profile_id'] if selected else None)
    finally:
        store.close()


def main():
    args = _parser().parse_args()
    try:
        if args.command == 'check':
            result = check_environment()
            code = 0 if result['ready'] else 1
        elif args.command == 'setup' and args.setup_command in {'catalog', 'status'}:
            from types import SimpleNamespace
            from .onboarding import selection_catalog, draft_status
            result = (selection_catalog() if args.setup_command == 'catalog' else
                      draft_status(SimpleNamespace(data_dir=data_directory(args.data_dir)), args.profile))
            code = 0
        elif args.command == 'auth':
            result, code = _auth(args), 0
        elif args.command == 'resource' and args.resource_command in {'catalog', 'template', 'setup-schema', 'validate-setup'}:
            from types import SimpleNamespace
            from .setup import load_catalog, profile_template, setup_schema, validate_setup
            if args.resource_command == 'catalog': result = load_catalog()
            elif args.resource_command == 'setup-schema': result = setup_schema(args.recipe)
            elif args.resource_command == 'validate-setup': result = validate_setup(args.recipe, args.values_file)
            else:
                result = profile_template(SimpleNamespace(data_dir=data_directory(args.data_dir)),
                                          args.recipe, args.profile, args.instance)
            code = 0 if args.resource_command != 'validate-setup' or result['valid'] else 1
        else:
            result, code = _with_store(args), 0
            if args.command == 'setup' and args.setup_command == 'validate' and not result['valid']:
                code = 1
    except Exception as exc:
        result, code = _error(getattr(exc, 'code', 'STORE_UNAVAILABLE')), 1
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
