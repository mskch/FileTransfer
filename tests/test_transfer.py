import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import threading
import pytest
from secure_transfer.client import Client, Paused, RemoteError, sha256
from secure_transfer.protocol import CHUNK, HEADER, MAGIC, VERSION, MAX_META, ProtocolError, encode, receive, REQUEST, PING
from secure_transfer.server import Service

@pytest.fixture(scope='module')
def environment(tmp_path_factory):
    root=tmp_path_factory.mktemp('sft')/'runtime'
    subprocess.run([sys.executable,'scripts/bootstrap.py','--output',str(root),'--port','0'],check=True)
    config=json.loads((root/'server.json').read_text());config['users'][0]['rate']=16*1024*1024;config['users'][1]['rate']=32*1024*1024
    loop=asyncio.new_event_loop();thread=threading.Thread(target=loop.run_forever,daemon=True);thread.start()
    async def start():
        service=Service(config);server=await service.start()
        return service,server.sockets[0].getsockname()[1]
    service,port=asyncio.run_coroutine_threadsafe(start(),loop).result(10)
    clients={}
    for name in ('alice','bob'):
        clients[name]=json.loads((root/(name+'.json')).read_text());clients[name]['port']=port
    yield root,clients,service,loop
    asyncio.run_coroutine_threadsafe(service.stop(),loop).result(10)
    loop.call_soon_threadsafe(loop.stop);thread.join(5);loop.close()

@pytest.mark.parametrize('size',[0,1,CHUNK-1,CHUNK,CHUNK+1,3*CHUNK+177])
def test_roundtrip(environment,tmp_path,size):
    _,configs,_,_=environment
    source=tmp_path/('中文 文件 '+str(size)+'.bin');source.write_bytes(os.urandom(size))
    with Client(configs['alice']) as client:
        connection=client.sock;fid=client.upload(source);output=tmp_path/'received.bin';client.download(fid,output)
        assert output.read_bytes()==source.read_bytes()
        assert client.sock is connection
        assert any(r['id']==fid and r['state']=='complete' for r in client.files())

def test_resume_upload_download(environment,tmp_path):
    _,configs,_,_=environment
    source=tmp_path/'resume.bin';source.write_bytes(os.urandom(3*CHUNK+111));stop=threading.Event()
    def pause(done,total):
        if done>=CHUNK:stop.set()
    with Client(configs['alice']) as client:
        with pytest.raises(Paused):client.upload(source,progress=pause,pause=stop)
    state=json.loads(Path(str(source)+'.sft-upload.json').read_text())
    with Client(configs['alice']) as client:
        info,_=client.request('RESUME',transfer_id=state['transfer_id']);assert info['offset']==CHUNK
        stop.clear();fid=client.upload(source);target=tmp_path/'download.bin'
        with pytest.raises(Paused):client.download(fid,target,progress=pause,pause=stop)
    with Client(configs['alice']) as client:
        observed=[];client.download(fid,target,progress=lambda n,t:observed.append(n))
        assert observed[0]==CHUNK
        assert sha256(target)==sha256(source)

def test_isolation_and_injection(environment,tmp_path):
    _,configs,_,_=environment
    source=tmp_path/"quote ' OR 1=1 --.txt";source.write_bytes(b'private')
    with Client(configs['alice']) as alice,Client(configs['bob']) as bob:
        fid=alice.upload(source)
        assert any(x['name']==source.name for x in alice.files())
        assert not any(x['id']==fid for x in bob.files())
        for op in ('RESUME','DOWNLOAD_INIT','READ','CANCEL','COMMIT'):
            with pytest.raises(RemoteError,match='NOT_FOUND'):bob.request(op,transfer_id=fid)
        with pytest.raises(RemoteError,match='BAD_REQUEST'):alice.request('RESUME',transfer_id="' OR 1=1 --")

@pytest.mark.parametrize('name',['../escape','/tmp/escape','..','a\\b','x\x00y'])
def test_unsafe_names(environment,name):
    _,configs,_,_=environment
    with Client(configs['alice']) as client:
        with pytest.raises(RemoteError,match='BAD_REQUEST'):client.request('UPLOAD_INIT',name=name,size=1,sha256='0'*64)

