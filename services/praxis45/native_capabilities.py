
from datetime import date, datetime
import copy
import hashlib
import json
from pathlib import Path
import re

EVIDENCE = 'praxis_native_evidence_research'
CLAIMS = 'praxis_native_claim_check'
ORIGIN = {'provider': 'praxis-native', 'implementation': 'original implementation'}
UPSTREAM = 'The upstream Amplifier Fact Check and Deep Research tools were not executed.'
BOUNDS = {'queries': 3, 'retrievals': 6, 'retained_sources': 8, 'claims': 6}
                                                             
P0_READ_ONCE = ('Read each source once in full within a producing task, cache that result, and use locators afterward; '
    'do not turn repeated complete downloads into free operations.')
P0_CAPS = 'Absolute ceiling remains 8 retained / 6 complete retrievals / 3 web discovery queries'
ACCOUNTING = (P0_READ_ONCE + ' ' + P0_CAPS + '; six retrieval attempts including failures, three discovery queries, eight retained sources and six claims, or stricter frozen budgets. '
    'The helper is the only source interface. Every search/fetch/read/control/passage/find/hash call appends one hash-chained per-Dispatch log entry and prints one PRAXIS-OP nonce marker. '
    'fetch acquires and completely reads a public HTTPS original; read completely reads a local original/cache with PDF/HTML extraction. Both count as complete reads and attempts. '
    'search counts one discovery attempt, including failure. read/fetch cache the complete extracted text and print a head of at most 600 characters; passage/find use cached text, at most 4,000 characters per call and 60 successful calls per Dispatch; hash is metadata only; neither counts as a complete read. A complete read is full machine extraction into cache, not model inspection. operation_accounting.returned_characters reports per-source unique returned-character coverage derived from log offsets; repeat returns count once. Search parsed output counts as returned text and the raw response is an inspectable pseudo-source.  '
    'operation_accounting is DERIVED from the authoritative log and must equal it exactly, including the stage and per-source map; under-report and over-report both refuse. '
    'Producer stage=producer, source_reads each exactly one; checker stage=checker, checker_source_reads each at most three. '
    'complete_source_reads equals sum(source_reads.values()) for producers or sum(checker_source_reads.values()) for checkers; retrieval_attempts equals complete_source_reads plus the number of failed retrieval rows; web_discovery_queries equals len(queries). All counters are nonnegative integers, never booleans. '
    'Both report complete_source_reads, retrieval_attempts (including failures) and web_discovery_queries. '
    'Checker reads of frozen producer retained originals/cache use retrieval_kind=retained_reread; they are rereads rather than acquisitions. '
    'Total checker rereads are at most three times retained sources; new acquisitions keep all established retrieval/query budgets and frozen corpus confinement. '
    'All successful rows and retained provenance bind helper-log original-byte SHA256. No native web tools or any other source interface are permitted. Control inputs (claim input, rubric, schemas, frozen config and non-research bindings) use control op only: frozen byte-bound non-research files, at most 3 complete control attempts per file per Dispatch. Log nonce and returned-text coverage apply; control_reads records attempts separately. Controls never count toward source_reads, complete_source_reads, retrieval_attempts or retained sources and never constitute claim evidence. ')
BOUND_BASIS = "post hoc from the worker's own receipts"

def require(condition, message):
    if not condition:
        raise ValueError(message)

def text(value, name):
    require(isinstance(value, str) and bool(value.strip()), name + ' must be nonempty text')

def timestamp(value):
    text(value, 'accessed_at')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(parsed.tzinfo is not None, 'access timestamp needs timezone')

