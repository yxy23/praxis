#!/usr/bin/env python3
"""Verify public bytes/chain, original commitment continuity, and unchanged rows."""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
import sys
import time

def main():
    ap=argparse.ArgumentParser();ap.add_argument('manifest',type=Path);ap.add_argument('redactions',type=Path);ap.add_argument('--journal',type=Path);ap.add_argument('--output',type=Path);args=ap.parse_args()
    m=json.loads(args.manifest.read_text());changes=defaultdict(list);pointers=set();change_count=0
    if type(m.get('records')) is not int or m['records']<1 or m.get('first_seq')!=0 or m.get('last_seq')!=m['records']-1:raise RuntimeError('Invalid record-range metadata')
    for line in args.redactions.read_bytes().splitlines():
        item=json.loads(line);seq=item.get('seq');pointer=item.get('json_pointer')
        if type(seq) is not int or not 0<=seq<m['records']:raise RuntimeError('Redaction sequence outside declared range')
        if not isinstance(pointer,str) or not pointer.startswith('/event/') or re.search(r'~(?![01])',pointer):raise RuntimeError('Invalid redaction JSON pointer')
        if (seq,pointer) in pointers:raise RuntimeError('Duplicate redaction pointer')
        if not isinstance(item.get('public_value'),str):raise RuntimeError('Invalid redaction replacement')
        for key in ('original_utf8_sha256','original_json_sha256'):
            if not isinstance(item.get(key),str) or not re.fullmatch('[0-9a-f]{64}',item[key]):raise RuntimeError('Invalid original-value hash')
        if type(item.get('original_characters')) is not int or item['original_characters']<0:raise RuntimeError('Invalid original-value length')
        pointers.add((seq,pointer));changes[seq].append(item);change_count+=1
    if hashlib.sha256(args.redactions.read_bytes()).hexdigest()!=m['redactions']['sha256']:raise RuntimeError('Redaction map hash mismatch')
    if change_count!=m['redactions'].get('local_path_fields_projected') or len(changes)!=m['redactions'].get('records_changed'):raise RuntimeError('Redaction map count mismatch')
    stream=args.journal.open('rb') if args.journal else sys.stdin.buffer
    started=time.time();head='0'*64;original_head='0'*64;raw=hashlib.sha256();rows=0;size=0;original_recomputed=0
    for line in stream:
        raw.update(line);size+=len(line)
        prefix=b'{"seq":'+str(rows).encode()+b',"prev":"'+head.encode()+b'","event":'
        if not line.startswith(prefix) or line[-76:-67]!=b',"hash":"' or not line.endswith(b'"}\n'):raise RuntimeError('Public format/sequence/previous mismatch at '+str(rows))
        head=line[-67:-3].decode();core=line[:-76]+b'}'
        if hashlib.sha256(core).hexdigest()!=head:raise RuntimeError('Public row hash mismatch at '+str(rows))
        split=core.rfind(b',"source":')
        source=json.loads(core[split+10:-1]);event=core[len(prefix):split]
        if not isinstance(source,dict) or set(source)!={'seq','prev','hash'} or type(source['seq']) is not int:raise RuntimeError('Invalid original commitment schema at '+str(rows))
        if any(not isinstance(source[key],str) or not re.fullmatch('[0-9a-f]{64}',source[key]) for key in ('prev','hash')):raise RuntimeError('Invalid original commitment hash at '+str(rows))
        if source['seq']!=rows or source['prev']!=original_head:raise RuntimeError('Original commitment discontinuity at '+str(rows))
        if rows not in changes:
            original_core=b'{"seq":'+str(rows).encode()+b',"prev":"'+original_head.encode()+b'","event":'+event+b'}'
            if hashlib.sha256(original_core).hexdigest()!=source['hash']:raise RuntimeError('Unchanged original content mismatch at '+str(rows))
            original_recomputed+=1
        else:
            tree={'event':json.loads(event)}
            for item in changes[rows]:
                node=tree
                for key in item['json_pointer'].split('/')[1:]:
                    key=key.replace('~1','/').replace('~0','~')
                    if isinstance(node,list):
                        if not re.fullmatch('0|[1-9][0-9]*',key) or int(key)>=len(node):raise RuntimeError('Noncanonical array pointer')
                        node=node[int(key)]
                    else:node=node[key]
                if node!=item['public_value']:raise RuntimeError('Redaction marker mismatch at '+str(rows))
        original_head=source['hash'];rows+=1
    if rows!=m['records'] or size!=m['public']['bytes'] or raw.hexdigest()!=m['public']['sha256'] or head!=m['public']['head'] or original_head!=m['original']['head']:raise RuntimeError('Public manifest identity mismatch')
    result={'verified':True,'records':rows,'public_bytes':size,'public_sha256':raw.hexdigest(),'public_head':head,'original_commitment_head':original_head,'unchanged_original_rows_recomputed':original_recomputed,'changed_rows_verified_against_redaction_map':len(changes),'elapsed_seconds':time.time()-started,'limits':'Changed original row contents remain withheld; verification is integrity and identity, not scientific validity.'}
    if args.output:args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result),flush=True)

if __name__=='__main__':main()
