"""Shared persistence for SQLite (local) and PostgreSQL (hosted).
Original snapshots are append-only. Legacy postings are kept, never rewritten.
"""
import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import (Column, ForeignKey, Integer, MetaData, String, Table, Text,
                        UniqueConstraint, create_engine, event, inspect, select, text)
from sqlalchemy.exc import IntegrityError
from settings import database_url

metadata = MetaData(schema='govtracker')
notices = Table('notices_v2', metadata,
    Column('id', String(64), primary_key=True),
    Column('latest_version', String(64), nullable=False),
    Column('created_at', String(40), nullable=False),
    Column('updated_at', String(40), nullable=False))
originals = Table('originals_v2', metadata,
    Column('id', String(64), primary_key=True),
    Column('notice_id', String(64), ForeignKey('notices_v2.id'), nullable=False, index=True),
    Column('content_hash', String(64), nullable=False),
    Column('payload', Text, nullable=False),
    Column('collected_at', String(40), nullable=False),
    UniqueConstraint('notice_id', 'content_hash'))
analyses = Table('analyses_v2', metadata,
    Column('id', String(64), primary_key=True),
    Column('original_id', String(64), ForeignKey('originals_v2.id'), nullable=False, index=True),
    Column('profile_hash', String(64), nullable=False),
    Column('prompt_version', String(80), nullable=False),
    Column('model', String(120), nullable=False),
    Column('result', Text, nullable=False),
    Column('created_at', String(40), nullable=False),
    UniqueConstraint('original_id', 'profile_hash', 'prompt_version', 'model'))
jobs = Table('jobs_v2', metadata,
    Column('id', String(64), primary_key=True),
    Column('trigger', String(40), nullable=False),
    Column('status', String(40), nullable=False),
    Column('started_at', String(40), nullable=False),
    Column('finished_at', String(40)), Column('report', Text))
locks = Table('locks_v2', metadata,
    Column('name', String(80), primary_key=True),
    Column('owner', String(64), nullable=False),
    Column('expires_at', String(40), nullable=False))
attempts = Table('ai_attempts_v2', metadata,
    Column('id', String(64), primary_key=True),
    Column('day_kst', String(10), nullable=False, index=True),
    Column('original_id', String(64), nullable=False),
    Column('created_at', String(40), nullable=False))
_engine = None


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def engine_for(url):
    opts = {'pool_pre_ping': True}
    if url.startswith('sqlite:'):
        opts['connect_args'] = {'check_same_thread': False, 'timeout': 30}
    engine = create_engine(url, **opts)
    if url.startswith('sqlite:'):
        @event.listens_for(engine, 'connect')
        def foreign_keys(connection, _):
            connection.execute('PRAGMA foreign_keys=ON')
    if url.startswith('sqlite:'):
        engine = engine.execution_options(schema_translate_map={'govtracker': None})
    return engine


def get_engine():
    global _engine
    if _engine is None:
        _engine = engine_for(database_url())
    return _engine


def init_db(engine=None):
    engine = engine or get_engine()
    with engine.begin() as conn:
        if engine.dialect.name == 'postgresql':
            conn.execute(text('CREATE SCHEMA IF NOT EXISTS govtracker'))
            conn.execute(text('REVOKE ALL ON SCHEMA govtracker FROM PUBLIC'))
        metadata.create_all(conn)
        if engine.dialect.name == 'postgresql':
            # Private schema is not exposed through Supabase public REST routes.
            # Backend uses the database owner; visitors never receive its credentials.
            for table in metadata.sorted_tables:
                conn.execute(text(f'ALTER TABLE govtracker.{table.name} ENABLE ROW LEVEL SECURITY'))
                conn.execute(text(f'REVOKE ALL ON TABLE govtracker.{table.name} FROM PUBLIC'))
    return engine


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def canonical_url(value):
    parts = urlsplit(str(value or '').strip())
    if parts.scheme not in ('http', 'https') or not parts.netloc:
        return ''
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in ('fbclid', 'gclid')]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path,
                       urlencode(sorted(query)), ''))


def valid_date(value):
    if not value:
        return None
    match = re.match(r'^(\d{4})[-./](\d{1,2})[-./](\d{1,2})(?:\D|$)', str(value).strip())
    if not match:
        return None
    try:
        return datetime(*map(int, match.groups())).date().isoformat()
    except ValueError:
        return None


def normalize_record(record):
    title = str(record.get('title') or '').strip()
    if not title:
        raise ValueError('공고 제목이 없습니다.')
    content = str(record.get('content') or '').strip()
    # Many legacy collectors duplicated the title instead of extracting a body.
    if content == title:
        content = ''
    payload = {
        'original_title': title, 'original_content': content,
        'original_url': canonical_url(record.get('url')),
        'organization': str(record.get('organization') or record.get('dept') or record.get('agency') or ''),
        'source': str(record.get('agency') or record.get('source') or ''),
        'notice_number': str(record.get('notice_number') or ''),
        'revision': str(record.get('revision') or ''),
        'published_date': valid_date(record.get('reg_date')),
        'application_start': valid_date(record.get('application_start')),
        'deadline': valid_date(record.get('due_date')),
        'region': record.get('region') or None,
        'organization_type': record.get('organization_type') or None,
        'budget': str(record.get('budget') or ''),
        'attachments': record.get('attachments') or [],
        'content_quality': 'body' if len(content) >= 80 else 'metadata_only',
        'data_origin': record.get('data_origin', 'collected'),
        'raw_record': record,
    }
    return payload