def provenance(row):
    require(isinstance(row, dict), 'provenance must be an object')
    for key in ('source', 'locator', 'source_date', 'accessed_at', 'content_sha256'):
        text(row.get(key), key)
    if row.get('basis')!='search_result_only' or 'passage' in row:
        text(row.get('passage'), 'passage')
        require(len(row['passage'])<=600,'receipt passage exceeds 600 characters')
    if row.get('basis')=='search_result_only' and 'snippet' in row:
        text(row['snippet'],'snippet');require(len(row['snippet'])<=600,'receipt snippet exceeds 600 characters')
    require(Path(row['source']).is_absolute() or row['source'].startswith(('https://', 'http://')) or
            row.get('basis')=='search_result_only' and re.fullmatch(r'10\.[0-9]{4,9}/\S+',row['source']) is not None,
            'source requires absolute path or URL')
    require(re.fullmatch(r'[0-9a-f]{64}', row['content_sha256']) is not None, 'invalid content sha256')
    require(row.get('basis') in {'read_source', 'search_result_only'}, 'invalid read/search basis')
    if row['source_date'] != 'unknown':
        date.fromisoformat(row['source_date'])
    timestamp(row['accessed_at'])
                                                                                 
    if Path(row['source']).is_absolute():
        path = Path(row['source'])
        require(path.is_file() and not path.is_symlink(), 'local source missing or symlink')
        require(hashlib.sha256(path.read_bytes()).hexdigest() == row['content_sha256'], 'local source hash differs')

def unique(rows, key):
    ids = []
    for row in rows:
        require(isinstance(row, dict), key + ' row must be object')
        text(row.get(key), key)
        ids.append(row[key])
    require(len(ids) == len(set(ids)), 'duplicate ' + key)
    return set(ids)

def source_aliases(bindings):

    aliases={}
    for row in bindings:
        for source in (row['path'],row.get('retained_path',row['path'])):
            require(source not in aliases or aliases[source]==row['path'], 'ambiguous frozen source alias')
            aliases[source]=row['path']
    return aliases

def canonical_receipt(value, bindings):

    aliases=source_aliases(bindings)
    result=copy.deepcopy(value)
    rows=[*result.get('retrievals',[]),*result.get('retained_sources',[]),
          *(e for claim in result.get('claims',[]) for e in claim.get('evidence',[]))]
    for row in rows:
        if 'source' in row:row['source']=aliases.get(row['source'],row['source'])
    accounting=result.get('operation_accounting',{})
    for key in ('checker_source_reads','returned_characters'):
        if not isinstance(accounting.get(key),dict):continue
        normalized={}
        for source,count in accounting[key].items():
            source=aliases.get(source,source)
            require(source not in normalized, 'duplicate accounting source alias')
            normalized[source]=count
        accounting[key]=normalized
    return result

