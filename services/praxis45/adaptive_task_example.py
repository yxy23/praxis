

from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from adaptive_contract import SCHEMA, sha

def prepare(root, workspace, publish_url=None, python=sys.executable):
    root = Path(root).resolve(); workspace = Path(workspace).resolve()
    if root.exists(): raise ValueError('fresh example root required')
    if publish_url:
        url = urlsplit(publish_url)
        if url.scheme != 'http' or url.hostname not in {'127.0.0.1','localhost','::1'}:
            raise ValueError('synthetic example only authorizes HTTP loopback publication')
    root.mkdir(parents=True); workspace.mkdir(parents=True,exist_ok=True)
    (root/'ADAPTIVE_FIXTURE_ROOT').touch()
    brief = root/'Cedar Studio brief.md'
    brief.write_text('Cedar Studio has six designers. Its proposed review cycle is two days. '
                     'The pilot measures revision time and unresolved comments. All content is fictional.\n')
    request = ('Create a 300-word Cedar Studio briefing, three editable slides using pandoc, '
        'and Python review_minutes(items, minutes_per_item) with normal, zero and negative unittest cases. '
        'Build a self-contained HTML site prominently labeled Synthetic runner demonstration. '
        + ('Publish the fictional page only to the supplied local test endpoint and read it back. ' if publish_url else 'Prepare the site without publication. ')
        + 'Locations and the local endpoint are resolved by the coordinator. No real projects, public uploads, '
        'correspondence, credentials, schedules or installed runtime changes are authorized.')
    authority = root/'fixture-switchboard'; authority.mkdir(); (authority/'request.md').write_text(request)
    event = {'request_id':'req_fixture_'+hashlib.sha256(str(root).encode()).hexdigest()[:12],
        'event_id':'event_fixture_'+hashlib.sha256(str(root).encode()).hexdigest()[:12],
        'user_event_sha256':hashlib.sha256(request.encode()).hexdigest(),
        'context_room_id':'synthetic-adaptive-task-example','context_digest':'synthetic-project-context'}
    store=authority/'state.json';store.write_text(json.dumps({'requests':{event['request_id']:dict(
        event,user_prompt_sha256=event['user_event_sha256'],output_dir=str(authority))}},indent=2)+'\n')
    def node(name, task, outputs, skills=(), checks=()):
        destination=workspace/name;destination.mkdir(exist_ok=True)
        return {'id':name,'kind':'native_task','depends_on':['admit'],'dependency_mode':'all_settled_evidence',
            'activation':{'op':'all_required_inputs_valid','args':[]},'input_bindings':[{'producer':'admit'}],
            'route':{'seat':'Avery'},'task':task,'effects':[],'acceptance':['Actual output and independent verification'],
            'scope':{'workspace':str(workspace),'write_roots':[str(destination)],'skills':list(skills),
                     'tools':['shell','filesystem'],'network':'none','publication':None},
            'outputs':[{'path':str(destination/path),'format':kind} for path,kind in outputs], 'checks':list(checks)}
    nodes=[{'id':'admit','kind':'admission','depends_on':[],'dependency_mode':'all_settled_evidence',
        'activation':{'op':'all_required_inputs_valid','args':[]},'input_bindings':[],'route':None,
        'outputs':[{'name':'admission'}],'acceptance':['Bound synthetic request'],'effects':[]}]
    nodes.append(node('writing','Write the real 300-word Markdown briefing from the bound fictional brief, including Cedar Studio.',
                      [('briefing.md','text')]))
    nodes.append(node('slides','Use pandoc to create a real editable three-slide PowerPoint from Markdown; titles Cedar Studio, A shorter review cycle, What to check next.',
                      [('slides.md','text'),('slides.pptx','pptx')],skills=['convert']))
    nodes[-1]['outputs'][1]['required_members']=['ppt/slides/slide1.xml','ppt/slides/slide2.xml','ppt/slides/slide3.xml']
    nodes.append(node('code','Implement review_minutes(items, minutes_per_item), reject negative inputs, and test normal/zero/negative cases; iterate until passing.',
                      [('review.py','text'),('test_review.py','text')],checks=[{'argv':[python,'-m','unittest','-v','test_review'],
                       'cwd':str(workspace/'code'),'timeout_seconds':30}]))
    site=node('site','Build the fictional self-contained HTML page with the exact label Synthetic runner demonstration. The runner must validate this build before any dependent publication task.',[('index.html','html')])
    site['outputs'][0]['contains']=['Synthetic runner demonstration','Cedar Studio']
    nodes.append(site)
    if publish_url:
        publication=node('publish_site','Copy the retained, independently validated HTML build from the site dependency into the declared output without rebuilding it. Publish that copy once via the declared HTTP PUT and retain its real receipt; read back and hash the actual served body.',[('index.html','html')])
        publication.update(depends_on=['site'],input_bindings=[{'producer':'site'}])
        publication['outputs'][0]['contains']=site['outputs'][0]['contains']
        publication['scope'].update(network='publish',publication={'argv':['curl','--fail-with-body','--request','PUT',
            '--data-binary','@'+str(workspace/'publish_site/index.html'),publish_url],
            'cwd':str(workspace/'publish_site'),'readback_url':publish_url})
        nodes.append(publication)
    nodes.append({'id':'join','kind':'all_input_join','depends_on':[n['id'] for n in nodes[1:] if not publish_url or n['id']!='site'],
        'dependency_mode':'all_settled_evidence','activation':{'op':'all_required_inputs_valid','args':[]},
        'input_bindings':[{'producer':n['id']} for n in nodes[1:] if not publish_url or n['id']!='site'],'route':None,'outputs':[{'name':'verified_outputs'}],
        'acceptance':['All actual outputs validated'],'effects':[]})
    graph={'id':'general_task_example','version':'1','authorizing_event':event,'nodes':nodes,
           'edges':[{'from':dep,'to':n['id']} for n in nodes for dep in n['depends_on']]}
    document={'schema_version':SCHEMA,'executable':False,'execution_authority':'none','graph_definitions':[graph],
              'source_bindings':{'brief':{'path':str(brief),'sha256':sha(brief),'role':'reference'}}}
    policy={'max_nodes':8,'max_edges':12,'max_parallel':4,'max_items':4,'start_ms':120000,
        'activity_ms':900000,'no_progress_ms':300000,'queue_ms':900000,'max_revisions':0}
    policy['justification']={k:'Four bounded synthetic tasks; deterministic local fixture with no automatic retry.' for k in policy}
    assignments={n['id']:{'scope':n['scope'],'authority':{'request_quote':request,
        'reason':'Coordinator scoped fictional format/build/publication fixture; existing host tools only.',
        'context_bindings':{'brief':{'path':str(brief),'sha256':sha(brief)}},
        'publication_authorized':n['scope']['publication'] is not None}} for n in nodes if n['kind']=='native_task'}
    for name,data in [('input-logical.json',document),('input-policy.json',policy),('assignments.json',assignments)]:
        (root/name).write_text(json.dumps(data,indent=2)+'\n')
    runner=str(Path(__file__).with_name('adaptive_general.py'))
    common=['--root',str(root),'--fixture-authority',str(store),'--fixture-authority-sha256',sha(store)]
    return {'status':'prepared_fixture_only','root':str(root),'native_launches':0,
        'authorize_argv':[python,'-I','-B',runner,'authorize',*common,'--logical',str(root/'input-logical.json'),
                          '--graph-id',graph['id'],'--assignment',str(root/'assignments.json')],
        'compile_argv':[python,'-I','-B',runner,'compile',*common,'--logical',str(root/'input-logical.json'),
                        '--policy',str(root/'input-policy.json'),'--graph-id',graph['id']],
        'execute_argv_prefix':[python,'-I','-B',runner,'execute',*common],
        'execute_required_arguments':['--expected-compile-sha256 (hash of compile-receipt.json)',
                                      '--engine-source (installed pinned source)','--engine-closure (installed pinned closure)']}

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture',action='store_true',required=True)
    parser.add_argument('--root',type=Path,required=True);parser.add_argument('--workspace',type=Path,required=True)
    parser.add_argument('--publish-url');parser.add_argument('--python',default=sys.executable)
    args=parser.parse_args(argv)
    print(json.dumps(prepare(args.root,args.workspace,args.publish_url,args.python),indent=2))

if __name__=='__main__':main()
