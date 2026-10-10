"""SQLite jobs, transactional event outbox, claims and filesystem image storage."""
import base64
import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from urllib.parse import urlsplit

from common import Problem, canonical, iso

MAX_IMAGE = 8 * 1024 * 1024
MIME = {'image/png': 'PNG', 'image/jpeg': 'JPEG', 'image/webp': 'WEBP'}


class JobInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    prompt: str = Field(min_length=1, max_length=16000)
    aspect_ratio: str | None = Field(default=None, pattern=r'^[1-9]\d{0,2}:[1-9]\d{0,2}$')
    width: StrictInt | None = Field(default=None, ge=1, le=8192)
    height: StrictInt | None = Field(default=None, ge=1, le=8192)
    reference_image_urls: list[str] = Field(default_factory=list, max_length=8)
    output_preferences: dict = Field(default_factory=dict)

    @field_validator('prompt')
    @classmethod
    def prompt_not_blank(cls, value):
        if not value.strip():
            raise ValueError('Prompt cannot be blank')
        return value

    @field_validator('reference_image_urls')
    @classmethod
    def validate_urls(cls, values):
        for value in values:
            parts = urlsplit(value)
            if len(value) > 2048 or parts.scheme != 'https' or not parts.hostname or parts.username or parts.password:
                raise ValueError('Reference images must use HTTPS URLs without credentials')
        return values

    @field_validator('output_preferences')
    @classmethod
    def validate_preferences(cls, value):
        if len(canonical(value)) > 4096:
            raise ValueError('Output preferences too large')
        return value


class GetRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    job_id: str = Field(pattern=r'^img_[0-9a-f]{32}$')
    claim_id: str | None = Field(default=None, min_length=8, max_length=128)


class JobID(BaseModel):
    model_config = ConfigDict(extra='forbid')
    job_id: str = Field(pattern=r'^img_[0-9a-f]{32}$')


class Submission(BaseModel):
    model_config = ConfigDict(extra='forbid')
    job_id: str = Field(pattern=r'^img_[0-9a-f]{32}$')
    attempt: StrictInt = Field(ge=1)
    claim_token: str = Field(min_length=32, max_length=128)
    generation_status: Literal['completed', 'failed']
    media_type: Literal['image/png', 'image/jpeg', 'image/webp'] | None = None
    image_base64: str | None = Field(default=None, max_length=12 * 1024 * 1024)
    error_details: str | None = Field(default=None, min_length=1, max_length=1000)


def image_bytes(encoded, media_type):
    if not encoded or media_type not in MIME:
        raise Problem(400, 'Image data and media_type are required')
    try:
        data = base64.b64decode(encoded, validate=True)
        if not data or len(data) > MAX_IMAGE:
            raise ValueError('Invalid image size')
        with Image.open(io.BytesIO(data)) as image:
            if image.format != MIME[media_type] or image.width > 8192 or image.height > 8192 or image.width * image.height > 24_000_000 or getattr(image, 'n_frames', 1) != 1:
                raise ValueError('Invalid image dimensions or format')
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            # Re-encode the pixels to remove metadata and trailing/polyglot content.
            clean = image.convert('RGBA' if media_type != 'image/jpeg' else 'RGB')
            output = io.BytesIO()
            clean.save(output, format=MIME[media_type])
            data = output.getvalue()
            if len(data) > MAX_IMAGE:
                raise ValueError('Normalized image too large')
            return data
    except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise Problem(400, 'Invalid, oversized, or unsupported image') from exc