def operation_counts(value, bindings=()):
    value=canonical_receipt(value,bindings)
    accounting = value.get('operation_accounting')
    require(isinstance(accounting, dict), 'operation_accounting required')
    producer = value.get('receipt_type') == EVIDENCE
    stage = 'producer' if producer else 'checker'
    if producer:
        for key in ('original_file_read_attempts','complete_retrievals','retrieval_attempts_including_failures'):
            if key in accounting:
                require(type(accounting[key]) is int and 0 <= accounting[key] <= BOUNDS['retrievals'], 'reported operation cap exceeded or invalid: '+key)
    require(accounting.get('stage') == stage, 'stage-specific operation accounting required: ' + stage)
    for key in ('web_discovery_queries', 'complete_source_reads', 'retrieval_attempts'):
        require(type(accounting.get(key)) is int and accounting[key] >= 0, 'actual operation counter required: ' + key)
    if 'control_reads' in accounting:
        controls=accounting['control_reads']
        require(isinstance(controls,dict) and all(isinstance(p,str) and p and type(n) is int and 1<=n<=3 for p,n in controls.items()),'invalid finite control_reads')
    map_key = 'source_reads' if producer else 'checker_source_reads'
    reads = accounting.get(map_key)
    require(isinstance(reads, dict), 'per-source complete-read accounting required: ' + map_key)
    require(all(isinstance(source, str) and source and type(count) is int and count >= 1
                for source, count in reads.items()), 'invalid per-source read count')
    if producer:
        require(all(count == 1 for count in reads.values()), 'read-once requirement violated')
    successful = [r for r in value['retrievals'] if r.get('operational_status') == 'ok']
    failed = len(value['retrievals']) - len(successful)
    require(set(reads) == {r.get('source') for r in successful} and
            sum(reads.values()) == accounting['complete_source_reads'], 'complete-read accounting inconsistent with retrieval records')
    if producer:
        require(accounting['complete_source_reads'] == len(successful), 'producer complete-read rows inconsistent')
    else:
        require(all(reads[source] >= sum(r.get('source') == source for r in successful)
                    for source in reads), 'checker read rows exceed accounting')
        require(all(count <= 3 for count in reads.values()), 'checker per-source read limit exceeded: 3')
        cached_reads={r['source'] for r in successful if r.get('retrieval_kind')=='retained_reread'}
        require(sum(reads[s] for s in cached_reads) <= 3 * len(value['retained_sources']), 'checker total reread limit exceeded: 3 times retained sources')
    require(accounting['retrieval_attempts'] == accounting['complete_source_reads'] + failed,
            'retrieval attempt accounting inconsistent, including failures')
    require(accounting['web_discovery_queries'] == len(value['queries']), 'query accounting inconsistent')
    aliases = {'original_file_read_attempts': 'retrieval_attempts', 'complete_retrievals': 'complete_source_reads',
               'retrieval_attempts_including_failures': 'retrieval_attempts',
               'successful_retrieval_records': None, 'unique_complete_retained_sources': None}
    for key, canonical in aliases.items():
        if key in accounting:
            if producer:
                require(type(accounting[key]) is int and 0 <= accounting[key] <= BOUNDS['retrievals'],
                        'reported operation cap exceeded or invalid: ' + key)
            expected = accounting[canonical] if canonical else (len(successful) if key == 'successful_retrieval_records' else len(reads))
            require(type(accounting[key]) is int and accounting[key] == expected, 'equivalent operation counter inconsistent: ' + key)
    for key in ('retained_sources', 'claims'):
        if key in accounting:
            require(type(accounting[key]) is int and accounting[key] == len(value[key]), 'equivalent count inconsistent: ' + key)
    cached = {r['source'] for r in successful if r.get('retrieval_kind') == 'retained_reread'} if not producer else set()
    require(all(r.get('retrieval_kind','retrieval') in {'retrieval','retained_reread'} for r in value['retrievals']), 'invalid retrieval kind')
    require(not producer or not any(r.get('retrieval_kind') == 'retained_reread' for r in value['retrievals']), 'producer cannot claim checker rereads')
    require(not any(r['source'] in cached and r.get('retrieval_kind') != 'retained_reread' for r in successful), 'mixed retrieval kinds for one source')
    return {'queries': accounting['web_discovery_queries'], 'retrievals': accounting['retrieval_attempts'] - sum(reads[s] for s in cached),
            'retained_sources': len(value['retained_sources']), 'claims': len(value['claims'])}

def validate_operations(value, *, claim_input=None):
    expected = validate_claim_input(claim_input, discovery=value.get('receipt_type') == EVIDENCE) if claim_input else {}
    value=canonical_receipt(value,expected.get('sources',[]))
    counts = operation_counts(value)
    for key, limit in BOUNDS.items():
        require(counts[key] <= limit, 'actual operation bound exceeded: ' + key)
    cached = {r['source'] for r in value['retrievals'] if r.get('retrieval_kind') == 'retained_reread'}
    bindings = {r['path']:r for r in expected.get('sources',[])}
    require(all(s in bindings and bindings[s].get('retained_path') for s in cached), 'checker reread requires frozen producer retained cache')
    if claim_input:
        budget = validate_claim_input(claim_input, discovery=value.get('receipt_type') == EVIDENCE).get('source_budget', {})
        for key, name in [('queries', 'web_discovery_queries_max'), ('retrievals', 'complete_retrievals_max'),
                          ('retained_sources', 'retained_sources')]:
            require(counts[key] <= budget.get(name, BOUNDS[key]), 'frozen input budget exceeded: ' + key)
    return counts

