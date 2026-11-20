"""Qt desktop client: network and disk work run outside the UI thread."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import queue
import sys
import threading
import time
from PySide6.QtCore import QObject, Signal, QTimer
from PySide6.QtWidgets import QApplication,QMainWindow,QWidget,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QFileDialog,QTableWidget,QTableWidgetItem,QProgressBar,QMessageBox,QHeaderView,QAbstractItemView
from .client import Client, Paused

class Signals(QObject):
    progress=Signal(int,object,object,float)
    finished=Signal(int,str)
    files=Signal(list)
    status=Signal(str)

class Window(QMainWindow):
    def __init__(self,config):
        super().__init__()
        self.config=config
        self.signals=Signals()
        self.executor=ThreadPoolExecutor(max_workers=3,thread_name_prefix='sft-worker')
        self.pool=queue.Queue()
        for _ in range(3):self.pool.put(Client(config))
        self.control=Client(config)
        self.control_executor=ThreadPoolExecutor(max_workers=1)
        self.jobs={};self.destinations=set();self.sources=set();self.file_rows=[]
        self.shutting_down=False
        self.setWindowTitle('Secure Transfer · 实验 1B');self.resize(1120,760)
        body=QWidget();self.setCentralWidget(body)
        layout=QVBoxLayout(body);layout.setContentsMargins(24,20,24,20);layout.setSpacing(14)
        title=QLabel('安全文件传输');title.setObjectName('title');layout.addWidget(title)
        self.status=QLabel('正在建立双向 TLS 连接…');layout.addWidget(self.status)
        actions=QHBoxLayout()
        self.upload_button=QPushButton('上传文件（可多选）');self.download_button=QPushButton('下载选中文件');self.refresh_button=QPushButton('刷新文件列表')
        for button in (self.upload_button,self.download_button,self.refresh_button):actions.addWidget(button)
        actions.addStretch();layout.addLayout(actions)
        self.files=QTableWidget(0,4);self.files.setHorizontalHeaderLabels(['文件名','大小','状态','文件 ID'])
        self.files.setSelectionBehavior(QAbstractItemView.SelectRows);self.files.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.files.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.files.horizontalHeader().setSectionResizeMode(0,QHeaderView.Stretch);self.files.horizontalHeader().setSectionResizeMode(3,QHeaderView.Stretch)
        layout.addWidget(self.files,3);layout.addWidget(QLabel('传输任务 · 最多 3 个并发任务 · 暂停后可继续'))
        self.tasks=QTableWidget(0,5);self.tasks.setHorizontalHeaderLabels(['任务','进度','速度','状态','操作'])
        self.tasks.horizontalHeader().setSectionResizeMode(0,QHeaderView.Stretch);self.tasks.horizontalHeader().setSectionResizeMode(1,QHeaderView.Stretch)
        self.tasks.setEditTriggers(QAbstractItemView.NoEditTriggers);layout.addWidget(self.tasks,2)
        hint=QLabel('仅在完整性校验通过后显示完成。连接异常时保留断点；点击“继续”重新连接。');hint.setWordWrap(True);layout.addWidget(hint)
        self.upload_button.clicked.connect(self.choose_upload);self.download_button.clicked.connect(self.choose_download);self.refresh_button.clicked.connect(self.refresh)
        self.signals.progress.connect(self.on_progress);self.signals.finished.connect(self.on_finished);self.signals.files.connect(self.on_files);self.signals.status.connect(self.status.setText)
        self.setStyleSheet('QWidget {font-size:14px;} QMainWindow {background:#f4f7fb;} QLabel#title {font-size:28px;font-weight:700;color:#17365a;} QPushButton {padding:9px 14px;border:1px solid #b9c9db;border-radius:5px;background:white;} QPushButton:hover {background:#e5effb;} QTableWidget {background:white;gridline-color:#e4eaf1;} QHeaderView::section {padding:8px;background:#e8eef6;border:0;} QProgressBar {border:1px solid #c5d1de;text-align:center;border-radius:4px;} QProgressBar::chunk {background:#3881bd;}')
        self.refresh()

    def refresh(self):
        if self.shutting_down:return
        def work():
            try:
                self.control.connect()
                self.signals.status.emit(f'已认证：{self.control.user}  |  TLS 1.3 双向证书  |  {self.config["host"]}:{self.config["port"]}')
                self.signals.files.emit(self.control.files())
            except Exception as exc:self.signals.status.emit('连接或操作失败：'+str(exc))
        self.control_executor.submit(work)

    def on_files(self,files):
        self.file_rows=files;self.files.setRowCount(len(files))
        for row,f in enumerate(files):
            for col,text in enumerate((f['name'],self.size(f['size']),'完成' if f['state']=='complete' else '未完成',f['id'])):
                self.files.setItem(row,col,QTableWidgetItem(text))

    @staticmethod
    def size(n):return f'{n/1024/1024:.2f} MiB' if n>=1024*1024 else f'{n/1024:.1f} KiB'

    def choose_upload(self):
        paths,_=QFileDialog.getOpenFileNames(self,'选择上传文件')
        for path in paths:
            resolved=str(Path(path).resolve())
            if resolved not in self.sources:
                self.sources.add(resolved);self.add_job('upload',resolved)

    def choose_download(self):
        selected=sorted({i.row() for i in self.files.selectedIndexes()})
        if not selected:return
        directory=QFileDialog.getExistingDirectory(self,'选择下载目录')
        if not directory:return
        for row in selected:
            f=self.file_rows[row]
            if f['state']!='complete':continue
            path=str(Path(directory)/f['name'])
            if path in self.destinations or Path(path).exists():
                QMessageBox.information(self,'目标已存在',f'请选择其他目录或重命名已有文件：{path}');continue
            self.destinations.add(path);self.add_job('download',path,f['id'])

    def add_job(self,kind,path,fid=None):
        row=self.tasks.rowCount();self.tasks.insertRow(row)
        self.tasks.setItem(row,0,QTableWidgetItem(('上传 ' if kind=='upload' else '下载 ')+Path(path).name))
        bar=QProgressBar();bar.setRange(0,1000);self.tasks.setCellWidget(row,1,bar)
        self.tasks.setItem(row,2,QTableWidgetItem('—'));self.tasks.setItem(row,3,QTableWidgetItem('排队'))
        button=QPushButton('暂停');self.tasks.setCellWidget(row,4,button)
        self.jobs[row]={'kind':kind,'path':path,'fid':fid,'pause':threading.Event(),'running':False,'done':False}
        button.clicked.connect(lambda checked=False,r=row:self.toggle(r));self.start_job(row)
        return row

    def start_job(self,row):
        job=self.jobs[row];job['pause'].clear();job['running']=True
        self.tasks.cellWidget(row,4).setText('暂停');self.tasks.item(row,3).setText('准备 / 计算校验值')
        def work():
            client=self.pool.get();started=time.monotonic();baseline=None
            def progress(done,total):
                nonlocal baseline
                if baseline is None:baseline=done
                self.signals.progress.emit(row,done,total,(done-baseline)/max(time.monotonic()-started,.001))
            try:
                if job['pause'].is_set():raise Paused('paused')
                if job['kind']=='upload':job['fid']=client.upload(job['path'],progress=progress,pause=job['pause'])
                else:client.download(job['fid'],job['path'],progress=progress,pause=job['pause'])
                self.signals.finished.emit(row,'完成')
            except Paused:self.signals.finished.emit(row,'已暂停')
            except Exception as exc:self.signals.finished.emit(row,'失败：'+str(exc))
            finally:self.pool.put(client)
        self.executor.submit(work)

    def toggle(self,row):
        job=self.jobs[row]
        if job['done']:return
        if job['running']:job['pause'].set();self.tasks.item(row,3).setText('正在暂停…')
        else:self.start_job(row)

    def on_progress(self,row,done,total,speed):
        bar=self.tasks.cellWidget(row,1);bar.setValue(int(done*1000/total) if total else 1000)
        bar.setFormat(f'{done*100/total:.1f}%' if total else '100%')
        self.tasks.item(row,2).setText(self.size(speed)+'/s');self.tasks.item(row,3).setText('传输 / 验证')

    def on_finished(self,row,status):
        job=self.jobs[row];job['running']=False;job['done']=status=='完成'
        self.tasks.item(row,3).setText(status);button=self.tasks.cellWidget(row,4)
        button.setText('已完成' if job['done'] else '继续');button.setEnabled(not job['done'])
        if job['done']:self.tasks.cellWidget(row,1).setValue(1000);self.refresh()

    def closeEvent(self,event):
        if not self.shutting_down:
            self.shutting_down=True
            for button in (self.upload_button,self.download_button,self.refresh_button):button.setEnabled(False)
            for job in self.jobs.values():job['pause'].set()
            self.status.setText('正在保存断点并关闭连接…')
        if any(job['running'] for job in self.jobs.values()):
            event.ignore();QTimer.singleShot(300,self.close);return
        self.executor.shutdown(wait=True);self.control_executor.shutdown(wait=True);self.control.close()
        while not self.pool.empty():self.pool.get().close()
        event.accept()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);args=parser.parse_args()
    app=QApplication(sys.argv);window=Window(json.loads(Path(args.config).read_text()));window.show();sys.exit(app.exec())

if __name__=='__main__':main()