class Store:
    def __init__(self, db_path, image_dir, clock=time.time):
        self.db_path, self.image_dir, self.clock = str(db_path), Path(image_dir), clock
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.image_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, tenant TEXT NOT NULL, prompt TEXT NOT NULL,
                    project_id TEXT, queue_id TEXT, aspect_ratio TEXT,
                    width INTEGER, height INTEGER, reference_urls TEXT NOT NULL,
                    output_preferences TEXT NOT NULL, status TEXT NOT NULL,
                    image_file TEXT, media_type TEXT, error TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event_outbox (
                    event_id TEXT PRIMARY KEY, tenant TEXT NOT NULL,
                    job_id TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                    data TEXT NOT NULL, created_at TEXT NOT NULL
                );
            ''')
            if db.execute('PRAGMA user_version').fetchone()[0] == 0:
                migration = Path(__file__).with_name('migrations').joinpath('001_events.sql').read_text()
                db.executescript('BEGIN IMMEDIATE;\n' + migration + '\nPRAGMA user_version=1; COMMIT;')
        os.chmod(self.db_path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def _event(self, db, row):
        stamp = self.clock()
        data = {k: row[k] for k in ('aspect_ratio', 'attempt')}
        data.update(job_id=row['id'], prompt_preview=row['prompt'][:100], created_at=iso(stamp))
        eid = 'evt_' + uuid.uuid4().hex
        db.execute('INSERT INTO event_outbox VALUES (?,?,?,?,?,?)', (eid, row['tenant'], row['id'], 'image.requested', canonical(data), iso(stamp)))
        subscriptions = db.execute('''SELECT id FROM subscriptions
            WHERE tenant=? AND active=1 AND expires_at>? AND arguments=?''',
            (row['tenant'], stamp, canonical({}))).fetchall()
        for subscription in subscriptions:
            db.execute('INSERT INTO deliveries (event_id, subscription_id, available_at) VALUES (?,?,?)', (eid, subscription['id'], stamp))
        return eid

    def _idempotency(self, db, tenant, key, fingerprint):
        if not isinstance(key, str) or not 8 <= len(key) <= 128:
            raise Problem(400, 'Idempotency-Key must contain 8 to 128 characters')
        cached = db.execute('SELECT * FROM idempotency WHERE tenant=? AND key=?', (tenant, key)).fetchone()
        if cached:
            if cached['request_hash'] != fingerprint:
                raise Problem(409, 'Idempotency key was used for a different request')
            return json.loads(cached['response'])

    def create(self, tenant, args, key):
        args = JobInput.model_validate(args)
        fingerprint = hashlib.sha256(('create:' + canonical(args.model_dump())).encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            cached = self._idempotency(db, tenant, key, fingerprint)
            if cached:
                return cached
            jid, stamp = 'img_' + uuid.uuid4().hex, iso(self.clock())
            db.execute('''INSERT INTO jobs (id,tenant,prompt,project_id,queue_id,aspect_ratio,width,height,reference_urls,output_preferences,status,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''', (jid, tenant, args.prompt, None, None, args.aspect_ratio,
                args.width, args.height, canonical(args.reference_image_urls), canonical(args.output_preferences), 'pending', stamp, stamp))
            row = dict(db.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone())
            self._event(db, row)
            response = {'job_id': jid, 'status': 'pending', 'attempt': 1, 'created_at': stamp}
            db.execute('INSERT INTO idempotency VALUES (?,?,?,?)', (tenant, key, fingerprint, canonical(response)))
        return response

    def get(self, tenant, jid):
        with self.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=? AND tenant=?', (jid, tenant)).fetchone()
        if row is None:
            raise Problem(404, 'Job not found')
        return dict(row)

    def request(self, tenant, jid, claim_id=None):
        if claim_id:
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT * FROM jobs WHERE id=? AND tenant=?', (jid, tenant)).fetchone()
                if not row:
                    raise Problem(404, 'Job not found')
                if row['status'] in ('completed', 'failed'):
                    raise Problem(409, 'Job already finalized; check its status')
                if row['lease_until'] and row['lease_until'] > self.clock() and row['claim_id'] != claim_id:
                    raise Problem(409, 'Job is already processing')
                token = row['claim_token'] if row['claim_id'] == claim_id and row['lease_until'] > self.clock() else secrets.token_urlsafe(32)
                db.execute("UPDATE jobs SET status='processing', claim_id=?, claim_token=?, lease_until=?, updated_at=? WHERE id=?",
                           (claim_id, token, self.clock() + 1800, iso(self.clock()), jid))
        row = self.get(tenant, jid)
        value = {'job_id': jid, 'prompt': row['prompt'], 'aspect_ratio': row['aspect_ratio'], 'width': row['width'], 'height': row['height'],
                 'reference_image_urls': json.loads(row['reference_urls']), 'output_preferences': json.loads(row['output_preferences']),
                 'status': row['status'], 'attempt': row['attempt']}
        if claim_id:
            value.update(claim_token=row['claim_token'], claim_expires_at=iso(row['lease_until']))
        return value

    def status(self, tenant, jid):
        row = self.get(tenant, jid)
        with self.connect() as db:
            counts = db.execute('''SELECT d.state, COUNT(*) AS n FROM deliveries d JOIN event_outbox e ON e.event_id=d.event_id
                WHERE e.job_id=? AND json_extract(e.data, '$.attempt')=? GROUP BY d.state''', (jid, row['attempt'])).fetchall()
        return {'job_id': jid, 'status': row['status'], 'attempt': row['attempt'],
                'generated_image_url': f'/jobs/{jid}/image' if row['image_file'] else None,
                'error_message': row['error'], 'created_at': row['created_at'], 'updated_at': row['updated_at'],
                'processing_expires_at': iso(row['lease_until']) if row['lease_until'] and row['status'] == 'processing' else None,
                'deliveries': {x['state']: x['n'] for x in counts}}

    def submit(self, tenant, raw):
        args = Submission.model_validate(raw)
        row = self.get(tenant, args.job_id)
        if row['attempt'] != args.attempt or not row['claim_token'] or not hmac.compare_digest(row['claim_token'], args.claim_token):
            raise Problem(409, 'Stale attempt or invalid claim token')
        data = None
        if args.generation_status == 'completed':
            data = image_bytes(args.image_base64, args.media_type)
            if args.error_details:
                raise Problem(400, 'Completed result cannot contain error_details')
        elif not args.error_details or args.image_base64 or args.media_type:
            raise Problem(400, 'Failed result requires error_details and no image')
        result_hash = hashlib.sha256((args.generation_status + ':' + (args.error_details or '')).encode() + (data or b'')).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM jobs WHERE id=? AND tenant=?', (args.job_id, tenant)).fetchone()
            if row['attempt'] != args.attempt or not hmac.compare_digest(row['claim_token'] or '', args.claim_token):
                raise Problem(409, 'Stale attempt or invalid claim token')
            if row['status'] in ('completed', 'failed'):
                if row['result_hash'] != result_hash:
                    raise Problem(409, 'Job already has a different final result')
            else:
                if row['lease_until'] is None or row['lease_until'] <= self.clock():
                    raise Problem(409, 'Processing claim expired; reclaim the job')
                filename = None
                if data is not None:
                    extension = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp'}[args.media_type]
                    filename = args.job_id + '_' + result_hash + extension
                    fd, temp = tempfile.mkstemp(dir=self.image_dir)
                    try:
                        with os.fdopen(fd, 'wb') as handle:
                            handle.write(data)
                            handle.flush()
                            os.fsync(handle.fileno())
                        os.replace(temp, self.image_dir / filename)
                        dirfd = os.open(self.image_dir, os.O_DIRECTORY)
                        try:
                            os.fsync(dirfd)
                        finally:
                            os.close(dirfd)
                    finally:
                        if os.path.exists(temp):
                            os.unlink(temp)
                db.execute('UPDATE jobs SET status=?, image_file=?, media_type=?, error=?, result_hash=?, updated_at=? WHERE id=?',
                           (args.generation_status, filename, args.media_type, args.error_details, result_hash, iso(self.clock()), args.job_id))
        return self.status(tenant, args.job_id)

    def retry(self, tenant, jid, key):
        fingerprint = hashlib.sha256(('retry:' + jid).encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            cached = self._idempotency(db, tenant, key, fingerprint)
            if cached:
                return cached
            row = db.execute('SELECT * FROM jobs WHERE id=? AND tenant=?', (jid, tenant)).fetchone()
            if not row:
                raise Problem(404, 'Job not found')
            if row['status'] == 'completed' or (row['status'] == 'processing' and row['lease_until'] > self.clock()):
                raise Problem(409, 'Cannot retry a completed job or an active processing claim')
            db.execute("UPDATE deliveries SET state='canceled', last_error='job_retried' WHERE event_id IN (SELECT event_id FROM event_outbox WHERE job_id=?) AND state IN ('queued','sending')", (jid,))
            db.execute("UPDATE jobs SET status='pending', attempt=attempt+1, error=NULL, claim_id=NULL, claim_token=NULL, lease_until=NULL, result_hash=NULL, updated_at=? WHERE id=?", (iso(self.clock()), jid))
            row = dict(db.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone())
            self._event(db, row)
            response = {'job_id': jid, 'status': 'pending', 'attempt': row['attempt']}
            db.execute('INSERT INTO idempotency VALUES (?,?,?,?)', (tenant, key, fingerprint, canonical(response)))
        return response