def test_corruption_and_offsets(environment):
    _,configs,_,_=environment
    with Client(configs['alice']) as client:
        info,_=client.request('UPLOAD_INIT',name='corrupt.bin',size=3,sha256=hashlib.sha256(b'abc').hexdigest());fid=info['transfer_id']
        with pytest.raises(RemoteError,match='INCOMPLETE'):client.request('COMMIT',transfer_id=fid)
        with pytest.raises(RemoteError,match='OFFSET'):client.request('DATA',b'abc',transfer_id=fid,offset=1)
        client.request('DATA',b'xyz',transfer_id=fid,offset=0)
        with pytest.raises(RemoteError,match='HASH_MISMATCH'):client.request('COMMIT',transfer_id=fid)
        with pytest.raises(RemoteError,match='STATE'):client.request('DOWNLOAD_INIT',transfer_id=fid)
        client.request('CANCEL',transfer_id=fid)

def test_wrong_server_identity(environment):
    _,configs,_,_=environment
    with pytest.raises(ssl.SSLCertVerificationError):Client(dict(configs['alice'],server_name='attacker.invalid')).connect()

def test_missing_client_certificate(environment):
    _,configs,_,_=environment;config=configs['alice'];context=ssl.create_default_context(cafile=config['ca'])
    with pytest.raises((ssl.SSLError,EOFError,ConnectionError)):
        with socket.create_connection((config['host'],config['port'])) as raw:
            with context.wrap_socket(raw,server_hostname='localhost') as sock:
                sock.settimeout(3);sock.sendall(encode(REQUEST,{'op':'HELLO','request_id':'1'}));receive(sock)

def test_parallel_transfers(environment,tmp_path):
    _,configs,_,_=environment
    def work(i):
        source=tmp_path/f'parallel-{i}';source.write_bytes(os.urandom(CHUNK+99))
        with Client(configs['alice' if i%2 else 'bob']) as client:
            fid=client.upload(source);dest=tmp_path/f'dest-{i}';client.download(fid,dest)
            return sha256(dest)==sha256(source)
    with ThreadPoolExecutor(max_workers=4) as pool:assert all(pool.map(work,range(4)))

def test_fragmentation_and_coalescing():
    left,right=socket.socketpair();raw=encode(REQUEST,{'op':'HELLO','request_id':'a'})+encode(REQUEST,{'op':'LIST','request_id':'b'})
    def send():
        for byte in raw:left.sendall(bytes([byte]))
        left.close()
    thread=threading.Thread(target=send);thread.start()
    assert receive(right)[1]['op']=='HELLO'
    assert receive(right)[1]['op']=='LIST'
    thread.join();right.close()

def test_oversized_frame():
    left,right=socket.socketpair();left.sendall(HEADER.pack(MAGIC,VERSION,REQUEST,MAX_META+1,0))
    with pytest.raises(ProtocolError):receive(right)
    left.close();right.close()

def test_server_rejects_oversize(environment):
    _,configs,_,_=environment
    with Client(configs['alice']) as client:
        client.sock.sendall(HEADER.pack(MAGIC,VERSION,REQUEST,MAX_META+1,0))
        with pytest.raises((EOFError,ssl.SSLError,ConnectionError)):receive(client.sock)

def test_quota_and_invalid_size(environment):
    _,configs,_,_=environment
    with Client(configs['alice']) as client:
        for size in (-1,True,2**40):
            with pytest.raises(RemoteError,match='BAD_REQUEST'):client.request('UPLOAD_INIT',name='quota.bin',size=size,sha256='0'*64)
        with pytest.raises(RemoteError,match='QUOTA'):client.request('UPLOAD_INIT',name='quota.bin',size=1024**3,sha256='0'*64)

def test_connection_limit(environment):
    _,configs,_,_=environment;clients=[]
    try:
        for _ in range(5):clients.append(Client(configs['alice']).connect())
        with pytest.raises(RemoteError,match='BUSY'):Client(configs['alice']).connect()
    finally:
        for client in clients:client.close()

