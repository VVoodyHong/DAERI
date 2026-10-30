"""Project knowledge and a transactional outbox through Aurora PostgreSQL Data API."""

from contextlib import contextmanager
from dataclasses import asdict
import json
import math
from uuid import uuid4

from daeri.core.cloud import AccessContext, CloudTask, KnowledgeJob, SharedMemory
from daeri.core.errors import StoreError


SCHEMA = (
    'CREATE EXTENSION IF NOT EXISTS vector',
    '''CREATE TABLE IF NOT EXISTS cloud_tasks (
        id text PRIMARY KEY, session_id text UNIQUE NOT NULL,
        actor_id text NOT NULL, project_id text NOT NULL, service_id text NOT NULL,
        source_ids jsonb NOT NULL, pending_tools jsonb NOT NULL DEFAULT '[]',
        current_turn text, state text NOT NULL DEFAULT 'active',
        created_at timestamptz NOT NULL DEFAULT now())''',
    '''CREATE TABLE IF NOT EXISTS cloud_turns (
        id text PRIMARY KEY, task_id text NOT NULL REFERENCES cloud_tasks(id),
        prompt text NOT NULL, reply jsonb NOT NULL, recalled_ids jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now())''',
    '''CREATE TABLE IF NOT EXISTS cloud_knowledge_jobs (
        turn_id text PRIMARY KEY REFERENCES cloud_turns(id),
        state text NOT NULL DEFAULT 'pending', attempts integer NOT NULL DEFAULT 0,
        lease_token text, lease_until timestamptz, error_code text,
        available_at timestamptz NOT NULL DEFAULT now())''',
    '''CREATE TABLE IF NOT EXISTS cloud_knowledge (
        id text PRIMARY KEY REFERENCES cloud_turns(id),
        project_id text NOT NULL, service_id text NOT NULL, source_ids jsonb NOT NULL,
        text text NOT NULL, embedding vector, model_id text,
        search_vector tsvector GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED,
        created_at timestamptz NOT NULL DEFAULT now())''',
    'CREATE INDEX IF NOT EXISTS cloud_knowledge_scope ON cloud_knowledge(project_id, service_id)',
    'CREATE INDEX IF NOT EXISTS cloud_knowledge_fts ON cloud_knowledge USING gin(search_vector)',
    '''CREATE INDEX IF NOT EXISTS cloud_jobs_pending ON cloud_knowledge_jobs(available_at)
       WHERE state = 'pending' ''',
)


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def _scope(context):
    return {'actor': context.actor_id, 'project': context.project_id,
            'service': context.service_id, 'sources': _json(sorted(context.source_ids))}


def _vector(value):
    if (not isinstance(value, list) or not 1 <= len(value) <= 8192
            or any(type(item) not in (int, float) or not math.isfinite(item) for item in value)
            or not any(value)):
        raise StoreError('INVALID_INPUT', 'Embedding must be a finite, nonzero numeric vector.')
    return _json(value)


_TASK_SCOPE = '''actor_id = :actor AND project_id = :project AND service_id = :service
                 AND source_ids = CAST(:sources AS jsonb)'''
_LIVE_JOB = 'turn_id = :id AND lease_token = :token AND lease_until > now()'


