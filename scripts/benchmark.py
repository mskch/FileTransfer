"""Measure local loopback throughput; configured rates include a one-second burst."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from secure_transfer.client import Client,sha256
from secure_transfer.server import Service

def main():
    root=ROOT/'runtime'/('benchmark-'+uuid.uuid4().hex[:8]);root.mkdir()
    cfg=json.loads((ROOT/'runtime/server.json').read_text());cfg.update(port=0,storage=str(root/'storage'))
    loop=asyncio.new_event_loop();thread=threading.Thread(target=loop.run_forever,daemon=True);thread.start()
    async def start():
        service=Service(cfg);server=await service.start()
        return service,server.sockets[0].getsockname()[1]
    service,port=asyncio.run_coroutine_threadsafe(start(),loop).result()
    source=root/'sample.bin';source.write_bytes(os.urandom(12*1024*1024))
    def work(name):
        config=json.loads((ROOT/f'runtime/{name}.json').read_text());config['port']=port
        with Client(config) as client:
            start=time.monotonic();fid=client.upload(source,root/(name+'-upload.json'));upload=time.monotonic()-start
            start=time.monotonic();client.download(fid,root/(name+'-received.bin'));download=time.monotonic()-start
        return {'user':name,'bytes':source.stat().st_size,'upload_seconds':round(upload,3),'download_seconds':round(download,3),'upload_MiB_s':round(12/upload,3),'download_MiB_s':round(12/download,3),'sha256_equal':sha256(source)==sha256(root/(name+'-received.bin'))}
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(work,['alice','bob']))
        output={'environment':'Linux loopback; Alice and Bob execute concurrently','configured_MiB_s':{'alice':1,'bob':4},'burst':'one second of configured rate, shared across all connections of each user','results':results}
        (ROOT/'docs/benchmark.json').write_text(json.dumps(output,indent=2));print(json.dumps(output,indent=2))
    finally:
        asyncio.run_coroutine_threadsafe(service.stop(),loop).result();loop.call_soon_threadsafe(loop.stop);thread.join();loop.close()
if __name__=='__main__':main()