def notice_identity(payload):
    source = payload['source']
    if payload['notice_number']:
        return digest(f"{source}|number|{payload['notice_number']}")
    # The title guards against a collector returning one shared list URL.
    # Without a reliable notice number, prefer separate records over data loss.
    title = re.sub(r'\s+', ' ', payload['original_title']).casefold()
    return digest(json.dumps([source, payload['organization'], payload['original_url'],
                              title, payload['published_date']], ensure_ascii=False))


def save_notice(record, engine=None):
    engine = engine or get_engine()
    payload = normalize_record(record)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    notice_id = notice_identity(payload)
    # Views, HTML decoration and raw collection metadata must not trigger paid reanalysis.
    substance = {k: v for k, v in payload.items() if k != 'raw_record'}
    content_hash = digest(json.dumps(substance, ensure_ascii=False, sort_keys=True, default=str))
    original_id = digest(notice_id + content_hash)
    now = utcnow()
    with engine.begin() as conn:
        exists = conn.execute(select(notices).where(notices.c.id == notice_id)).mappings().first()
        if exists and exists['latest_version'] == original_id:
            return notice_id, original_id, 'unchanged'
        known_version = conn.execute(select(originals.c.id).where(originals.c.id == original_id)).first()
        if not exists:
            conn.execute(notices.insert().values(id=notice_id, latest_version=original_id,
                                                 created_at=now, updated_at=now))
        if not known_version:
            conn.execute(originals.insert().values(id=original_id, notice_id=notice_id,
                content_hash=content_hash, payload=encoded, collected_at=now))
        if exists:
            conn.execute(notices.update().where(notices.c.id == notice_id)
                         .values(latest_version=original_id, updated_at=now))
    return notice_id, original_id, 'updated' if exists else 'new'


def import_legacy(engine=None):
    engine = engine or get_engine()
    if 'postings' not in inspect(engine).get_table_names():
        return 0
    with engine.connect() as conn:
        records = [dict(r) for r in conn.execute(text('SELECT * FROM postings')).mappings()]
    imported = 0
    for record in records:
        # Do not reactivate retired product recommendations or unverifiable old AI results.
        clean = {k: record.get(k) for k in ('agency', 'source', 'title', 'dept',
                 'reg_date', 'due_date', 'url', 'budget', 'attach')}
        clean['data_origin'] = 'legacy_unverified'
        _, _, state = save_notice(clean, engine)
        imported += state == 'new'
    return imported


def save_analysis(original_id, result, profile_hash, prompt_version, model, engine=None):
    engine = engine or get_engine()
    key = digest('|'.join([original_id, profile_hash, prompt_version, model]))
    with engine.begin() as conn:
        if conn.execute(select(analyses.c.id).where(analyses.c.id == key)).first():
            return False
        conn.execute(analyses.insert().values(id=key, original_id=original_id,
            profile_hash=profile_hash, prompt_version=prompt_version, model=model,
            result=json.dumps(result, ensure_ascii=False), created_at=utcnow()))
    return True


def list_notices(profile_hash, prompt_version, model, engine=None):
    engine = engine or get_engine()
    join = notices.join(originals, notices.c.latest_version == originals.c.id).outerjoin(
        analyses, (analyses.c.original_id == originals.c.id) &
        (analyses.c.profile_hash == profile_hash) &
        (analyses.c.prompt_version == prompt_version) & (analyses.c.model == model))
    query = select(notices.c.id, notices.c.created_at, notices.c.updated_at,
        originals.c.id.label('original_id'), originals.c.payload, originals.c.collected_at,
        analyses.c.result, analyses.c.created_at.label('analyzed_at')).select_from(join)
    with engine.connect() as conn:
        result = []
        for row in conn.execute(query).mappings():
            item = dict(row)
            item['original'] = json.loads(item.pop('payload'))
            item['analysis'] = json.loads(item.pop('result')) if row['result'] else None
            result.append(item)
        return sorted(result, key=lambda r: r['updated_at'], reverse=True)


def acquire_lock(owner, seconds=3600, engine=None):
    engine = engine or get_engine()
    now = utcnow()
    expiry = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()
    try:
        with engine.begin() as conn:
            conn.execute(locks.delete().where(locks.c.expires_at < now))
            conn.execute(locks.insert().values(name='pipeline', owner=owner, expires_at=expiry))
        return True
    except IntegrityError:
        return False


def release_lock(owner, engine=None):
    with (engine or get_engine()).begin() as conn:
        conn.execute(locks.delete().where(locks.c.owner == owner))


def start_job(trigger, engine=None):
    key = uuid.uuid4().hex
    with (engine or get_engine()).begin() as conn:
        conn.execute(jobs.insert().values(id=key, trigger=trigger, status='running', started_at=utcnow()))
    return key


def finish_job(key, report, status, engine=None):
    with (engine or get_engine()).begin() as conn:
        conn.execute(jobs.update().where(jobs.c.id == key).values(
            finished_at=utcnow(), status=status, report=json.dumps(report, ensure_ascii=False)))


def recent_jobs(engine=None):
    with (engine or get_engine()).connect() as conn:
        rows = conn.execute(select(jobs).order_by(jobs.c.started_at.desc()).limit(10)).mappings()
        return [dict(r) for r in rows]


def reserve_ai_attempt(original_id, daily_limit, engine=None):
    """Called under the pipeline lock; all calls including failures count toward the budget."""
    from sqlalchemy import func
    from zoneinfo import ZoneInfo
    day = datetime.now(ZoneInfo('Asia/Seoul')).date().isoformat()
    with (engine or get_engine()).begin() as conn:
        count = conn.execute(select(func.count()).select_from(attempts).where(attempts.c.day_kst == day)).scalar_one()
        if count >= daily_limit:
            return False
        conn.execute(attempts.insert().values(id=uuid.uuid4().hex, day_kst=day,
                                             original_id=original_id, created_at=utcnow()))
        return True
