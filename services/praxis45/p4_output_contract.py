
import json
import sys
from pathlib import Path
import shlex

from native_capabilities import EVIDENCE, CLAIMS, ACCOUNTING
from source_access import TRANSMISSION

PYTHON = sys.executable
ENTRY = Path(__file__).with_name('p4_run.py').resolve()
if (ENTRY.parents[3]/'release.json').is_file():
    PYTHON = str(ENTRY.parents[3]/'venv/bin/python')
FORMATS = (
    "source_date must be exactly 'YYYY-MM-DD' (a valid calendar date) or exactly 'unknown'. "
    "Never append a qualifier, filename, time or explanation to source_date. Preserve uncertainty in "
    "a separate source_date_note field, which the validator tolerates; a filename/HTTP modification "
    "date is not automatically a publication date. accessed_at and every timestamp must be ISO 8601 "
    "with a UTC offset, for example 2026-10-10T23:52:58+00:00 (Z also means UTC). "
    "Never use '2026-10-10 23:52:58 UTC'. Put any explanation in accessed_at_note or another note field. "
    "These formats apply to queries, retrievals, retained_sources, every claim evidence row and nested records. "
)

def example_row(capability):
    provenance = {'source': 'https://example.org/fixture', 'passage': 'Fixture evidence.',
        'locator': 'Fixture heading, sentence 1', 'source_date': 'unknown',
        'source_date_note': 'Illustrative fixture; publication date unavailable.',
        'accessed_at': '2026-10-10T23:52:58+00:00', 'content_sha256': 'a' * 64,
        'basis': 'read_source'}
    row = {'claim_id': 'EXAMPLE', 'claim': 'The fixture says Fixture evidence.', 'evidence': [provenance]}
    if capability == CLAIMS:
        row.update(epistemic_label='supported', operational_status='ok', reason='Exact fixture passage.')
    elif capability != EVIDENCE:
        raise ValueError('unknown Praxis-native capability')
    return row

def instructions(capability, operation_log=None):
    helper=shlex.quote(str(ENTRY.with_name('source_access.py')))
    log=operation_log or '<frozen-operation-log-with-own-Dispatch-basename>'
    operations='<search|fetch|read|control|passage|find|hash>' if capability==EVIDENCE else '<read|control|passage|find|hash>'
    backend=' [--backend mediawiki|crossref]' if capability==EVIDENCE else ''
    acquisition=('Use fetch for public HTTPS originals or read for local originals, each once as a producer, retaining --cache bytes. Both extract full PDF/HTML text into the cache and print only counts and at most a 600-character head. ' if capability==EVIDENCE else 'Use read for frozen retained originals/cache, with PDF/HTML extraction built in; retain --cache bytes. ')
    common=(ACCOUNTING + (TRANSMISSION+' ' if capability==EVIDENCE else '') + FORMATS.replace(ACCOUNTING,'') +
        'Literal valid claim row for this capability (illustration only; never retain it as task evidence): '+json.dumps(example_row(capability),sort_keys=True)+'. '+
        'Exact source interface: '+shlex.quote(PYTHON)+' '+helper+
        ' '+operations+' <source> --source-id <stable-whitespace-free-ID> --dispatch <your-own-Dispatch> --log '+shlex.quote(log)+
        ' --entry <bound-native-entry> [--cache <absolute-task-local-retained-directory/source>] [--start <character-offset> --length <1-to-4000>]'+backend+'. '
        'The frozen native entry supplies the Dispatch and exact log directory; --dispatch and --log must match it before any operation. The frozen log template replaces {dispatch_id} with the Dispatch from your injected preamble. One helper per provider tool call and one helper per code cell, never batched. Use the pinned Python and absolute helper path literally, one foreground invocation per tool call; do not combine source operations with other commands. '
        +acquisition+
        'Use passage by --start/--length or --query after a read/fetch/control/search for locators, find --query for case-insensitive whitespace-normalized matches with offsets (4,000 characters total), and hash for metadata; passage/find share a cap of 60 successful calls per Dispatch and are not reads; never inspect originals or caches with Read/Grep/Glob/Edit, Python, shell, or any other tool. '
        'Every log nonce must appear in at least one output and every output marker must exist in the log; global first-appearance order is not required. Recover a lost/truncated/reordered marker with source_access.py attest --nonce <existing-nonce> --dispatch <own-Dispatch> --log <exact-log> --entry <bound-native-entry> before worker_done; attestation is logged separately and counts as no operation. The verifier checks chain hashes, exact log-derived accounting and original bytes; repeated markers are harmless. '
        'Quote arguments any way the shell accepts. Short identifiers alone remain below the content threshold; identifiers embedded in source text receive no exemption. Native web tools invalidate the receipt by name. '
        'Record per-source returned-character coverage in operation_accounting.returned_characters from the log, and describe it in coverage_boundary: 60 successful inspections times at most 4,000 characters; complete read means machine extraction, not full model inspection. Only a retrieval row naming operation_nonce of a logged failed read/fetch attempt for that source is exempt from quote-was-returned, including tool_failed; operational_status on retained or claim evidence exempts nothing. A search_result_only row names operation_nonce of a successful search, source equal to its returned URL/DOI, and content_sha256 of the cached raw search response; passage is optional, but any passage/snippet must be returned title/snippet text for that result. Search-only evidence cannot support or refute. Keep every requested claim. Evidence rows lacking fetched support use evidence=[] and missing_evidence with the actual reason. Checker labels are supported, contradicted, or unresolved; supported/contradicted require read_source citations, unresolved requires reason and missing_evidence. Include URL, helper-returned title when available, accessed_at and exact returned passage/locator; unreachable sources are failures, never citations.  Receipt passages must be at most 600 characters each and quote only text the helper actually returned; recognized request reflections grant no content-coverage credit after aligned extraction/decoding of every segment/value and joined forms. Public helper request values must be below 200 decoded characters and cumulative Dispatch request text at most 600; this is a measured backstop, not a proved returned-output bound. Unrecognized server expansion or transformation remains a residual. Public-mode local reads require pre-producer hash-bound unchanged files; producer-written files cannot supply coverage. Detection runs on raw output; only logged returned control text and the delivered hash-bound frozen Task spec supply non-research coverage, never claim evidence; unreturned tails supply nothing. Every tool output, including nonce-bearing outputs, is checked after full nested JSON/string escape decoding and whitespace normalization: spans of retained/bound source text of at least 200 characters are counted; only the source offsets of logged heads/passages/finds earn coverage; request-tainted returned intervals earn none; text at unrelated offsets earns none, including adjacent passages, persisted-output slices and receipt/self-check echoes. Uncovered source text is measured and labeled partly unverified or possible request reflection (unverified); ordinary delivery is allowed unless the explicit coverage hard-block switch is ON. '
        'Argument/interpreter errors before an operation are harmless and may be corrected; attempted file/network operations are always logged, including failures. This is post hoc observability, not OS isolation or a mid-turn limiter: uncovered source text below 1000 characters per output, silent reads and consistent log/output forgery remain undetected. '
        'Before worker_done MUST run p4_run.py validate-output --capability '+capability+
        ' --file <required-output-path> --p4-config <frozen-config> --node <own-node> [--claim-input <frozen-claim-input>]; '
        'the bound entry supplies the Dispatch/provider/log. Standalone use additionally requires --dispatch-id <own-Dispatch> --agent <codex-or-claude> --operation-log <frozen-log-template> --dispatch-start <ISO-time>. '
        'Correct receipt errors locally; a forbidden past tool operation cannot be repaired by changing the receipt. Preserve failures and report failed if validation cannot pass. ')
    if capability==EVIDENCE:
        common+='Provider transcript classification is advisory; unclassified tool calls/records receive a visible uncertainty label. Direct network/web access or vault/institutional evidence reads outside the helper still fail. Generated store/load cells, literal or generated receipt writes (printf, heredoc, Python or Write), occurrence-local inspection, helper --help, self-check and Orca lifecycle calls are control operations. Discovery only through helper search with --backend mediawiki or crossref; use one of the exact acquisition.plan.queries from the frozen entry. A plain fetch of an exact URL from an earlier same-Dispatch helper-log discovery result is allowed; search --fetch-index 0 also works atomically. Fragments must be removed and every other URL byte preserved. Honest Praxis User-Agent, no email/key/cookies/login, no silent fallback; failures consume attempts. '

    else:
        common+='For checker counters pass --stage checker --claim-input <frozen-claim-input> on each helper invocation; stdout maps retained originals/cache to source identities. Source evidence inspection uses read/passage/find/hash through the helper. Control operations include receipt writes, generated store/load cells, occurrence-local inspection and helper caches, self-check, --help and native Orca lifecycle calls including the injected Python subprocess worker_done pattern. Direct network/web access outside the helper and source reads outside occurrence-local controls/caches refuse. Unclassified control calls are recorded and visibly labeled unverified; classification is post hoc observability. Every citation still requires helper-fetched/read bytes with matching hash and returned passage. Retained original reads are counted rereads at most three per source and three times retained sources total; preserve acquisition limits and corpus confinement. '

    return common

