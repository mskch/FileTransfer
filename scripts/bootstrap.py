"""Create a LOCAL experiment CA, server and two client identities. Never commit keys."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess

def generate(output,port=8443):
    out=Path(output).resolve()
    if out.exists():
        raise SystemExit(f'Refusing to overwrite existing directory: {out}')
    out.mkdir(parents=True,mode=0o700)
    openssl=shutil.which('openssl')
    if not openssl:
        raise SystemExit('OpenSSL is required on PATH')
    def run(*args):
        subprocess.run([openssl,*args],cwd=out,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    run('req','-x509','-newkey','rsa:3072','-sha256','-nodes','-keyout','ca.key','-out','ca.crt','-days','30','-subj','/CN=SFT Experiment CA','-addext','basicConstraints=critical,CA:TRUE','-addext','keyUsage=critical,keyCertSign,cRLSign')
    def issue(name,server=False):
        run('req','-new','-newkey','rsa:3072','-sha256','-nodes','-keyout',name+'.key','-out',name+'.csr','-subj','/CN='+name)
        ext='basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage='+('serverAuth' if server else 'clientAuth')+'\n'
        if server:
            ext+='subjectAltName=DNS:localhost,IP:127.0.0.1\n'
        (out/(name+'.ext')).write_text(ext)
        run('x509','-req','-in',name+'.csr','-CA','ca.crt','-CAkey','ca.key','-CAcreateserial','-out',name+'.crt','-days','30','-sha256','-extfile',name+'.ext')
    issue('server',True)
    for name in ('alice','bob'):
        issue(name)
    def cert(name):return str(out/(name+'.crt'))
    def key(name):return str(out/(name+'.key'))
    users=[]
    for name,rate in [('alice',1024*1024),('bob',4*1024*1024)]:
        der=ssl.PEM_cert_to_DER_cert(Path(cert(name)).read_text())
        users.append({'name':name,'fingerprint':hashlib.sha256(der).hexdigest(),'rate':rate,'quota':1024**3})
        config={'host':'127.0.0.1','port':port,'server_name':'localhost','ca':cert('ca'),'cert':cert(name),'key':key(name)}
        (out/(name+'.json')).write_text(json.dumps(config,indent=2))
    server={'host':'127.0.0.1','port':port,'ca':cert('ca'),'cert':cert('server'),'key':key('server'),'storage':str(out/'storage'),'users':users,'max_connections':32,'connections_per_user':5,'max_file_size':1024**3,'max_files_per_user':1000,'idle_timeout':60,'requests_per_second':100}
    (out/'server.json').write_text(json.dumps(server,indent=2))
    if os.name=='posix':
        for path in out.glob('*.key'):path.chmod(0o600)
    print(f'Created local experiment credentials: {out}')
    return out

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',default='runtime')
    parser.add_argument('--port',type=int,default=8443)
    args=parser.parse_args()
    os.umask(0o077)
    generate(args.output,args.port)
