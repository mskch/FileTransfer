"""Build Chinese HTML/PDF/DOCX report and original protocol figures from actual evidence."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import html
import argparse
import json
import platform
import subprocess
import sys
import xml.etree.ElementTree as ET
from PySide6.QtCore import Qt,QRectF,QUrl,QMarginsF,QSizeF
from PySide6.QtGui import QImage,QPainter,QPen,QColor,QFont,QTextDocument,QPageSize
from PySide6.QtPrintSupport import QPrinter
from PySide6.QtWidgets import QApplication

ROOT=Path(__file__).resolve().parents[1];DOC=ROOT/'docs';DOC.mkdir(exist_ok=True)
parser=argparse.ArgumentParser()
parser.add_argument('--name',required=True)
parser.add_argument('--student-id',required=True)
parser.add_argument('--repo',required=True)
args=parser.parse_args()
app=QApplication([])
FONT='Droid Sans Fallback'

def sequence(filename,title,steps):
    height=155+len(steps)*74
    image=QImage(1000,height,QImage.Format_RGB32);image.fill(QColor('white'))
    p=QPainter(image);p.setRenderHint(QPainter.Antialiasing)
    p.setFont(QFont(FONT,18));p.setPen(QColor('#183b60'));p.drawText(QRectF(20,10,960,40),Qt.AlignCenter,title)
    xs=[130,500,870]
    for x,label in zip(xs,['客户端','服务端','文件 / 数据库']):
        p.fillRect(QRectF(x-90,60,180,40),QColor('#e7eff8'));p.setPen(QColor('#183b60'));p.setFont(QFont(FONT,14));p.drawText(QRectF(x-90,60,180,40),Qt.AlignCenter,label)
        p.setPen(QPen(QColor('#a7b7c7'),2,Qt.DashLine));p.drawLine(x,105,x,height-20)
    for i,(src,dst,label) in enumerate(steps):
        y=145+i*74;a,b=xs[src],xs[dst];p.setPen(QPen(QColor('#27648a'),2));p.setFont(QFont(FONT,12))
        if src==dst:
            p.drawLine(a,y,a+65,y);p.drawLine(a+65,y,a+65,y+22);p.drawLine(a+65,y+22,a,y+22)
            p.drawLine(a,y+22,a+9,y+16);p.drawLine(a,y+22,a+9,y+28)
            p.drawText(QRectF(a+10,y-28,320,28),Qt.AlignLeft,label)
        else:
            p.drawLine(a,y,b,y);direction=1 if b>a else -1;p.drawLine(b,y,b-direction*10,y-6);p.drawLine(b,y,b-direction*10,y+6)
            p.drawText(QRectF(min(a,b)+5,y-35,abs(b-a)-10,32),Qt.AlignCenter,label)
    p.end();image.save(str(DOC/filename))

sequences=[
('seq-session.png','会话建立与身份绑定',[(0,1,'TLS ClientHello / ServerHello'),(1,0,'服务端证书：验证 CA 与主机名'),(0,1,'客户端证书：证明私钥持有'),(1,1,'证书指纹映射用户 / 连接配额'),(0,1,'HELLO + request_id'),(1,0,'RESPONSE：用户、速率、块大小')]),
('seq-list.png','文件列表与分页',[(0,1,'LIST(after="")'),(1,2,'参数化查询：owner + 游标，10 条'),(2,1,'当前用户的文件 / 待传任务'),(1,0,'RESPONSE(files, next)'),(0,1,'LIST(after=next)，直到 next 为空')]),
('seq-upload.png','上传、持久确认与提交',[(0,1,'UPLOAD_INIT(name, size, sha256)'),(1,2,'验证配额 / 创建任务与 .part 文件'),(1,0,'RESPONSE(transfer_id, offset=0)'),(0,1,'DATA(transfer_id, offset) + 载荷'),(1,2,'写文件 + fsync + 提交 offset'),(1,0,'RESPONSE(offset)：持久确认'),(0,1,'循环 DATA 后发送 COMMIT'),(1,2,'校验 SHA-256 / 原子改名 / 完成'),(1,0,'RESPONSE(file_id, sha256)')]),
('seq-download.png','下载与本地完整性确认',[(0,1,'DOWNLOAD_INIT(transfer_id)'),(1,0,'RESPONSE(name, size, sha256)'),(0,1,'READ(offset, length)'),(1,2,'仅访问当前用户已完成文件'),(1,0,'RESPONSE(offset, eof) + 载荷'),(0,0,'fsync 本地分块，保存断点'),(0,1,'循环 READ，直到达到 size'),(0,0,'验证 SHA-256，无覆盖原子发布')]),
('seq-resume.png','断线恢复与上传续传',[(0,1,'重建 TLS / HELLO'),(0,1,'RESUME(transfer_id)'),(1,2,'检查 owner，读取已确认 offset'),(1,0,'RESPONSE(size, sha256, offset, state)'),(0,0,'确认源文件及用户 / 服务端一致'),(0,1,'从服务端 offset 继续 DATA'),(0,1,'COMMIT（可幂等重试）')]),
('seq-download-resume.png','下载恢复',[(0,0,'读取本地状态，丢弃未确认尾部'),(0,1,'重建 TLS / HELLO'),(0,1,'DOWNLOAD_INIT(transfer_id)'),(1,0,'返回不可变文件大小与摘要'),(0,0,'比较身份与摘要'),(0,1,'READ(本地持久 offset, length)'),(0,0,'循环写入并最终校验')]),
('seq-heartbeat.png','保活与异常连接释放',[(0,0,'连接设置 SO_KEEPALIVE'),(1,1,'连接设置 SO_KEEPALIVE'),(0,1,'空闲时每 15 秒 PING'),(1,0,'PONG + 匹配 request_id'),(1,1,'完整报文 60 秒超时则断开')]),
('seq-cancel.png','取消未完成任务',[(0,1,'CANCEL(transfer_id)'),(1,2,'检查 owner 与 pending 状态'),(1,2,'删除 .part 与任务记录 / 释放配额'),(1,0,'RESPONSE(ok=true)')]),
('seq-quit.png','正常退出',[(0,0,'等待块确认 / 保留暂停断点'),(0,1,'QUIT'),(1,0,'RESPONSE(ok=true)'),(0,1,'关闭 TLS 与 TCP'),(1,1,'释放连接计数与会话资源')]),
('seq-error.png','业务错误与协议错误',[(0,1,'合法帧但操作越权 / 偏移错误'),(1,0,'RESPONSE(ok=false, code, message)'),(0,1,'同连接可继续合法请求'),(0,1,'超长帧 / 错误版本 / 非法 JSON'),(1,1,'立即关闭连接，限制解析资源')]),
]
for diagram in sequences:sequence(*diagram)

image=QImage(1000,270,QImage.Format_RGB32);image.fill(QColor('white'));p=QPainter(image);p.setRenderHint(QPainter.Antialiasing);p.setFont(QFont(FONT,18));p.setPen(QColor('#183b60'));p.drawText(QRectF(0,10,1000,40),Qt.AlignCenter,'SFT/1 应用帧格式（网络字节序）')
x=20
for label,width in [('Magic\n4 B',200),('Version\n1 B',100),('Type\n1 B',100),('MetaLength\n4 B',280),('PayloadLength\n4 B',280)]:
    p.fillRect(QRectF(x,70,width,75),QColor('#e7eff8'));p.setPen(QPen(QColor('#27648a'),2));p.drawRect(QRectF(x,70,width,75));p.setFont(QFont(FONT,14));p.drawText(QRectF(x,70,width,75),Qt.AlignCenter,label);x+=width
for x,width,label in [(20,480,'JSON 元数据：2–8192 B'),(500,480,'二进制载荷：0–262144 B')]:
    p.fillRect(QRectF(x,160,width,65),QColor('#edf5ef'));p.drawRect(QRectF(x,160,width,65));p.drawText(QRectF(x,160,width,65),Qt.AlignCenter,label)
p.end();image.save(str(DOC/'frame.png'))

bench=json.loads((DOC/'benchmark.json').read_text())
results=ET.parse(DOC/'test-results.xml').getroot();suite=results if results.tag=='testsuite' else results.find('testsuite')
count=suite.attrib['tests'];failures=int(suite.attrib.get('failures',0))+int(suite.attrib.get('errors',0))
demo=json.loads((DOC/'demo-result.json').read_text())

def table(headers,rows):return '<table><tr>'+''.join('<th>'+html.escape(str(x))+'</th>' for x in headers)+'</tr>'+''.join('<tr>'+''.join('<td>'+html.escape(str(x))+'</td>' for x in row)+'</tr>' for row in rows)+'</table>'
def code(s):return '<pre>'+html.escape(s.strip())+'</pre>'
def img(name,width=650):
    picture=QImage(str(DOC/name))
    height=round(width*picture.height()/picture.width())
    return f'<p><img src="{name}" width="{width}" height="{height}" /></p>'
parts=[]
def add(s):parts.append(s)
add(f'''<h1>实验 1B：安全文件传输软件设计与实现</h1>
<p><b>姓名：</b>{html.escape(args.name)}　<b>学号：</b>{html.escape(args.student_id)}</p>
<p><b>Git 仓库地址：</b>{html.escape(args.repo)}</p>
<p><b>日期：</b>2026 年 11 月 21 日　<b>版本：</b>最终提交版</p>
<p>本文记录系统设计、协议实现、安全机制与实际测试结果。</p>
<h2>1. 问题与目标</h2>
<p>文件传输不仅需要把字节发给对端，还需确定对端身份、文件边界、所属用户及最终完整性。网络中断后，客户端已发送的字节数可能大于服务端已持久保存的字节数。若据此前进断点，就会产生缺失或损坏文件。本实验实现 TLS 长连接上的应用协议，并将服务端持久确认偏移作为上传恢复依据。</p>
''')
add(table(['课件要求','对应实现 / 证据'],[
('多用户并发、完整传输（第33页）','asyncio 服务端，GUI 3 个传输线程；SHA-256 + 大小验证'),
('长连接与 keep alive（第36–37页）','连接池、SO_KEEPALIVE、15 秒 PING/PONG；同连接复用测试'),
('应用层协议与时序图（第36–46页）','14 字节前缀 + JSON + 二进制载荷，本文逐模块时序图'),
('安全防护（第33页）','mTLS、证书指纹授权、参数化 SQL、配额、限速、超时'),
('服务端目录隔离（第45页）','不使用客户端文件名拼接路径；非 root 容器、只读根、数据目录唯一持久可写挂载'),
('加分项（第40页）','上传下载断点、多任务 GUI、公钥身份验证、按用户汇总带宽'),
('源码 / 视频 / 报告 / 开源记录（第2–3页）','源代码、真实执行视频、PDF/DOCX、本地 Git；远程地址待补')]))
add('''<h2>2. 架构与技术选择</h2>
<p>服务端采用 Python 标准库 asyncio、ssl、sqlite3；客户端使用 ssl/socket 和 PySide6。控制连接负责列表，最多三个传输工作线程各自复用一个持久 TLS 连接。每个连接只有一个在途请求，以 request_id 匹配响应；并行发生在连接之间。这避免在同一字节流里交错 TLS 读写，又保留多文件同时上传、下载的能力。服务端每个用户默认最多五条连接。</p>
<p>GUI 线程只更新界面，通过 Qt Signal 接收后台结果。线程池处理文件哈希、网络和磁盘；任务完成后不关闭连接，关闭客户端时才发送 QUIT。暂停任务保留本地状态，继续时询问服务端断点。请求失败不会偷偷重放有副作用的操作，界面显示失败并允许用户继续恢复。</p>
<p>相比普通 HTTP 上传，本方案能直接展示应用报文边界和连接生命周期。相比 C++，Python 缩短实现与验证时间。安全传输使用成熟 TLS，不自行发明加密算法；自定义部分是文件传输语义。</p>
<h2>3. 协议与状态机</h2>
<p>协议名称 SFT/1。所有控制和数据帧都在完成双向身份验证的 TLS 1.3 连接内发送。固定前缀使用 struct 格式 !4sBBII，总长 14 字节。</p>''')
add(img('frame.png'))
add(table(['字段','定义与约束'],[('Magic','ASCII SFT1'),('Version','1'),('Type','1 REQUEST；2 RESPONSE；3 PING；4 PONG'),('MetaLength','UTF-8 JSON 字节数，2–8192'),('PayloadLength','二进制载荷字节数，0–262144'),('request_id','每次请求生成 UUID 字符串；响应原样回送'),('op','HELLO / LIST / UPLOAD_INIT / DATA / COMMIT / RESUME / DOWNLOAD_INIT / READ / CANCEL / QUIT'),('transfer_id','服务端生成的 32 位十六进制标识，不能单凭标识获得权限'),('offset / size','非负整数；拒绝 bool、负值、超上限及不符合当前状态的偏移'),('sha256','64 位小写十六进制的完整文件摘要')]))
add('''<p>JSON 元数据负责描述语义，载荷直接传二进制，不使用 Base64。报文读取使用 readexactly / read_exact，先读取固定头，再验证长度，然后读取元数据与载荷。即使 TCP 拆包或粘包，也能恢复完整帧。256 KiB 块的固定头开销约 0.0053%，另有少量 JSON 和 TLS 开销。</p>
<p>上传任务状态：UPLOAD_INIT 创建 pending(0)；合法 DATA 使 pending(offset) 前进；COMMIT 在大小、摘要均正确时变为 complete；CANCEL 删除 pending。已完成文件不可修改，重复 COMMIT 返回同一结果。错误偏移不能隐式覆盖已有数据。暂停只停止客户端发送，不改变服务端状态。</p>
<p>所有业务响应 Type 均为 RESPONSE，成功含 ok=true，失败含 ok=false、code、message。UPLOAD_READY、ACK 等语义由对应请求的 RESPONSE 实现，并非额外的数字 Type。下载最终确认在客户端完成；当前版本没有额外的服务端下载完成审计报文。</p>''')
for filename,title,steps in sequences:
    add('<h3>'+title+'</h3>'+img(filename))
add('''<h2>4. 完整性、断点与正确性</h2>
<p><b>上传持久确认：</b>服务端在任务锁内检查 owner、state、offset 和数据长度，然后写 .part、flush、fsync，提交 SQLite offset，最后返回确认。客户端 ACK 丢失时，RESUME 返回数据库中已确认的偏移，不盲目重复追加。</p>
<p><b>重启恢复：</b>启动时遍历 pending 任务。临时文件比数据库偏移长时，截断未确认尾部；若最终文件已改名但数据库尚未标记完成，则重新校验大小与摘要后补全状态；文件缺失则删除失效任务。对机器突然断电的保证受文件系统、设备缓存与操作系统持久化语义影响，实测范围包括进程强制终止和构造的崩溃窗口。</p>
<p><b>文件提交：</b>所有字节到齐后，在后台线程计算服务端 SHA-256；成功才 os.replace 为不可变 .bin 文件。在 POSIX 系统同步目录后标记完成。传输失败不会把临时文件显示成完整文件。COMMIT 可幂等重试。</p>
<p><b>下载恢复：</b>本地 .sft-download.json 记录文件 ID、服务端/用户作用域、大小、摘要和已 fsync 偏移。恢复时丢弃未确认尾部，核对远端元数据，再继续 READ。最终完整校验后用硬链接原子发布目标文件，避免覆盖已有文件。要求下载目录文件系统支持硬链接；FAT、某些网络盘可能不支持，此时会显式失败并保留断点。</p>
<p><b>不变量：</b>已确认偏移不超过声明大小；每个任务只接受所属用户访问；完成文件必须与声明摘要一致；每条连接只有一组匹配请求/响应；文件结束由 size 和 COMMIT 定义，不依赖 TCP 断开。</p>
<h2>5. 安全机制与边界</h2>''')
add(table(['威胁','措施','验证或边界'],[
('中间人 / 假服务器','仅 TLS 1.3；验证 CA 链和 server_name','错误主机名测试拒绝连接；不允许关闭证书验证'),
('未认证 / 未授权用户','要求客户端证书，并将 DER 证书 SHA-256 指纹映射到配置用户','无客户端证书、不在允许列表中的可信证书被拒绝'),
('横向越权','查询始终包含 owner；传输 ID 不替代授权','Bob 不能读取、恢复、提交或取消 Alice 的任务'),
('SQL 注入','所有客户端数据使用参数绑定；无动态表名','带引号和 SQL 片段文件名正常存取，不能改变查询语义'),
('目录穿越','文件名仅显示；实际路径由已配置用户名和服务端 UUID 构成','拒绝 ../、绝对路径、反斜线、NUL'),
('大报文 / 慢速发送','8 KiB 元数据、256 KiB 数据块上限；整帧超时 60 秒；TLS 握手超时 5 秒','超长帧、只发送部分前缀均释放连接'),
('连接 / 存储耗尽','总连接32、每用户5、文件数1000、单文件1 GiB、用户配额1 GiB','并发配额及存储上限测试；未完成任务也占用配额'),
('带宽滥用','每用户共享令牌桶；上下载合计使用同一预算','Alice 1 MiB/s，Bob 4 MiB/s；多连接不能各自获得独立预算'),
('进程突破后的文件访问','容器非 root、只读根、能力全部移除、禁止提权、只挂载必要凭证与数据','Docker 配置已提供，当前环境无 Docker，未实际启动验证'),
('证书私钥泄露','每次本机生成凭证，POSIX key 0600；运行和打包排除私钥','CA 私钥不挂载到服务容器，不提交 Git/ZIP')]))
add('''<p><b>DDoS 边界：</b>本实现缓解应用层资源消耗，不能抵御任意规模的带宽耗尽攻击。TLS 完成后才进入应用连接计数，因此握手前的分布式洪泛仍需操作系统、防火墙或上游防护。容器文件描述符、内存、CPU 和进程数上限能限制资源占用，但不等于保证攻击下可用性。未实施公网洪泛测试。</p>
<p><b>信任与部署：</b>本地 CA 只用于实验，需通过可信渠道分发 CA 公钥。服务端仅持有自己的私钥，每个用户仅持有自己的证书私钥。初始化脚本为了本机演示把所有凭证生成在 runtime，但真实部署必须分开分发。证书有效期为 30 天；当前无在线 CRL/OCSP，撤销用户需更新允许列表并重启服务以断开既有会话。</p>
<p><b>已知限制：</b>使用 SQLite 与有界同步文件写入，适合课程规模，不宣称高性能存储服务；长时间磁盘阻塞可能影响其他会话。摘要校验没有专门的全局 CPU 调度器。用户目录信任服务端管理员，不防御同权限本地攻击者替换符号链接。未完成任务需显式 CANCEL 清理，完成文件尚无删除接口。磁盘不足会造成当前请求失败并保留待恢复状态。</p>
<h2>6. 实际测试结果</h2>''')
add(f'<p>Python {platform.python_version()}，Linux，PySide6 6.12.0。自动化测试 <b>{count} 项</b>，失败与错误 <b>{failures} 项</b>。完整机器可读结果见 test-results.xml；不包含 Windows 或 Docker 实测。</p>')
add(table(['测试组','实际覆盖'],[
('基础文件传输','0、1、CHUNK−1、CHUNK、CHUNK+1、多块文件；中文和空格文件名'),
('连接与报文','同一 TLS 连接复用、PING/PONG、逐字节输入模拟拆包、多个帧连续读取、超长帧拒绝'),
('断点与恢复','上传/下载暂停继续、ACK 后断线、服务进程 SIGKILL 后重启、未确认尾部回滚、改名与 DB 提交窗口恢复'),
('完整性','摘要错误、提前 COMMIT、错误 offset、完成提交幂等、本地目标拒绝覆盖'),
('认证与权限','错误主机名、无客户端证书、可信但未登记证书、跨用户访问、注入字符串和路径穿越'),
('并发与限额','多用户同时传输、同时上传下载、连接上限、配额、类型校验、慢速帧超时、共享令牌桶')]))
add('<p>性能实验采用 Linux 回环网络，Alice 与 Bob 同时处理各自的 12 MiB 文件，先上传再下载；这是一次实测，不是网络环境通用吞吐保证。令牌桶允许一个配置速率秒的初始突发，因此短上传的平均速率可以高于长期限速。</p>')
add(table(['用户','配置 MiB/s','上传秒','下载秒','上传 MiB/s','下载 MiB/s','哈希一致'],[(r['user'],bench['configured_MiB_s'][r['user']],r['upload_seconds'],r['download_seconds'],r['upload_MiB_s'],r['download_MiB_s'],r['sha256_equal']) for r in bench['results']]))
add(f'<p>真实 GUI 自动演示结果：{demo["files"]} 个文件上传及下载，暂停恢复成功={demo["pause_resume"]}，整体成功={demo["success"]}。视频同时展示 wheel 构建、测试命令、服务启动和 GUI 传输；无配音，由脚本自动操作。详见 demo.mp4、demo-log.txt 和 demo-result.json。</p>')
add(img('gui-complete.png'))
add('''<h2>7. 安装、运行与复现</h2>
<p>前提：Python 3.11 或以上、OpenSSL 命令行工具、能运行 Qt 的桌面环境。服务端与命令行客户端仅使用 Python 标准库。以下命令均在项目根目录执行。</p>
<h3>Linux 首次安装</h3>''')
add(code('''python -m venv .venv
.venv/bin/python -m pip install -r requirements-verified.txt
.venv/bin/python scripts/bootstrap.py --output runtime
# bootstrap refuses to overwrite existing runtime credentials.
.venv/bin/python -m secure_transfer.server --config runtime/server.json
# Another terminal:
.venv/bin/python -m secure_transfer.gui --config runtime/alice.json
# A second user:
.venv/bin/python -m secure_transfer.gui --config runtime/bob.json'''))
add('<p>本工作区已创建 .venv 和 runtime，可直接运行 scripts/run_server.sh 与 scripts/run_gui.sh alice。不要重复 bootstrap 覆盖现有数据。证书路径使用绝对路径，迁移机器后应重新初始化或修改配置。无显示设备的服务器不能直接显示桌面窗口，可在有桌面的客户端运行 GUI；Qt 离屏模式用于自动演示。</p>')
add('<h3>Windows 运行步骤（未实机验证）</h3>')
add(code(r'''py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-verified.txt
# Install OpenSSL and ensure openssl.exe is on PATH first.
.\.venv\Scripts\python.exe scripts/bootstrap.py --output runtime
.\.venv\Scripts\python.exe -m secure_transfer.server --config runtime/server.json
# Another terminal:
.\.venv\Scripts\python.exe -m secure_transfer.gui --config runtime/alice.json'''))
add('<p>默认服务器绑定 127.0.0.1，仅本机访问。若跨机器演示，应将服务绑定到实验网地址，并为真实 IP 或 DNS 重新签发包含对应 SAN 的证书，同时调整客户端 host 和 server_name，不能禁用主机名验证。</p><h3>命令行、测试与构建</h3>')
add(code('''.venv/bin/python -m secure_transfer.client --config runtime/alice.json list
.venv/bin/python -m secure_transfer.client --config runtime/alice.json upload /path/file.bin
.venv/bin/python -m secure_transfer.client --config runtime/alice.json download FILE_ID /path/output.bin
.venv/bin/python -m secure_transfer.client --config runtime/alice.json cancel TRANSFER_ID
.venv/bin/python -m pytest -q --junitxml=docs/test-results.xml
.venv/bin/python -m pip wheel --no-deps --no-build-isolation . -w dist
.venv/bin/python scripts/benchmark.py
QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/demo.py
QT_QPA_PLATFORM=offscreen .venv/bin/python scripts/build_report.py'''))
add('<p>取消任务后，删除对应本地 .sft-upload.json 才能重新开始同一源文件。失败或暂停时，保留源文件及状态文件；不要同时对同一源文件使用相同断点状态启动多个任务。下载状态和 .sft-part 保存在目标文件旁。</p><h3>容器隔离（配置提供，未运行验证）</h3>')
add(code('''# After generating runtime credentials, on a Linux Docker host:
sudo python scripts/prepare_container.py
docker compose up --build -d
# Stop the host server first if it already occupies port 8443.
# Inspect effective permissions and mounts:
docker compose exec server id
docker compose exec server python -c "open('/data/permission-check','w').close()"
# The next command should fail with a read-only filesystem error:
docker compose exec server python -c "open('/app/forbidden','w').close()"
docker compose down'''))
add('<p>容器 UID/GID 为 10001，prepare_container.py 显式设置数据目录 0700、凭证文件 0400 和所属用户。根文件系统只读；仅 /data 持久可写，/tmp 为 16 MiB 临时内存盘。挂载的证书目录不包含 CA 私钥或任何客户端私钥。当前机器无 Docker，因此以上隔离效果仍需在 Docker 主机复核。</p><h2>8. 代码结构与演示结果</h2>')
add(table(['文件','职责'],[('secure_transfer/protocol.py','帧编码、长度边界与精确读取'),('secure_transfer/server.py','TLS 服务、授权、数据库、任务锁、配额与共享限速'),('secure_transfer/client.py','TLS 客户端、心跳、恢复状态与 CLI'),('secure_transfer/gui.py','后台任务、进度条、暂停继续与文件列表'),('scripts/bootstrap.py','生成实验 CA、服务端与用户证书'),('tests/test_transfer.py','功能、安全、崩溃恢复与并发测试'),('scripts/demo.py','实际构建/部署/GUI 演示录制'),('scripts/benchmark.py','实测用户带宽等级'),('scripts/build_report.py','报告与各模块时序图生成'),('scripts/package.py','排除私钥和运行数据，生成课程要求 ZIP')]))
add('''<h2>9. 总结与局限</h2>
<p>本实验实现了带双向身份认证的持久文件传输协议，并将完整性确认、用户隔离、断点恢复和资源上限作为协议的一部分。数据通路已通过自动测试与真实 GUI 演示。下一步工程化方向包括跨平台打包实测、全局磁盘/哈希调度、证书撤销、下载完成审计、过期任务回收和更严格的操作系统隔离验证。</p>
<h2>参考资料</h2>
<p>课程《实验 1 讲解 2026.pdf》，重点第 2–3、33–46、65 页。</p>
<p>Python ssl 官方文档：https://docs.python.org/3/library/ssl.html</p>
<p>OWASP SQL Injection Prevention Cheat Sheet：https://cheatsheetseries.owasp.org/cheatsheets/SQL_Injection_Prevention_Cheat_Sheet.html</p>
<p>OWASP Denial of Service Cheat Sheet：https://cheatsheetseries.owasp.org/cheatsheets/Denial_of_Service_Cheat_Sheet.html</p>''')
css='''body {font-family:"Droid Sans Fallback",sans-serif;font-size:11pt;color:#202f40;line-height:1.45;} h1 {font-size:24pt;color:#183b60;} h2 {font-size:17pt;color:#183b60;page-break-before:always;} h3 {font-size:13pt;color:#27648a;} table {border-collapse:collapse;width:100%;font-size:10pt;} td,th {border:1px solid #b7c7d7;padding:6px;} th {background:#e7eff8;} pre {font-family:monospace;font-size:9pt;background:#f1f4f7;white-space:pre-wrap;} img {max-width:100%;}'''
content='<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>实验 1B 报告</title><style>'+css+'</style></head><body>'+''.join(parts)+'</body></html>'
(DOC/'report.html').write_text(content,encoding='utf-8')
doc=QTextDocument();doc.setBaseUrl(QUrl.fromLocalFile(str(DOC)+'/'))
for picture in DOC.glob('*.png'):
    doc.addResource(QTextDocument.ImageResource,QUrl(picture.name),QImage(str(picture)))
    doc.addResource(QTextDocument.ImageResource,QUrl.fromLocalFile(str(picture)),QImage(str(picture)))
doc.setHtml(content)
printer=QPrinter(QPrinter.HighResolution);printer.setResolution(96);printer.setOutputFormat(QPrinter.PdfFormat);printer.setOutputFileName(str(DOC/'report.pdf'));printer.setPageSize(QPageSize(QPageSize.A4));printer.setPageMargins(QMarginsF(14,14,14,14));doc.setPageSize(QSizeF(printer.pageRect(QPrinter.DevicePixel).size()));doc.setDocumentMargin(0);doc.print_(printer)
subprocess.run(['pandoc',str(DOC/'report.html'),'--from=html','--to=docx','--resource-path='+str(DOC),'-o',str(DOC/'report.docx')],check=True)
print('Created docs/report.html, report.pdf, report.docx and protocol figures.')