def validate_claim_input(path_or_value, *, discovery=False):

    value = load_json(path_or_value) if isinstance(path_or_value, (str, Path)) else path_or_value
    require(isinstance(value, dict), 'claim input must be object')
    claims = value.get('claims')
    require(isinstance(claims, list) and 1 <= len(claims) <= BOUNDS['claims'], '1 to 6 claim rows required')
    unique(claims, 'claim_id')
    for row in claims:
        text(row.get('claim'), 'claim wording')
    sources = value.get('sources')
    require(isinstance(sources, list) and len(sources) <= BOUNDS['retained_sources'], 'bounded sources list required')
    require(bool(sources) or discovery and value.get('evidence_policy') == 'bounded-public-web' or value.get('evidence_policy') == 'retained-or-unresolved', 'unbound claims refused')
    seen = set()
    for row in sources:
        require(isinstance(row, dict), 'source binding must be object')
        text(row.get('path'), 'source path');text(row.get('sha256'), 'source sha256')
        require(Path(row['path']).is_absolute() or row['path'].startswith(('https://','http://')), 'malformed source identity')
        require(Path(row['path']).name not in {'qualification-expected.json','heldout-expected.json','ada-evaluation-spec.md'}, 'answer-key source binding refused')
        source = Path(row.get('retained_path', row['path']))
        require(source.is_absolute() and source.is_file() and not source.is_symlink(), 'source binding missing or malformed')
        require(re.fullmatch(r'[0-9a-f]{64}', row['sha256']) is not None and
                hashlib.sha256(source.read_bytes()).hexdigest() == row['sha256'], 'source binding hash differs')
        require(row['path'] not in seen, 'duplicate source binding');seen.add(row['path'])
    budget = value.get('source_budget', {})
    require(isinstance(budget, dict), 'source budget must be object')
    for key, cap in [('web_discovery_queries_max', 3), ('complete_retrievals_max', 6), ('retained_sources', 8)]:
        if key in budget:
            require(type(budget[key]) is int and 0 <= budget[key] <= cap, 'invalid source budget: ' + key)
    for row in claims:
        if 'source_paths' in row:
            require(isinstance(row['source_paths'], list) and (bool(row['source_paths']) or value.get('evidence_policy')=='retained-or-unresolved') and
                    set(row['source_paths']) <= seen, 'claim source binding outside input')
    return value

def load_json(path):
    def pairs(items):
        result = {}
        for key, val in items:
            require(key not in result, 'duplicate JSON key: ' + key);result[key] = val
        return result
    return json.loads(Path(path).read_bytes(), object_pairs_hook=pairs,
                      parse_constant=lambda v: (_ for _ in ()).throw(ValueError('invalid JSON constant: ' + v)))

