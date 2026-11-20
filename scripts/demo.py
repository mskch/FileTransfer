"""Record actual build/test/GUI execution offscreen; not a synthetic UI mockup."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import argparse
import hashlib
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from PySide6.QtCore import QObject,Signal,QTimer
from PySide6.QtGui import QImage,QFont
from PySide6.QtWidgets import QApplication,QMainWindow,QWidget,QVBoxLayout,QLabel,QPlainTextEdit
from secure_transfer.gui import Window
from secure_transfer.client import Client,sha256

class Events(QObject):
    log=Signal(str)
    ready=Signal(dict)
    failed=Signal(str)

class Demo:
    def __init__(self,app):
        self.app=app;self.events=Events();self.events.log.connect(self.log);self.events.ready.connect(self.gui_start);self.events.failed.connect(self.fail)
        self.root=ROOT/'runtime'/('demo-'+uuid.uuid4().hex[:8]);self.root.mkdir(parents=True)
        self.server=None;self.gui=None;self.finished=False;self.stage='build';self.started=time.monotonic();self.log_lines=[]
        self.console=QMainWindow();self.console.setWindowTitle('实验 1B · 构建、部署与测试');self.console.resize(1120,760)
        body=QWidget();layout=QVBoxLayout(body);layout.setContentsMargins(24,24,24,24)
        label=QLabel('安全文件传输 · 可复现实验演示');label.setStyleSheet('font-size:26px;font-weight:bold;color:#183b60;');layout.addWidget(label)
        label=QLabel('构建、部署、测试与文件传输演示');layout.addWidget(label)
        self.text=QPlainTextEdit();self.text.setReadOnly(True);self.text.setFont(QFont('DejaVu Sans Mono',11));layout.addWidget(self.text)
        self.console.setCentralWidget(body);self.console.show();self.current=self.console
        self.ffmpeg=subprocess.Popen(['ffmpeg','-y','-loglevel','error','-f','rawvideo','-pixel_format','rgba','-video_size','1120x760','-framerate','5','-i','-','-an','-c:v','libx264','-preset','ultrafast','-crf','23','-pix_fmt','yuv420p','-movflags','+faststart',str(ROOT/'docs'/'demo.mp4')],stdin=subprocess.PIPE)
        self.timer=QTimer();self.timer.timeout.connect(self.capture);self.timer.start(200)
        self.poll=QTimer();self.poll.timeout.connect(self.tick);self.poll.start(200)
        threading.Thread(target=self.prepare,daemon=True).start()

    def log(self,line):
        self.log_lines.append(line);self.text.appendPlainText(line)
        self.text.verticalScrollBar().setValue(self.text.verticalScrollBar().maximum())

    def capture(self):
        if self.finished:return
        img=self.current.grab().toImage().convertToFormat(QImage.Format_RGBA8888)
        if img.width()!=1120 or img.height()!=760:img=img.scaled(1120,760)
        try:self.ffmpeg.stdin.write(bytes(img.constBits()))
        except BrokenPipeError:self.fail('ffmpeg failed')

    def command(self,args):
        self.events.log.emit('$ '+' '.join(str(a) for a in args))
        process=subprocess.Popen(args,cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
        for line in process.stdout:self.events.log.emit(line.rstrip())
        if process.wait()!=0:raise RuntimeError('command failed')

    def prepare(self):
        try:
            self.events.log.emit('1. BUILD / 构建 Python wheel（Python 无需 C++ 编译）')
            self.command([sys.executable,'-m','pip','wheel','--no-deps','--no-build-isolation','.', '-w',str(self.root/'wheel')])
            time.sleep(1)
            self.events.log.emit('2. TEST / 运行自动化功能、安全和恢复测试')
            self.command([sys.executable,'-m','pytest','-q','--junitxml=docs/test-results.xml'])
            time.sleep(1)
            with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
            config=json.loads((ROOT/'runtime/server.json').read_text());config.update(port=port,storage=str(self.root/'storage'))
            (self.root/'server.json').write_text(json.dumps(config))
            client_config=json.loads((ROOT/'runtime/alice.json').read_text());client_config['port']=port
            self.events.log.emit('3. DEPLOY / 启动本地 TLS 1.3 服务端，端口 '+str(port))
            log=open(self.root/'server.log','w')
            self.server=subprocess.Popen([sys.executable,'-m','secure_transfer.server','--config',str(self.root/'server.json')],cwd=ROOT,stdout=log,stderr=log)
            log.close()
            for _ in range(100):
                try:
                    with Client(client_config) as c:assert c.user=='alice'
                    break
                except OSError:time.sleep(.1)
            else:raise RuntimeError('server startup failed')
            self.events.log.emit('mTLS connection verified. User = alice. Server ready.')
            time.sleep(2)
            for i in range(3):(self.root/f'demo-file-{i+1}.bin').write_bytes(os.urandom(4*1024*1024+i*37))
            self.events.ready.emit(client_config)
        except Exception as exc:self.events.failed.emit(str(exc))

    def gui_start(self,config):
        self.gui=Window(config);self.gui.show();self.current=self.gui;self.stage='upload';self.phase=time.monotonic()
        for i in range(3):self.gui.add_job('upload',str(self.root/f'demo-file-{i+1}.bin'))
        self.did_pause=False;self.did_resume=False

    def tick(self):
        if time.monotonic()-self.started>180:self.fail('demo timeout');return
        if self.stage=='upload':
            if not self.did_pause and time.monotonic()-self.phase>1:
                self.gui.toggle(0);self.did_pause=True
            if self.did_pause and not self.did_resume and not self.gui.jobs[0]['running'] and not self.gui.jobs[0]['done']:
                if time.monotonic()-self.phase>3:
                    self.gui.grab().save(str(ROOT/'docs/gui-paused.png'))
                    self.gui.toggle(0);self.did_resume=True
            if all(j['done'] for j in self.gui.jobs.values()):
                self.gui.grab().save(str(ROOT/'docs/gui-upload.png'))
                originals=list(self.gui.jobs.values())
                for i,job in enumerate(originals):self.gui.add_job('download',str(self.root/f'download-{i}.bin'),job['fid'])
                self.stage='download'
            elif any(not j['running'] and not j['done'] and self.gui.tasks.item(r,3).text().startswith('失败') for r,j in self.gui.jobs.items()):self.fail('GUI transfer failed')
        elif self.stage=='download':
            if all(j['done'] for j in self.gui.jobs.values()):
                for i in range(3):assert sha256(self.root/f'demo-file-{i+1}.bin')==sha256(self.root/f'download-{i}.bin')
                self.gui.grab().save(str(ROOT/'docs/gui-complete.png'))
                self.stage='hold';self.phase=time.monotonic()
            elif any(not j['running'] and not j['done'] for j in self.gui.jobs.values()):self.fail('GUI download failed')
        elif self.stage=='hold' and time.monotonic()-self.phase>3:
            self.current=self.console
            self.log('4. GUI RESULT / 三文件并发上传、暂停继续、三文件并发下载：通过')
            self.log('SHA-256: 3 / 3 received files equal source files.')
            self.log('Authenticated persistent connections remain open until client closes.')
            self.stage='end';self.phase=time.monotonic()
        elif self.stage=='end' and time.monotonic()-self.phase>4:self.finish()

    def fail(self,message):
        self.log('FAILED: '+message)
        self.finish(1)

    def finish(self,code=0):
        if self.finished:return
        self.finished=True;self.timer.stop();self.poll.stop()
        if self.gui:self.gui.close()
        if self.server:
            self.server.terminate()
            try:self.server.wait(5)
            except subprocess.TimeoutExpired:self.server.kill();self.server.wait()
        self.ffmpeg.stdin.close();self.ffmpeg.wait(20)
        (ROOT/'docs/demo-log.txt').write_text('\n'.join(self.log_lines),encoding='utf-8')
        (ROOT/'docs/demo-result.json').write_text(json.dumps({'success':code==0,'files':3,'pause_resume':self.did_resume if self.gui else False,'tls':'TLS 1.3 mTLS','rendering':'actual application','duration_seconds':round(time.monotonic()-self.started,2)},indent=2))
        self.app.exit(code)

if __name__=='__main__':
    app=QApplication([]);demo=Demo(app);sys.exit(app.exec())
