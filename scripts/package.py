"""Build the course ZIP without runtime credentials, virtualenv or user data."""
import argparse
from pathlib import Path
import re
import subprocess
import zipfile

ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser()
parser.add_argument('--name',required=True)
parser.add_argument('--student-id',required=True)
parser.add_argument('--output',default=str(ROOT/'dist'))
args=parser.parse_args()
for value in (args.name,args.student_id):
    if not value or re.search(r'[\\/:*?"<>|\x00-\x1f]',value):raise SystemExit('Invalid name or student ID')
prefix=f'{args.name}_{args.student_id}'
folder=prefix+'_EX1B'
output=Path(args.output);output.mkdir(parents=True,exist_ok=True)
archive=output/(folder+'.zip')
tracked=subprocess.check_output(['git','ls-files','-z'],cwd=ROOT).decode().split('\0')
temporary=archive.with_suffix('.zip.tmp')
with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED) as z:
    def add(name,data):
        if re.search(rb'(?m)^-----BEGIN (?:RSA |EC |ENCRYPTED )?PRIVATE KEY-----\r?$',data):raise RuntimeError('Private key detected')
        z.writestr(folder+'/'+name,data)
    for relative in tracked:
        if not relative:continue
        p=Path(relative)
        if any(x in p.parts for x in ('runtime','.venv','.git','__pycache__','.pytest_cache','dist','build')) or p.suffix=='.key':raise RuntimeError('Forbidden tracked artifact: '+relative)
        if relative in ('docs/report.pdf','docs/report.docx','docs/demo.mp4'):continue
        add(prefix+'_ex1B_src/'+relative,(ROOT/relative).read_bytes())
    add(prefix+'_ex1B_play.mp4',(ROOT/'docs/demo.mp4').read_bytes())
    add(prefix+'_ex1B_report.pdf',(ROOT/'docs/report.pdf').read_bytes())
with zipfile.ZipFile(temporary) as z:
    assert z.testzip() is None
    assert all(not (i.flag_bits & 1) for i in z.infolist())
    file_count=len(z.infolist())
temporary.replace(archive)
print(archive)
print(f'{archive.stat().st_size} bytes; {file_count} files; no private keys or runtime data')
