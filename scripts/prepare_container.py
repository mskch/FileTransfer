"""Prepare least-privilege runtime mounts. Run as host root for UID mapping."""
import json
import os
from pathlib import Path
import shutil

root=Path('runtime').resolve()
config=json.loads((root/'server.json').read_text())
destination=root/'deploy'
if destination.exists():raise SystemExit('runtime/deploy already exists; refusing to replace it')
if os.name!='posix' or os.geteuid()!=0:raise SystemExit('Run on Linux as root to assign the container UID 10001')
(destination/'config').mkdir(parents=True,mode=0o750)
(destination/'storage').mkdir(mode=0o700)
for name in ('ca.crt','server.crt','server.key'):
    shutil.copyfile(root/name,destination/'config'/name)
    (destination/'config'/name).chmod(0o400)
config.update(host='0.0.0.0',storage='/data',ca='/config/ca.crt',cert='/config/server.crt',key='/config/server.key')
(destination/'config'/'server.json').write_text(json.dumps(config,indent=2))
(destination/'config'/'server.json').chmod(0o400)
for path in [destination/'config',destination/'storage',*(destination/'config').iterdir()]:os.chown(path,10001,10001)
print('Prepared isolated mounts. CA private key and client private keys are not mounted.')