def validate(value, capability, *, claim_input=None):
    require(capability in {EVIDENCE, CLAIMS}, 'unknown Praxis-native capability')
    require(isinstance(value, dict), 'receipt must be object')
    if claim_input:
        value=canonical_receipt(value,validate_claim_input(claim_input,discovery=capability==EVIDENCE)['sources'])
    require(type(value.get('schema_version')) is int and value['schema_version'] == 1, 'receipt schema_version must be 1')
    require(value.get('receipt_type') == capability, 'receipt type differs from capability')
    require(value.get('origin') == ORIGIN, 'explicit original Praxis-native origin required')
    require(value.get('upstream_execution') == UPSTREAM, 'upstream nonexecution statement required')
    require(value.get('bounds_basis') == BOUND_BASIS, 'bounds enforcement basis must be explicit')
    text(value.get('coverage_boundary'), 'coverage_boundary')
    require(isinstance(value.get('missingness'), list) and all(isinstance(x, str) and x.strip()
            for x in value['missingness']), 'explicit missingness list required (empty if none)')
    for key, limit in BOUNDS.items():
        require(isinstance(value.get(key), list), key + ' list required')
        require(len(value[key]) <= limit, key + ' bound exceeded: ' + str(limit))
    counts = validate_operations(value, claim_input=claim_input)
    for query in value['queries']:
        require(isinstance(query, dict), 'query receipt must be object')
        text(query.get('query'), 'query');timestamp(query.get('accessed_at'))
    unique(value['retrievals'], 'retrieval_id')
    for retrieval in value['retrievals']:
        require(retrieval.get('operational_status') in {'ok', 'retrieval_failed', 'tool_failed'}, 'invalid retrieval status')
        text(retrieval.get('source'), 'retrieval source');timestamp(retrieval.get('accessed_at'))
        if retrieval['operational_status'] == 'ok':
            provenance(retrieval)
            require(retrieval['basis'] == 'read_source', 'complete retrieval must read source')
        else:
            text(retrieval.get('error'), 'retrieval error')
    unique(value['retained_sources'], 'source_id')
    retained = set()
    for source in value['retained_sources']:
        provenance(source)
        retained.add((source['source'], source['content_sha256'], source['basis']))
        if source['basis'] == 'read_source':
            require(any(r['operational_status'] == 'ok' and r['source'] == source['source']
                and r.get('content_sha256') == source['content_sha256'] for r in value['retrievals']),
                'retained read source requires complete retrieval receipt')
    ids = unique(value['claims'], 'claim_id')
    if claim_input is not None:
        expected = validate_claim_input(claim_input, discovery=capability == EVIDENCE)
        require(ids == {c['claim_id'] for c in expected['claims']}, 'claims must match frozen input exactly')
        original = {c['claim_id']: c['claim'] for c in expected['claims']}
        require(all(c.get('claim') == original[c['claim_id']] for c in value['claims']), 'claim wording changed')
        budget = expected.get('source_budget', {})
        for key, name in [('queries', 'web_discovery_queries_max'), ('retrievals', 'complete_retrievals_max'),
                          ('retained_sources', 'retained_sources')]:
            require(counts[key] <= budget.get(name, BOUNDS[key]), 'frozen input budget exceeded: ' + key)
        allowed = {(s['path'], s['sha256']) for s in expected['sources']}
        require(expected.get('evidence_policy') == 'bounded-public-web' and capability == EVIDENCE or
                all(r['source'] in {s['path'] for s in expected['sources']} for r in value['retrievals']),
                'retrieval outside frozen corpus')
        require(expected.get('evidence_policy') == 'bounded-public-web' and capability == EVIDENCE or
                all((s['source'], s['content_sha256']) in allowed for s in value['retained_sources']),
                'qualification source outside frozen corpus')
    for claim in value['claims']:
        text(claim.get('claim'), 'claim')
        require(isinstance(claim.get('evidence'), list), 'claim evidence list required')
        if claim_input and capability == CLAIMS:
            requested = next(c for c in expected['claims'] if c['claim_id'] == claim['claim_id'])
            if requested.get('source_paths'):
                require(all(e.get('source') in requested['source_paths'] for e in claim['evidence']), 'evidence outside claim binding')
        for evidence in claim['evidence']:
            provenance(evidence)
            require((evidence['source'], evidence['content_sha256'], evidence['basis']) in retained,
                    'claim provenance must reference a retained source')
        if claim['evidence'] and not any(e['basis']=='read_source' for e in claim['evidence']):
            require(False,'claim has no retained evidence; fetch a source before checking; search-only claim evidence is refused')
        if capability == EVIDENCE:
            if not claim['evidence']:text(claim.get('missing_evidence'), 'unresolved missing_evidence')
            require('epistemic_label' not in claim, 'evidence inquiry does not interpret claims')
            continue
        status, label = claim.get('operational_status'), claim.get('epistemic_label')
        require(status in {'ok', 'retrieval_failed', 'tool_failed'}, 'invalid operational status')
        require('epistemic_label' in claim and (label in {'supported', 'contradicted', 'unresolved', 'refuted', 'unverifiable', 'opinion'}
                or label is None and status != 'ok'), 'invalid epistemic label; failures are operational')
        text(claim.get('reason'), 'claim reason')
        if status != 'ok':
            text(claim.get('error'), 'claim operational error')
            require(any(r['operational_status'] == status for r in value['retrievals']), 'hidden operational failure')
        if label not in {None,'unresolved','unverifiable'}:
            require(bool(claim['evidence']), 'supported/contradicted verdict requires provenance')
        if label in {'supported', 'contradicted', 'refuted'}:
            require(all(e['basis'] == 'read_source' for e in claim['evidence']), 'search snippets cannot support/refute')
        if label in {'unresolved','unverifiable'}:
            text(claim.get('missing_evidence'), 'unverifiable missing_evidence')
    return {'capability': capability, 'accepted': True, 'counts': counts,
            'bounds_basis': BOUND_BASIS, 'origin': ORIGIN}