def self_check(capability, output, claim_input=None, *, config_path=None, node=None):
    argv = [PYTHON, '-I', str(ENTRY), 'validate-output', '--capability', capability, '--file', str(output)]
    if claim_input:
        argv += ['--claim-input', str(claim_input)]
    if config_path:
        argv += ["--p4-config", str(config_path), "--node", node]
    return shlex.join(argv)

def validate_p4_output(value, config, node):

    import hashlib
    from p4_contract import EVALUATION_LABEL, RULE_ORIGIN_LABEL, load, require
    require(value.get('evaluation_label') == EVALUATION_LABEL, 'precommitted-rule evaluation label missing')
    if config['kind'] == 'correction' or config['nodes'][node].get('consumes'):
        require(value.get('rule_origin_label') == RULE_ORIGIN_LABEL, 'precommitted rule origin label missing')
    if config['kind'] == 'web' and node == 'audit':
        producer = load(config['nodes']['research']['outputs'][0]['path'])
        require([(r['claim_id'], r['claim']) for r in producer['claims']] ==
                [(r['claim_id'], r['claim']) for r in value['claims']], 'web check changed producer claim checklist')
    if config['kind'] != 'correction':
        return
    commission = config['commission']; rule = commission['rule']
    raw = Path(rule['path']).read_bytes()
    require(hashlib.sha256(raw).hexdigest() == rule['sha256'], 'frozen correction rule digest differs')
    if node == 'research':
        record = value.get('rule_record') or {}
        require(isinstance(record, dict), 'correction record incomplete')
        require(record.get('rule_text') == raw.decode('utf-8'), 'correction rule text differs')
        require(record.get('rule_sha256') == rule['sha256'] and isinstance(record.get('reason'), str)
                and record['reason'].strip() and record.get('before_claims') and record.get('after_claims'),
                'correction record incomplete')
        require(record.get('original_producer_links') == commission['original_producer_links'], 'correction producer lineage differs')
    else:
        review = value.get('rule_review') or {}
        require(isinstance(review, dict) and review.get('accepted') is True and review.get('rule_sha256') == rule['sha256']
                and isinstance(review.get('reason'), str) and review['reason'].strip(), 'independent rule review missing or rejected')