class AuroraCloudStore:
    """The injected client owns AWS authentication; all user data uses SQL parameters."""

    def __init__(self, client, resource_arn, secret_arn, database):
        self.client = client
        self.connection = {'resourceArn': resource_arn, 'secretArn': secret_arn, 'database': database}

    def initialize(self):
        """Explicit administrative setup, separate from request handling."""
        for statement in SCHEMA:
            self._execute(statement)

    def _execute(self, sql, values=None, transaction=None):
        parameters = []
        for name, value in (values or {}).items():
            if value is None:
                field = {'isNull': True}
            elif type(value) is int:
                field = {'longValue': value}
            else:
                field = {'stringValue': value}
            parameters.append({'name': name, 'value': field})
        request = {**self.connection, 'sql': sql, 'parameters': parameters}
        if transaction:
            request['transactionId'] = transaction
        try:
            result = self.client.execute_statement(**request)
            return [json.loads(row[0]['stringValue']) for row in result.get('records', [])]
        except Exception:
            raise StoreError('STORAGE_UNAVAILABLE', 'Cloud storage could not complete the operation.') from None

    def _read_document(self, sql, values):
        # Data API caps each returned row at 64 KiB. 8,192 Unicode characters
        # remain below that ceiling even when every character needs JSON escaping.
        parts, offset = [], 1
        while True:
            rows = self._execute(f'''SELECT jsonb_build_object('part',
                substring(payload FROM :part_offset FOR 8192))::text
                FROM ({sql}) AS document(payload)''', {**values, 'part_offset': offset})
            if not rows:
                if parts:
                    raise StoreError('STORAGE_CHANGED', 'The document changed while being read.')
                return None
            part = rows[0]['part']
            parts.append(part)
            if len(part) < 8192:
                try:
                    return json.loads(''.join(parts))
                except (TypeError, ValueError):
                    raise StoreError('STORAGE_CHANGED', 'The document changed while being read.') from None
            offset += len(part)

    @contextmanager
    def _transaction(self):
        transaction = None
        try:
            transaction = self.client.begin_transaction(**self.connection)['transactionId']
            yield transaction
            self.client.commit_transaction(resourceArn=self.connection['resourceArn'],
                                           secretArn=self.connection['secretArn'], transactionId=transaction)
        except Exception as error:
            if transaction:
                try:
                    self.client.rollback_transaction(resourceArn=self.connection['resourceArn'],
                                                     secretArn=self.connection['secretArn'], transactionId=transaction)
                except Exception:
                    pass
            if isinstance(error, StoreError):
                raise
            raise StoreError('STORAGE_UNAVAILABLE', 'Cloud storage could not commit the operation.') from None

    def create_task(self, context):
        task = CloudTask(str(uuid4()), str(uuid4()), context)
        self._execute('''INSERT INTO cloud_tasks(id, session_id, actor_id, project_id, service_id, source_ids)
                         VALUES(:id, :session, :actor, :project, :service, CAST(:sources AS jsonb))''',
                      {'id': task.id, 'session': task.session_id, **_scope(context)})
        return task

    def get_task(self, context, task_id):
        row = self._read_document(f'''SELECT (to_jsonb(t) || jsonb_build_object('goal',
                    COALESCE((SELECT r.prompt FROM cloud_turns r WHERE r.task_id = t.id
                              ORDER BY r.created_at, r.id LIMIT 1), '')))::text
                    FROM cloud_tasks t WHERE id = :id AND {_TASK_SCOPE}''',
                                  {'id': task_id, **_scope(context)})
        if row is None:
            raise StoreError('ACCESS_DENIED', 'The task is unavailable in the current access context.')
        return CloudTask(row['id'], row['session_id'], context, tuple(row['pending_tools']), row['goal'])

    def begin_turn(self, task):
        turn_id = str(uuid4())
        rows = self._execute(f'''UPDATE cloud_tasks SET current_turn = :turn
                                 WHERE id = :id AND current_turn IS NULL AND {_TASK_SCOPE}
                                   AND pending_tools = CAST(:pending AS jsonb)
                                 RETURNING jsonb_build_object('id', id)::text''',
                             {'id': task.id, 'turn': turn_id, 'pending': _json(task.pending_tools),
                              **_scope(task.context)})
        if not rows:
            raise StoreError('TASK_BUSY', 'The task changed or already has an invocation in progress.')
        return turn_id

    def finish_turn(self, task, turn_id, prompt, reply, recalled_ids):
        state = 'interrupted' if reply.error else 'waiting' if reply.pending_tools else 'active'
        with self._transaction() as transaction:
            rows = self._execute('''UPDATE cloud_tasks SET current_turn = NULL,
                                      pending_tools = CAST(:pending AS jsonb), state = :state
                                    WHERE id = :task AND current_turn = :turn
                                    RETURNING jsonb_build_object('id', id)::text''',
                                 {'task': task.id, 'turn': turn_id, 'pending': _json(reply.pending_tools),
                                  'state': state}, transaction)
            if not rows:
                raise StoreError('TURN_CONFLICT', 'The invocation no longer owns this task turn.')
            self._execute('''INSERT INTO cloud_turns(id, task_id, prompt, reply, recalled_ids)
                             VALUES(:id, :task, :prompt, CAST(:reply AS jsonb), CAST(:recall AS jsonb))''',
                          {'id': turn_id, 'task': task.id, 'prompt': prompt,
                           'reply': _json(asdict(reply)), 'recall': _json(recalled_ids)}, transaction)
            if reply.stop_reason == 'end_turn' and reply.text.strip() and not reply.error and not reply.pending_tools:
                self._execute('INSERT INTO cloud_knowledge_jobs(turn_id) VALUES(:id) ON CONFLICT DO NOTHING',
                              {'id': turn_id}, transaction)

    def search(self, context, query, vector, model_id, limit):
        if type(limit) is not int or not 1 <= limit <= 50:
            raise StoreError('INVALID_INPUT', 'Recall limit must be between 1 and 50.')
        parameters = {key: value for key, value in _scope(context).items() if key != 'actor'}
        parameters.update(query=query, limit=limit)
        # Materialization keeps scope and vector space filtering ahead of distance calculation.
        sql = '''WITH allowed AS MATERIALIZED (
                    SELECT * FROM cloud_knowledge
                    WHERE project_id = :project AND service_id = :service
                      AND source_ids <@ CAST(:sources AS jsonb)),
                 lexical AS (
                    SELECT id, row_number() OVER(ORDER BY ts_rank_cd(search_vector,
                        websearch_to_tsquery('simple', :query)) DESC, id) AS rank
                    FROM allowed WHERE search_vector @@ websearch_to_tsquery('simple', :query)
                    ORDER BY rank LIMIT 50)'''
        if vector is not None:
            if not isinstance(model_id, str) or not model_id:
                raise StoreError('INVALID_INPUT', 'Vector recall requires its embedding model identity.')
            parameters.update(embedding=_vector(vector), dimensions=len(vector), model=model_id)
            sql += ''', compatible AS MATERIALIZED (
                        SELECT * FROM allowed WHERE model_id = :model AND embedding IS NOT NULL
                          AND vector_dims(embedding) = :dimensions),
                      semantic AS (
                        SELECT id, row_number() OVER(ORDER BY embedding <=> CAST(:embedding AS vector), id) AS rank
                        FROM compatible ORDER BY rank LIMIT 50),
                      candidates AS (SELECT id, rank FROM lexical UNION ALL SELECT id, rank FROM semantic)'''
        else:
            sql += ', candidates AS (SELECT id, rank FROM lexical)'
        sql += ''', ranked AS (SELECT id, sum(1.0 / (60 + rank)) AS score FROM candidates GROUP BY id)
                  SELECT jsonb_build_object('id', m.id)::text
                  FROM ranked r JOIN allowed m ON m.id = r.id
                  ORDER BY r.score DESC, m.id LIMIT :limit'''
        matches = []
        for item in self._execute(sql, parameters):
            document = self._read_document('''SELECT jsonb_build_object('id', id, 'text', text,
                    'source_ids', source_ids)::text FROM cloud_knowledge
                    WHERE id = :id AND project_id = :project AND service_id = :service
                      AND source_ids <@ CAST(:sources AS jsonb)''',
                                          {'id': item['id'], **{key: parameters[key]
                                               for key in ('project', 'service', 'sources')}})
            if document is not None:
                matches.append(SharedMemory(document['id'], document['text'], frozenset(document['source_ids'])))
        return matches

    def claim_job(self):
        token = str(uuid4())
        rows = self._execute('''WITH picked AS (
                    SELECT turn_id FROM cloud_knowledge_jobs
                    WHERE state = 'pending' AND available_at <= now()
                      AND (lease_until IS NULL OR lease_until <= now())
                    ORDER BY available_at, turn_id FOR UPDATE SKIP LOCKED LIMIT 1),
                claimed AS (
                    UPDATE cloud_knowledge_jobs j SET lease_token = :token,
                        lease_until = now() + interval '5 minutes', attempts = attempts + 1
                    FROM picked p WHERE j.turn_id = p.turn_id RETURNING j.turn_id, j.lease_token)
                SELECT jsonb_build_object('id', turn_id, 'token', lease_token)::text FROM claimed''',
                             {'token': token})
        if not rows:
            return None
        claim = rows[0]
        row = self._read_document('''SELECT jsonb_build_object('id', j.turn_id, 'token', j.lease_token,
                    'actor', t.actor_id, 'project', t.project_id, 'service', t.service_id,
                    'sources', t.source_ids, 'prompt', CASE WHEN r.prompt ~ '[^[:space:]]' THEN r.prompt
                        ELSE (SELECT first.prompt FROM cloud_turns first WHERE first.task_id = t.id
                              ORDER BY first.created_at, first.id LIMIT 1) END,
                    'response', r.reply->>'text',
                    'text', k.text)::text
                FROM cloud_knowledge_jobs j JOIN cloud_turns r ON r.id = j.turn_id
                JOIN cloud_tasks t ON t.id = r.task_id LEFT JOIN cloud_knowledge k ON k.id = j.turn_id
                WHERE j.turn_id = :id AND j.lease_token = :token AND j.lease_until > now()''', claim)
        if row is None:
            raise StoreError('JOB_LEASE_EXPIRED', 'The knowledge job lease expired while being read.')
        context = AccessContext(row['actor'], row['project'], row['service'], frozenset(row['sources']))
        return KnowledgeJob(row['id'], row['token'], context, row['prompt'], row['response'], row['text'])

    def save_text(self, job, text):
        rows = self._execute(f'''WITH owned AS (
                    SELECT turn_id FROM cloud_knowledge_jobs WHERE {_LIVE_JOB}
                      AND state = 'pending' FOR UPDATE)
                    INSERT INTO cloud_knowledge(id, project_id, service_id, source_ids, text)
                    SELECT j.turn_id, t.project_id, t.service_id, t.source_ids, :text
                    FROM owned j JOIN cloud_turns r ON r.id = j.turn_id
                    JOIN cloud_tasks t ON t.id = r.task_id
                    ON CONFLICT(id) DO UPDATE SET text = EXCLUDED.text
                    RETURNING jsonb_build_object('id', id)::text''',
                             {'id': job.id, 'token': job.token, 'text': text})
        self._require_lease(rows)

    def complete_job(self, job, vector, model_id):
        if vector is not None and (not isinstance(model_id, str) or not model_id):
            raise StoreError('INVALID_INPUT', 'An embedding model identity is required.')
        with self._transaction() as transaction:
            rows = self._execute(f'''UPDATE cloud_knowledge_jobs SET state = 'complete', error_code = NULL,
                                      lease_until = NULL, lease_token = NULL
                                    WHERE {_LIVE_JOB} AND state = 'pending'
                                    RETURNING jsonb_build_object('id', turn_id)::text''',
                                 {'id': job.id, 'token': job.token}, transaction)
            self._require_lease(rows)
            if vector is not None:
                updated = self._execute('''UPDATE cloud_knowledge SET embedding = CAST(:embedding AS vector),
                                            model_id = :model WHERE id = :id
                                          RETURNING jsonb_build_object('id', id)::text''',
                                        {'id': job.id, 'embedding': _vector(vector), 'model': model_id}, transaction)
                if not updated:
                    raise StoreError('KNOWLEDGE_MISSING', 'Persist knowledge text before indexing.')

    def retry_job(self, job, code):
        rows = self._execute(f'''UPDATE cloud_knowledge_jobs SET lease_until = NULL, lease_token = NULL,
                                  error_code = :code, available_at = now() + interval '1 minute'
                                WHERE {_LIVE_JOB} AND state = 'pending'
                                RETURNING jsonb_build_object('id', turn_id)::text''',
                             {'id': job.id, 'token': job.token, 'code': code})
        self._require_lease(rows)

    @staticmethod
    def _require_lease(rows):
        if not rows:
            raise StoreError('JOB_LEASE_EXPIRED', 'Another processor owns this knowledge update.')