def validate_file(path, capability, *, claim_input=None):
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'duplicate JSON key: ' + key)
            result[key] = value
        return result
    def invalid_constant(value):
        raise ValueError('invalid JSON constant: ' + value)
    return validate(json.loads(Path(path).read_text(), object_pairs_hook=object_pairs,
                               parse_constant=invalid_constant), capability, claim_input=claim_input)

def node_template(capability, output, inputs):

    require(capability in {EVIDENCE, CLAIMS}, 'unknown capability')
    seat, route = ('Hermione', 'literature-research') if capability == EVIDENCE else ('Scully', 'verification')
    schema = Path(__file__).parent / 'schemas' / (capability + '.schema.json')
    spec = ('Execute the ORIGINAL Praxis-native capability ' + capability + ' using the helper source interface only. '
        'No stock implementation, API credentials, purchases, external transmission, or scientific decisions. '
        'Read only supplied corpus and task-relevant sources; never open qualification-expected.json or heldout-expected.json. '
        'Return one JSON receipt matching ' + str(schema.resolve()) + '. '
        'Record every query, retrieval attempt (including failures), retained source, and material claim; '
        'Maximum 3 queries, 6 producer retrieval attempts, 6 retrieval record rows, 8 retained sources, 6 claims, or stricter input limits; checker rereads use transparent totals. '
        'Use exact passages, original-byte hashes, locators, source dates or unknown, access timestamps, '
        'and read_source/search_result_only basis; keep missingness and coverage explicit. '
        'Bounds are validated from receipts AND independent dispatch-bound output evidence and the helper log. '
        + UPSTREAM + ' ')
    spec += ('Retrieve and record provenance without interpretation or epistemic labels. ' if capability == EVIDENCE else
        'Audit each frozen claim exactly once, unchanged; epistemic_label supported/contradicted/unresolved '
        'is separate from operational_status ok/retrieval_failed/tool_failed; null label only if failure prevents assessment. '
        'Search snippets cannot support/refute; unverifiable needs missing_evidence; any failed operation needs error. '
        'This schema check does not adjudicate scientific truth. ')
    spec += 'Read input bindings: ' + json.dumps(inputs, sort_keys=True) + '. Write ' + output + '.'
    from p4_output_contract import instructions
    operation_log=str(Path(output).parent/'operations'/'{dispatch_id}.jsonl')
    spec += ' ' + instructions(capability,operation_log)
    return {'operation_log':operation_log,'capability': capability, 'seat': seat, 'task_class': route, 'spec': spec,
        'source_bindings': inputs, 'outputs': [{'path': output, 'required': True, 'validator': capability}],
        'return_contract': {'required_substrings': ['Praxis-native receipt written']}}

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capability', choices=[EVIDENCE, CLAIMS])
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--claim-input', type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(validate_file(args.receipt, args.capability, claim_input=args.claim_input), indent=2))
    except (ValueError, OSError, TypeError, KeyError) as exc:
        print(json.dumps({'accepted': False, 'capability': args.capability, 'reason': str(exc)}))
        raise SystemExit(4)
