"""Bounded, network-byte-order frames over a persistent TLS byte stream."""
import json
import struct

MAGIC = b'SFT1'
VERSION = 1
HEADER = struct.Struct('!4sBBII')
MAX_META = 8192
CHUNK = 256 * 1024
REQUEST, RESPONSE, PING, PONG = 1, 2, 3, 4

class ProtocolError(Exception):
    pass

def encode(kind, meta, payload=b''):
    raw = json.dumps(meta, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if kind not in (REQUEST, RESPONSE, PING, PONG) or len(raw) > MAX_META or len(payload) > CHUNK:
        raise ProtocolError('invalid frame size or type')
    return HEADER.pack(MAGIC, VERSION, kind, len(raw), len(payload)) + raw + payload

def unpack_header(raw):
    magic, version, kind, ml, pl = HEADER.unpack(raw)
    if magic != MAGIC or version != VERSION or kind not in (REQUEST, RESPONSE, PING, PONG):
        raise ProtocolError('unsupported frame')
    if not 2 <= ml <= MAX_META or pl > CHUNK:
        raise ProtocolError('frame exceeds limits')
    return kind, ml, pl

def decode_meta(raw):
    try:
        obj = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ProtocolError('invalid JSON') from exc
    if not isinstance(obj, dict):
        raise ProtocolError('metadata must be an object')
    return obj

def read_exact(sock, n):
    out = bytearray()
    while len(out) < n:
        data = sock.recv(n - len(out))
        if not data:
            raise EOFError('connection closed')
        out.extend(data)
    return bytes(out)

def receive(sock):
    kind, ml, pl = unpack_header(read_exact(sock, HEADER.size))
    return kind, decode_meta(read_exact(sock, ml)), read_exact(sock, pl)

async def receive_async(reader):
    kind, ml, pl = unpack_header(await reader.readexactly(HEADER.size))
    meta = decode_meta(await reader.readexactly(ml))
    return kind, meta, await reader.readexactly(pl)
