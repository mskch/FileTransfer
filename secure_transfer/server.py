"""mTLS file service. Run with python -m secure_transfer.server --config ..."""
import argparse
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import signal
import socket
import sqlite3
import ssl
import time
import uuid
from .protocol import CHUNK, REQUEST, RESPONSE, PING, PONG, ProtocolError, encode, receive_async

LOG = logging.getLogger('sft.server')

class Denied(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message

def integer(value, name, maximum=2**63-1):
    if type(value) is not int or not 0 <= value <= maximum:
        raise Denied('BAD_REQUEST', f'invalid {name}')
    return value

def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{32}', value):
        raise Denied('BAD_REQUEST', 'invalid identifier')
    return value

def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(CHUNK), b''):
            h.update(block)
    return h.hexdigest()

class Bucket:
    """Aggregate per-user token bucket, shared by all connections."""
    def __init__(self, rate, capacity=None):
        self.rate = rate
        self.capacity = capacity or max(rate, CHUNK)
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self.lock = asyncio.Lock()

    async def take(self, amount):
        async with self.lock:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now-self.updated)*self.rate)
            self.updated = now
            if amount > self.tokens:
                await asyncio.sleep((amount-self.tokens)/self.rate)
                self.updated = time.monotonic()
                self.tokens = 0
            else:
                self.tokens -= amount

