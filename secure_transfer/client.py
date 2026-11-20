"""Thread-safe persistent client with heartbeat and resumable transfers."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import ssl
import threading
import uuid
from .protocol import CHUNK, REQUEST, RESPONSE, PING, PONG, ProtocolError, encode, receive

class RemoteError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f'{code}: {message}')

class Paused(Exception):
    pass

def sha256(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for data in iter(lambda:f.read(CHUNK),b''):
            h.update(data)
    return h.hexdigest()

def atomic_json(path, data):
    path=Path(path)
    temp=path.with_suffix(path.suffix+'.tmp')
    with open(temp,'w',encoding='utf-8') as f:
        json.dump(data,f,ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp,path)

class Client:
    def __init__(self,config):
        self.config=config
        self.lock=threading.RLock()
        self.sock=None
        self.closed=threading.Event()
        self.heartbeat=None
        self.user=None

    def connect(self):
        with self.lock:
            if self.sock:
                return self
            if self.closed.is_set():
                raise RuntimeError('client already closed')
            ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.minimum_version=ssl.TLSVersion.TLSv1_3
            ctx.load_verify_locations(self.config['ca'])
            ctx.load_cert_chain(self.config['cert'],self.config['key'])
            raw=socket.create_connection((self.config.get('host','127.0.0.1'),self.config.get('port',8443)),timeout=10)
            raw.setsockopt(socket.SOL_SOCKET,socket.SO_KEEPALIVE,1)
            try:
                self.sock=ctx.wrap_socket(raw,server_hostname=self.config.get('server_name','localhost'))
                self.sock.settimeout(45)
                result,_=self.request('HELLO')
                self.user=result['user']
            except Exception:
                raw.close()
                self._disconnect()
                raise
            if self.heartbeat is None:
                self.heartbeat=threading.Thread(target=self._heartbeat,daemon=True,name='sft-heartbeat')
                self.heartbeat.start()
        return self

    def _disconnect(self):
        if self.sock:
            self.sock.close()
            self.sock=None

    def _heartbeat(self):
        while not self.closed.wait(15):
            # Never interleave TLS reads/writes with a transfer request.
            if self.lock.acquire(blocking=False):
                try:
                    if self.sock:
                        self._exchange(PING,{},b'')
                except Exception:
                    self._disconnect()
                finally:
                    self.lock.release()

    def _exchange(self,kind,meta,data):
        rid=uuid.uuid4().hex
        meta=dict(meta,request_id=rid)
        try:
            self.sock.sendall(encode(kind,meta,data))
            response_kind,result,payload=receive(self.sock)
            if not result.get('ok'):
                raise RemoteError(result.get('code','ERROR'),result.get('message','request failed'))
            if response_kind != (PONG if kind==PING else RESPONSE) or result.get('request_id')!=rid:
                raise ProtocolError('response mismatch')
            return result,payload
        except RemoteError:
            raise
        except Exception:
            self._disconnect()
            raise

    def request(self,op,data=b'',**meta):
        with self.lock:
            if not self.sock:
                self.connect()
            return self._exchange(REQUEST,dict(meta,op=op),data)

    def files(self):
        rows=[]
        after=''
        while True:
            result,_=self.request('LIST',after=after)
            rows.extend(result['files'])
            after=result['next']
            if not after:
                return rows

    def scope(self):
        # Bind resume state to server identity configuration and client certificate.
        return hashlib.sha256(json.dumps({k:self.config.get(k) for k in ('host','port','server_name')},sort_keys=True).encode()+Path(self.config['cert']).read_bytes()+Path(self.config['ca']).read_bytes()).hexdigest()

    def upload(self,path,state_path=None,progress=None,pause=None):
        path=Path(path)
        size=path.stat().st_size
        checksum=sha256(path)
        state_path=Path(state_path or str(path)+'.sft-upload.json')
        identity={'scope':self.scope(),'size':size,'sha256':checksum,'name':path.name}
        old=json.loads(state_path.read_text()) if state_path.exists() else None
        if old:
            if any(old.get(k)!=v for k,v in identity.items()):
                raise ValueError('resume state belongs to another file/server/user; choose a new state file')
            fid=old['transfer_id']
            info,_=self.request('RESUME',transfer_id=fid)
            if info['size']!=size or info['sha256']!=checksum:
                raise ValueError('server resume metadata mismatch')
            offset=info['offset']
        else:
            info,_=self.request('UPLOAD_INIT',name=path.name,size=size,sha256=checksum)
            fid,offset=info['transfer_id'],info['offset']
            atomic_json(state_path,dict(identity,transfer_id=fid))
        with open(path,'rb') as f:
            f.seek(offset)
            if progress:
                progress(offset,size)
            while offset<size:
                if pause and pause.is_set():
                    raise Paused('upload paused; run again to resume')
                data=f.read(min(CHUNK,size-offset))
                if not data:
                    raise ValueError('source file changed during upload')
                result,_=self.request('DATA',data,transfer_id=fid,offset=offset)
                if result['offset']!=offset+len(data):
                    raise ProtocolError('invalid upload acknowledgement')
                offset=result['offset']
                if progress:
                    progress(offset,size)
        result,_=self.request('COMMIT',transfer_id=fid)
        if result['sha256']!=checksum:
            raise ProtocolError('commit checksum mismatch')
        state_path.unlink(missing_ok=True)
        return fid

    def download(self,fid,destination,progress=None,pause=None):
        destination=Path(destination)
        if destination.exists():
            raise FileExistsError('destination already exists; choose another filename')
        part=Path(str(destination)+'.sft-part')
        state=Path(str(destination)+'.sft-download.json')
        info,_=self.request('DOWNLOAD_INIT',transfer_id=fid)
        identity={'scope':self.scope(),'transfer_id':fid,'size':info['size'],'sha256':info['sha256']}
        if state.exists():
            saved=json.loads(state.read_text())
            if any(saved.get(k)!=v for k,v in identity.items()):
                raise ValueError('download resume identity mismatch')
            offset=saved['offset']
            if not part.exists() or part.stat().st_size<offset or not 0<=offset<=info['size']:
                raise ValueError('invalid local partial file')
            with open(part,'r+b') as f:
                f.truncate(offset)
        else:
            with open(part,'xb'):
                pass
            offset=0
            atomic_json(state,dict(identity,offset=offset))
        with open(part,'r+b') as f:
            f.seek(offset)
            if progress:
                progress(offset,info['size'])
            while offset<info['size']:
                if pause and pause.is_set():
                    raise Paused('download paused; run again to resume')
                result,data=self.request('READ',transfer_id=fid,offset=offset,length=CHUNK)
                if result['offset']!=offset or not data or offset+len(data)>info['size']:
                    raise ProtocolError('invalid download chunk')
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
                offset+=len(data)
                atomic_json(state,dict(identity,offset=offset))
                if progress:
                    progress(offset,info['size'])
        if sha256(part)!=info['sha256']:
            # Preserve damaged data for inspection, but never promote it to a complete file.
            raise ValueError('download checksum mismatch; remove partial/state files before retry')
        # Atomic no-clobber publication on a filesystem supporting hard links.
        os.link(part,destination)
        part.unlink()
        state.unlink()
        return str(destination)

    def close(self):
        self.closed.set()
        with self.lock:
            if self.sock:
                try:
                    self._exchange(REQUEST,{'op':'QUIT'},b'')
                except Exception:
                    pass
                self._disconnect()

    def __enter__(self):
        return self.connect()

    def __exit__(self,*args):
        self.close()

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--config',required=True)
    sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('list')
    up=sub.add_parser('upload');up.add_argument('path');up.add_argument('--state')
    down=sub.add_parser('download');down.add_argument('id');down.add_argument('path')
    cancel=sub.add_parser('cancel');cancel.add_argument('id')
    a=p.parse_args()
    config=json.loads(Path(a.config).read_text())
    with Client(config) as c:
        if a.command=='list':
            print(json.dumps(c.files(),ensure_ascii=False,indent=2))
        elif a.command=='upload':
            print(c.upload(a.path,a.state))
        elif a.command=='download':
            print(c.download(a.id,a.path))
        else:
            print(c.request('CANCEL',transfer_id=a.id)[0])

if __name__=='__main__':
    main()
