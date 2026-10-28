#!/usr/bin/env python3
"""Verify every release part, then the complete decompressed public chain."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time

def main():
    ap=argparse.ArgumentParser();ap.add_argument('journal_dir',type=Path);args=ap.parse_args()
    root=args.journal_dir;assets=root/'release-assets'
    if assets.is_symlink() or not assets.is_dir():raise RuntimeError('Unsafe release-assets directory')
    m=json.loads((assets/'PARTS-MANIFEST.json').read_text());started=time.time()
    projection=json.loads((root/'projection-manifest.json').read_text())
    if m.get('uncompressed')!=projection.get('public'):raise RuntimeError('Conflicting uncompressed identity metadata')
    if not isinstance(m.get('parts'),list) or not m['parts']:raise RuntimeError('No ordered parts supplied')
    names=set();declared_total=0
    for i,part in enumerate(m['parts']):
        name=part.get('file');p=assets/name if isinstance(name,str) else None
        if type(part.get('order')) is not int or part['order']!=i or name!='ingolstadt-journal.public.jsonl.zst.part-%03d'%i:raise RuntimeError('Invalid part name/order')
        if name in names or p.is_symlink() or not p.is_file() or p.resolve().parent!=assets.resolve():raise RuntimeError('Unsafe or duplicate part path')
        if type(part.get('bytes')) is not int or not 0<part['bytes']<1024**3 or p.stat().st_size!=part['bytes']:raise RuntimeError('Invalid part size')
        if not isinstance(part.get('sha256'),str) or not re.fullmatch('[0-9a-f]{64}',part['sha256']):raise RuntimeError('Invalid part hash')
        names.add(name);declared_total+=part['bytes']
    if declared_total!=m.get('compressed_bytes') or not re.fullmatch('[0-9a-f]{64}',m.get('compressed_stream_sha256','')):raise RuntimeError('Invalid compressed identity metadata')
    decoder=subprocess.Popen(['zstd','--decompress','--stdout'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    verifier=subprocess.Popen([sys.executable,str(Path(__file__).with_name('verify_public_journal.py')),str(root/'projection-manifest.json'),str(root/'redactions.jsonl'),'--output',str(root/'public-journal-verification.json')],stdin=decoder.stdout,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    decoder.stdout.close();whole=hashlib.sha256();total=0
    for i,part in enumerate(m['parts']):
        if part['order']!=i:raise RuntimeError('Part ordering mismatch')
        p=assets/part['file'];h=hashlib.sha256();size=0
        with p.open('rb') as f:
            while block:=f.read(8*1024*1024):
                h.update(block);whole.update(block);size+=len(block);total+=len(block);decoder.stdin.write(block)
        if size!=part['bytes'] or h.hexdigest()!=part['sha256']:raise RuntimeError('Part identity mismatch: '+part['file'])
    decoder.stdin.close();decoder.stdin=None;_,derr=decoder.communicate();vout,verr=verifier.communicate()
    if decoder.returncode or verifier.returncode:raise RuntimeError('Decompression/verification failed: '+derr.decode()+verr.decode())
    if whole.hexdigest()!=m['compressed_stream_sha256'] or total!=m['compressed_bytes']:raise RuntimeError('Compressed stream mismatch')
    result={'verified':True,'parts':len(m['parts']),'compressed_bytes':total,'compressed_stream_sha256':whole.hexdigest(),'decompressed_chain':json.loads(vout),'elapsed_seconds':time.time()-started,'publication_status':'locally prepared and verified; not uploaded'}
    (root/'release-assets-verification.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result),flush=True)

if __name__=='__main__':main()