def test_idempotent_commit(environment,tmp_path):
    _,configs,_,_=environment;path=tmp_path/'idempotent';path.write_bytes(b'finished')
    with Client(configs['alice']) as client:
        fid=client.upload(path)
        assert client.request('COMMIT',transfer_id=fid)[0]['file_id']==fid

def test_heartbeat(environment):
    _,configs,_,_=environment
    with Client(configs['alice']) as client:
        assert client._exchange(PING,{},b'')[0]['ok']
        assert client.request('HELLO')[0]['user']=='alice'

def test_unacknowledged_tail_recovery(environment,tmp_path):
    # Re-open an isolated database, simulating restart after data write but before ACK/DB commit.
    root,configs,_,_=environment
    config=json.loads((root/'server.json').read_text());config['storage']=str(tmp_path/'store')
    service=Service(config)
    user=config['users'][0]
    async def prepare():
        info,_=await service.dispatch(user,'UPLOAD_INIT',{'name':'restart.bin','size':6,'sha256':hashlib.sha256(b'abcdef').hexdigest()},b'')
        await service.dispatch(user,'DATA',{'transfer_id':info['transfer_id'],'offset':0},b'abc')
        return info['transfer_id']
    fid=asyncio.run(prepare());row=service.row(user,fid)
    with open(service.path(row,'.part'),'ab') as f:f.write(b'UNACKNOWLEDGED')
    service.db.close()
    reopened=Service(config)
    assert reopened.row(user,fid)['offset']==3
    assert reopened.path(row,'.part').read_bytes()==b'abc'
    async def finish():
        await reopened.dispatch(user,'DATA',{'transfer_id':fid,'offset':3},b'def')
        result,_=await reopened.dispatch(user,'COMMIT',{'transfer_id':fid},b'')
        return result
    assert asyncio.run(finish())['file_id']==fid
    reopened.db.close()

def test_rename_before_database_commit_recovery(environment,tmp_path):
    root,_,_,_=environment
    config=json.loads((root/'server.json').read_text());config['storage']=str(tmp_path/'store')
    service=Service(config);user=config['users'][0]
    async def prepare():
        info,_=await service.dispatch(user,'UPLOAD_INIT',{'name':'rename.bin','size':3,'sha256':hashlib.sha256(b'abc').hexdigest()},b'')
        await service.dispatch(user,'DATA',{'transfer_id':info['transfer_id'],'offset':0},b'abc')
        return info['transfer_id']
    fid=asyncio.run(prepare());row=service.row(user,fid)
    os.replace(service.path(row,'.part'),service.path(row,'.bin'));service.db.close()
    reopened=Service(config)
    assert reopened.row(user,fid)['state']=='complete'
    reopened.db.close()

def test_aggregate_bucket_rate():
    from secure_transfer.server import Bucket
    import time
    async def work():
        bucket=Bucket(1000,100)
        start=time.monotonic()
        await asyncio.gather(bucket.take(200),bucket.take(200),bucket.take(200))
        return time.monotonic()-start
    elapsed=asyncio.run(work())
    assert .45<=elapsed<2.0

def test_disconnect_after_ack(environment,tmp_path):
    _,configs,_,_=environment
    source=tmp_path/'disconnect.bin';source.write_bytes(os.urandom(2*CHUNK+7))
    client=Client(configs['alice']).connect();interrupted=False
    def cut(done,total):
        nonlocal interrupted
        if done>=CHUNK and not interrupted:
            interrupted=True
            # Drop TCP without QUIT; application intentionally exits with its state file intact.
            client._disconnect()
            raise ConnectionError('simulated cable pull')
    with pytest.raises(ConnectionError):client.upload(source,progress=cut)
    client.close()
    with Client(configs['alice']) as recovered:
        fid=recovered.upload(source)
        target=tmp_path/'result';recovered.download(fid,target)
        assert target.read_bytes()==source.read_bytes()

def test_download_no_overwrite(environment,tmp_path):
    _,configs,_,_=environment
    path=tmp_path/'original';path.write_bytes(b'content')
    target=tmp_path/'existing';target.write_bytes(b'keep this')
    with Client(configs['alice']) as client:
        fid=client.upload(path)
        with pytest.raises(FileExistsError):client.download(fid,target)
    assert target.read_bytes()==b'keep this'