class Service:
    def __init__(self, config):
        self.cfg = config
        self.root = Path(config['storage']).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.root / 'metadata.sqlite3')
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS files (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL,
            size INTEGER NOT NULL, sha256 TEXT NOT NULL, offset INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'pending', created REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS owner_idx ON files(owner, state);
        ''')
        self.users = {}
        for user in config['users']:
            name = user['name']
            if not re.fullmatch(r'[a-zA-Z0-9_-]{1,40}', name):
                raise ValueError('unsafe configured user name')
            if user['fingerprint'] in self.users or any(u['name']==name for u in self.users.values()):
                raise ValueError('duplicate user identity')
            if user['rate'] <= 0 or user['quota'] < 0:
                raise ValueError('invalid rate or quota')
            self.users[user['fingerprint']] = user
            (self.root / name).mkdir(exist_ok=True, mode=0o700)
        self.active = 0
        self.by_user = {}
        self.locks = {}
        self.bandwidth = {u['name']: Bucket(u['rate']) for u in self.users.values()}
        self.requests = {u['name']: Bucket(config.get('requests_per_second', 100), 100) for u in self.users.values()}
        self.server = None
        self.connections = set()
        self.handlers = set()
        self.recover()

    def path(self, row, suffix):
        # Client filenames are display metadata only; never filesystem paths.
        return self.root / row['owner'] / (row['id'] + suffix)

    def recover(self):
        # ACK means fsynced file + committed database offset. Discard unacknowledged tails.
        for row in self.db.execute("SELECT * FROM files WHERE state='pending'").fetchall():
            part, final = self.path(row, '.part'), self.path(row, '.bin')
            if final.exists() and final.stat().st_size == row['size'] and digest(final) == row['sha256']:
                self.db.execute("UPDATE files SET state='complete', offset=size WHERE id=?", (row['id'],))
            elif part.exists():
                offset = min(part.stat().st_size, row['offset'])
                with open(part, 'r+b') as f:
                    f.truncate(offset)
                self.db.execute('UPDATE files SET offset=? WHERE id=?', (offset, row['id']))
            else:
                self.db.execute('DELETE FROM files WHERE id=?', (row['id'],))
        self.db.commit()

    def row(self, user, file_id):
        identifier(file_id)
        row = self.db.execute('SELECT * FROM files WHERE id=? AND owner=?', (file_id, user['name'])).fetchone()
        if row is None:
            raise Denied('NOT_FOUND', 'file or transfer not found')
        return row

    async def start(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        ctx.load_cert_chain(self.cfg['cert'], self.cfg['key'])
        ctx.load_verify_locations(self.cfg['ca'])
        ctx.verify_mode = ssl.CERT_REQUIRED
        self.server = await asyncio.start_server(self.handle, self.cfg.get('host', '127.0.0.1'), self.cfg.get('port', 8443),
            ssl=ctx, ssl_handshake_timeout=5, ssl_shutdown_timeout=3, limit=CHUNK+16384, backlog=64)
        return self.server

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        writers = list(self.connections)
        for writer in writers:
            writer.close()
        if writers:
            await asyncio.gather(*(w.wait_closed() for w in writers), return_exceptions=True)
        tasks = list(self.handlers)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.db.close()

    async def send(self, writer, kind, meta, data=b''):
        writer.write(encode(kind, meta, data))
        await asyncio.wait_for(writer.drain(), timeout=30)

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        admitted = False
        user = None
        try:
            cert = writer.get_extra_info('ssl_object').getpeercert(binary_form=True)
            user = self.users.get(hashlib.sha256(cert).hexdigest())
            if user is None:
                return
            if self.active >= self.cfg.get('max_connections', 32) or self.by_user.get(user['name'], 0) >= self.cfg.get('connections_per_user', 5):
                await self.send(writer, RESPONSE, {'ok': False, 'code': 'BUSY', 'message': 'connection limit'})
                return
            self.active += 1
            self.by_user[user['name']] = self.by_user.get(user['name'], 0) + 1
            self.connections.add(writer)
            admitted = True
            writer.get_extra_info('socket').setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            hello = False
            while True:
                kind, meta, payload = await asyncio.wait_for(receive_async(reader), self.cfg.get('idle_timeout', 60))
                request_id = meta.get('request_id')
                if not isinstance(request_id, str) or len(request_id) > 64:
                    raise ProtocolError('invalid request id')
                await self.requests[user['name']].take(1)
                if kind == PING:
                    if payload:
                        raise ProtocolError('ping payload')
                    await self.send(writer, PONG, {'request_id': request_id, 'ok': True})
                    continue
                if kind != REQUEST:
                    raise ProtocolError('expected request')
                op = meta.get('op')
                try:
                    if payload and op != 'DATA':
                        raise Denied('BAD_REQUEST', 'unexpected payload')
                    if not hello and op != 'HELLO':
                        raise Denied('AUTH_REQUIRED', 'HELLO required')
                    if op == 'HELLO':
                        hello = True
                        result, data = {'user': user['name'], 'rate': user['rate'], 'chunk_size': CHUNK}, b''
                    else:
                        result, data = await self.dispatch(user, op, meta, payload)
                    await self.send(writer, RESPONSE, dict(result, ok=True, request_id=request_id), data)
                    if op == 'QUIT':
                        break
                except Denied as exc:
                    LOG.info('rejected user=%s op=%s code=%s', user['name'], op, exc.code)
                    await self.send(writer, RESPONSE, {'ok': False, 'request_id': request_id, 'code': exc.code, 'message': exc.message})
        except (EOFError, asyncio.IncompleteReadError, ConnectionError, ssl.SSLError, TimeoutError, ProtocolError):
            pass
        except Exception:
            LOG.exception('connection failed')
        finally:
            self.handlers.discard(task)
            if admitted:
                self.active -= 1
                self.by_user[user['name']] -= 1
                self.connections.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, ssl.SSLError, TimeoutError):
                pass

    async def dispatch(self, user, op, m, payload):
        if op == 'QUIT':
            return {}, b''
        if op == 'LIST':
            after = m.get('after', '')
            if not isinstance(after, str) or len(after)>32:
                raise Denied('BAD_REQUEST', 'invalid cursor')
            rows = self.db.execute('SELECT id,name,size,sha256,state,offset FROM files WHERE owner=? AND id>? ORDER BY id LIMIT 10', (user['name'], after)).fetchall()
            return {'files': [dict(r) for r in rows], 'next': rows[-1]['id'] if len(rows)==10 else None}, b''
        if op == 'UPLOAD_INIT':
            name = m.get('name')
            if not isinstance(name, str) or not 1 <= len(name.encode('utf-8')) <= 180 or any(c in name for c in '/\\\x00') or name in ('.', '..') or any(ord(c)<32 for c in name):
                raise Denied('BAD_REQUEST', 'invalid filename')
            size = integer(m.get('size'), 'size', self.cfg.get('max_file_size', 2**30))
            sha = m.get('sha256')
            if not isinstance(sha, str) or not re.fullmatch('[a-f0-9]{64}', sha):
                raise Denied('BAD_REQUEST', 'invalid sha256')
            usage, count = self.db.execute('SELECT COALESCE(SUM(size),0),COUNT(*) FROM files WHERE owner=?', (user['name'],)).fetchone()
            if usage + size > user['quota'] or count >= self.cfg.get('max_files_per_user', 1000):
                raise Denied('QUOTA', 'storage or file count quota exceeded')
            fid = uuid.uuid4().hex
            self.db.execute('INSERT INTO files(id,owner,name,size,sha256,created) VALUES(?,?,?,?,?,?)', (fid,user['name'],name,size,sha,time.time()))
            try:
                with open(self.root/user['name']/(fid+'.part'), 'xb'):
                    pass
                self.db.commit()
            except OSError:
                self.db.rollback()
                raise Denied('STORAGE', 'cannot create upload')
            LOG.info('upload initialized user=%s id=%s bytes=%s', user['name'], fid, size)
            return {'transfer_id': fid, 'offset': 0}, b''
        if op not in ('RESUME', 'DATA', 'COMMIT', 'DOWNLOAD_INIT', 'READ', 'CANCEL'):
            raise Denied('BAD_REQUEST', 'unknown operation')
        fid = identifier(m.get('transfer_id'))
        # Reference-count lock entries so a completed request does not leak a lock per file forever.
        entry = self.locks.setdefault(fid, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                row = self.row(user, fid)
                if op == 'RESUME':
                    return dict(row), b''
                if op == 'CANCEL':
                    if row['state'] != 'pending':
                        raise Denied('STATE', 'cannot cancel a completed file')
                    self.path(row,'.part').unlink(missing_ok=True)
                    self.db.execute('DELETE FROM files WHERE id=? AND owner=?',(fid,user['name']))
                    self.db.commit()
                    return {}, b''
                if op == 'DATA':
                    offset = integer(m.get('offset'), 'offset')
                    if row['state'] != 'pending' or offset != row['offset'] or not payload or offset+len(payload)>row['size']:
                        raise Denied('OFFSET', 'invalid state, offset or data length; RESUME first')
                    await self.bandwidth[user['name']].take(len(payload))
                    with open(self.path(row,'.part'),'r+b') as f:
                        f.seek(offset)
                        f.write(payload)
                        f.flush()
                        os.fsync(f.fileno())
                    end = offset+len(payload)
                    self.db.execute('UPDATE files SET offset=? WHERE id=?',(end,fid))
                    self.db.commit()
                    return {'offset':end}, b''
                if op == 'COMMIT':
                    if row['state'] == 'complete':
                        return {'file_id':fid,'sha256':row['sha256']}, b''
                    if row['offset'] != row['size']:
                        raise Denied('INCOMPLETE','file has missing bytes')
                    path = self.path(row,'.part')
                    if await asyncio.to_thread(digest,path) != row['sha256']:
                        raise Denied('HASH_MISMATCH','file checksum mismatch; cancel and reupload')
                    os.replace(path,self.path(row,'.bin'))
                    if os.name == 'posix':
                        fd = os.open(path.parent,os.O_RDONLY)
                        try:
                            os.fsync(fd)
                        finally:
                            os.close(fd)
                    self.db.execute("UPDATE files SET state='complete' WHERE id=?",(fid,))
                    self.db.commit()
                    LOG.info('upload committed user=%s id=%s',user['name'],fid)
                    return {'file_id':fid,'sha256':row['sha256']}, b''
                if row['state'] != 'complete':
                    raise Denied('STATE','file is not complete')
                if op == 'DOWNLOAD_INIT':
                    return {'name':row['name'],'size':row['size'],'sha256':row['sha256']}, b''
                offset = integer(m.get('offset'),'offset',row['size'])
                length = integer(m.get('length',CHUNK),'length',CHUNK)
                if length == 0:
                    raise Denied('BAD_REQUEST','empty read')
                length = min(length,row['size']-offset)
                await self.bandwidth[user['name']].take(length)
                with open(self.path(row,'.bin'),'rb') as f:
                    f.seek(offset)
                    data = f.read(length)
                return {'offset':offset,'eof':offset+len(data)==row['size']},data
        finally:
            entry[1] -= 1
            if not entry[1]:
                self.locks.pop(fid,None)

async def run(config):
    service = Service(config)
    server = await service.start()
    LOG.info('listening on %s',server.sockets[0].getsockname())
    done = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT,signal.SIGTERM):
        try:
            loop.add_signal_handler(sig,done.set)
        except NotImplementedError:
            pass
    try:
        await done.wait()
    finally:
        await service.stop()

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',required=True)
    args=parser.parse_args()
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    os.umask(0o077)
    try:
        asyncio.run(run(json.loads(Path(args.config).read_text())))
    except KeyboardInterrupt:
        pass

if __name__=='__main__':
    main()
