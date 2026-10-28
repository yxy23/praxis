#!/usr/bin/env python3
"""Verify the finite scientific supporting archive without extracting files."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import tarfile

def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        while block:=f.read(1024*1024):h.update(block)
    return h.hexdigest()

class CheckedStream:
    """Hash all decoded bytes and retain tar read-ahead past the latest member."""
    def __init__(self,stream):
        self.stream=stream;self.sha=hashlib.sha256();self.bytes=0;self.end=0;self.tail=bytearray()
    def read(self,size=-1):
        block=self.stream.read(size);start=self.bytes;self.sha.update(block);self.bytes+=len(block)
        if self.bytes>self.end:self.tail.extend(block[max(0,self.end-start):])
        if len(self.tail)>1_000_000:raise ValueError('Unexpected excessive tar read-ahead')
        return block
    def mark_member_end(self,end):
        if end<self.end:raise ValueError('Non-monotonic tar member offsets')
        if end>=self.bytes:self.tail.clear()
        else:del self.tail[:end-self.end]
        self.end=end
    def finish(self):
        # tarfile may have read the zero trailer and hidden bytes beyond it in
        # its own buffer. The wrapper retains those bytes before they disappear.
        if any(self.tail):raise ValueError('Nonzero content after final tar member')
        self.tail.clear()
        while block:=self.stream.read(1024*1024):
            self.sha.update(block);self.bytes+=len(block)
            if any(block):raise ValueError('Nonzero trailing archive content')
        if self.bytes-self.end<1024:raise ValueError('Missing complete tar trailer')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('directory',type=Path,nargs='?',default=Path('.'));args=parser.parse_args()
    base=args.directory;manifest=json.loads((base/'ASSET-ARCHIVE-MANIFEST.json').read_text());name=manifest['file']
    if name!='ingolstadt-scientific-assets.tar.zst':raise ValueError('Unexpected asset basename')
    asset=base/'release-assets'/name
    if asset.is_symlink() or not asset.is_file() or asset.parent.is_symlink():raise ValueError('Asset must be a regular local file')
    if asset.stat().st_size!=manifest['bytes'] or digest(asset)!=manifest['sha256']:raise ValueError('Compressed asset identity mismatch')
    observed={};package=None
    process=subprocess.Popen(['zstd','-d','-c',str(asset)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    decoded=CheckedStream(process.stdout)
    try:
        with tarfile.open(fileobj=decoded,mode='r|') as archive:
            for member in archive:
                path=PurePosixPath(member.name)
                if not member.isfile() or not path.parts or member.name=='.' or path.is_absolute() or '..' in path.parts or '\\' in member.name or str(path)!=member.name or member.name in observed:raise ValueError('Unsafe/duplicate archive member')
                decoded.mark_member_end(member.offset_data+((member.size+511)//512)*512)
                if member.uid or member.gid or member.uname or member.gname or member.mtime or member.mode!=0o644 or any(k!='path' for k in member.pax_headers):raise ValueError('Unexpected archive metadata')
                stream=archive.extractfile(member);h=hashlib.sha256();size=0;chunks=[]
                while block:=stream.read(1024*1024):
                    h.update(block);size+=len(block)
                    if member.name=='PACKAGE-MANIFEST.json':
                        if size>100_000_000:raise ValueError('Oversized package manifest')
                        chunks.append(block)
                observed[member.name]={'sha256':h.hexdigest(),'bytes':size}
                if member.name=='PACKAGE-MANIFEST.json':package=json.loads(b''.join(chunks))
        decoded.finish()
        error=process.stderr.read();code=process.wait()
        if code:raise ValueError('Decompression failed: '+error.decode())
        if decoded.bytes!=manifest['uncompressed_tar_bytes'] or decoded.sha.hexdigest()!=manifest['uncompressed_tar_sha256']:raise ValueError('Uncompressed archive identity mismatch')
    except BaseException:
        process.kill();process.wait();raise
    if package is None or len(observed)!=manifest['files']:raise ValueError('Archive member count or manifest missing')
    expected={}
    for item in package['files']:
        if item['path'] in expected:raise ValueError('Duplicate package declaration')
        expected[item['path']]={'sha256':item['sha256'],'bytes':item['bytes']}
    actual=dict(observed);package_identity=actual.pop('PACKAGE-MANIFEST.json')
    if manifest.get('package_manifest_sha256')!=package_identity['sha256'] or actual!=expected:raise ValueError('Package member identity mismatch')
    print(json.dumps({'verified':True,'archive_sha256':manifest['sha256'],'files':len(observed),'normalized_metadata':True}))

if __name__=='__main__':main()
