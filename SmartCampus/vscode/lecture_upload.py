"""Single-lecture upload ingestion, isolated from the historical source registry."""
import copy
import hashlib
import json
import math
import re
import subprocess
from pathlib import Path
import fitz
from lecture_workflow import LectureWorkflow, PROJECT, read, write
from slide_rendering import apply_registered_renders

MAX_BYTES = 100 * 1024 * 1024
MAX_PAGES = 120

def note_external_references(workflow):
    """Report source scope without fetching or obeying linked web content."""
    if workflow.doc.get('external_references_checked'):return workflow
    source=Path(workflow.doc['source_path'])
    if source.suffix.lower()!='.pdf':return workflow
    references=[]
    with fitz.open(source) as pdf:
        for number,page in enumerate(pdf,1):
            urls=list(dict.fromkeys(link['uri'] for link in page.get_links() if link.get('uri')))
            if urls:references.append({'page':number,'urls':urls})
    workflow.doc['external_references_checked']=True
    workflow.doc['external_references']=references
    write(Path(workflow.config['document_manifest']),[workflow.doc])
    return workflow


def reuse_verified_page_notes(workflow):
    """Reuse only original-image-matched extraction; never old educational artifacts."""
    if workflow.state['sections']:return
    expected={p['source_id']:p for p in workflow.doc['pages']}
    image_hashes={sid:hashlib.sha256(Path(p['image']).read_bytes()).hexdigest() for sid,p in expected.items()}
    taken=set();origins=[];records=[]
    paths=sorted((PROJECT/'run/full_lectures').glob('*/progress.json'),key=lambda p:p.stat().st_mtime,reverse=True)
    for path in paths:
        if path.parent==workflow.root:continue
        previous=read(path,{})
        if previous.get('source_sha256')!=workflow.doc['sha256']:continue
        for section in previous.get('sections',[]):
            record=section.get('record',{});ids=section.get('source_ids',[])
            if section.get('status')!='accepted' or not record.get('parsed') or record.get('model')!='gemini-3.5-flash-lite':continue
            if not ids or len(ids)>4 or set(ids)-set(expected) or taken.intersection(ids):continue
            manifest={m['source_id']:m.get('sha256') for m in record.get('image_manifest',[]) if m.get('kind')=='full_page'}
            if any(manifest.get(sid)!=image_hashes[sid] for sid in ids):continue
            if {p['source_id'] for p in record['parsed']['pages']}!=set(ids):continue
            if any(set(c['citations'])-set(ids) for c in record['parsed']['concepts']):continue
            reusable=copy.deepcopy(section);reusable['page_processing_reused_from']=path.parent.name
            workflow.state['sections'].append(reusable);taken.update(ids);origins.append(path.parent.name);records.append(record)
    if taken:
        workflow.state['page_processing_cache']={'source_jobs':sorted(set(origins)),'pages':len(taken),
            'policy':'Source SHA, fixed Gemini35 model, every full-page image hash, four-page max and source IDs verified. Prior guides, quizzes, reviews and metrics excluded.'}
        workflow.ns['GENERATIONS'].extend({r['id']:r for r in records}.values())
        workflow.update_notes();workflow.save()

def reuse_trial_materials(workflow):
    """Reuse generated Gemini materials for identical uploaded bytes/evidence.

    Comparison scores, conversations, approvals and ledgers are never copied.
    Content flags remain attached; a cached draft is still a draft.
    """
    if any(r.get('parsed') for r in workflow.state['artifacts'].values()):return
    manifest=read(PROJECT/'run/lecture_comparison_v3/manifest.json',{})
    spec=next((s for s in manifest.get('lectures',{}).values() if s['sha256']==workflow.doc['sha256']),None)
    if not spec:return
    config=read(Path(spec['configs']['gemini']),{})
    if config.get('model')!='gemini-3.5-flash-lite' or config.get('quality_policy')!=workflow.config.get('quality_policy'):return
    root=PROJECT/'run/full_lectures'/config['job_name'];state=read(root/'progress.json',{})
    if state.get('source_sha256')!=workflow.doc['sha256'] or not state.get('coverage',{}).get('processing_complete'):return
    if set(state.get('generation_models_used',[]))-{config['model']}:return
    available={k:v for k,v in state.get('artifacts',{}).items() if v.get('parsed')}
    if not available:return
    guide=state.get('artifacts',{}).get('study_guide',{})
    expected={p['source_id']:p for p in workflow.doc['pages']}
    if guide.get('parsed') and (guide.get('guide_version')!='comprehensive_v2' or set(guide['parsed']['citations'])!=set(expected)):return
    if any(set(record.get('source_ids',[]))-set(expected) for record in available.values()):return
    image_hashes={sid:hashlib.sha256(Path(p['image']).read_bytes()).hexdigest() for sid,p in expected.items()}
    verified=set()
    for section in state.get('sections',[]):
        record=section.get('record',{})
        if section.get('status')!='accepted' or record.get('model')!=config['model']:continue
        for image in record.get('image_manifest',[]):
            if image.get('kind')=='full_page' and image.get('sha256')==image_hashes.get(image['source_id']):verified.add(image['source_id'])
    if verified!=set(expected):return
    if not workflow.coverage()['processing_complete']:reuse_verified_page_notes(workflow)
    if not workflow.coverage()['processing_complete']:return
    for key in ['guide_chapters','guide_overview','guide_source_groups','content_reviews','quiz_reviews','quiz_parts','quiz_item_revisions']:
        if key in state:workflow.state[key]=copy.deepcopy(state[key])
    workflow.state['artifacts']=copy.deepcopy(available)
    workflow.state['materials_reused_from']={'job':root.name,'source_sha256':workflow.doc['sha256'],
        'model':config['model'],'policy':'Every original page-image hash and source ID verified. Raw materials and content flags reused; no scores, history, instructor approval or request ledger copied.'}
    coverage=read(root/'guide_coverage.json')
    if coverage:write(workflow.root/'guide_coverage.json',coverage)
    workflow.save()


def ingest_upload(filename, data, session_id):
    suffix = Path(filename).suffix.lower()
    if suffix not in {'.pdf','.pptx'}:
        raise ValueError('Upload a PDF or PPTX lecture.')
    if not data or len(data)>MAX_BYTES:
        raise ValueError('Upload a nonempty lecture no larger than 100 MB.')
    if not re.fullmatch(r'[a-f0-9]{32}',session_id):
        raise ValueError('Invalid study session.')
    sha = hashlib.sha256(data).hexdigest()
    doc_id = sha[:12]
    # Resume the existing content-identical upload job across browser/server
    # restarts. Keep its checkpoints AND its ledger, rather than starting over.
    candidates=[]
    for prior_path in (PROJECT/'run/uploads').glob('*/'+sha+'/quality_v1/config.json'):
        prior_config=read(prior_path,{})
        if prior_config.get('guide_style')!='comprehensive_v2' or prior_config.get('doc_id')!=doc_id or prior_config.get('quality_policy')!='source_quality_v1':
            continue
        progress=PROJECT/'run/full_lectures'/prior_config['job_name']/'progress.json'
        prior_state=read(progress,{})
        if prior_state.get('source_sha256')==sha:
            candidates.append((progress.stat().st_mtime,prior_path))
    for _,prior_path in sorted(candidates,reverse=True):
        try:
            saved=note_external_references(LectureWorkflow(prior_path))
            reuse_trial_materials(saved)
            return saved
        except (ValueError,FileNotFoundError):continue
    root = PROJECT/'run/uploads'/session_id/sha/'quality_v1'
    root.mkdir(parents=True,exist_ok=True)
    manifest = root/'document.json'
    source = root/('lecture'+suffix)
    if source.exists() and hashlib.sha256(source.read_bytes()).hexdigest()!=sha:
        raise ValueError('Stored upload identity mismatch.')
    if not source.exists():source.write_bytes(data)
    display_name = filename.replace('\\','/').split('/')[-1][:180]
    if not manifest.exists():
        known = next((d for d in read(PROJECT/'run/exports/sources.json',[]) if d['sha256']==sha),None)
        if known:
            document = copy.deepcopy(apply_registered_renders([known],PROJECT)[0])
        else:
            pdf = source
            if suffix=='.pptx':
                pdf = root/'rendered.pdf'
                subprocess.run(['powershell','-NoProfile','-ExecutionPolicy','Bypass','-File',
                    str(PROJECT/'tools/render_uploaded_pptx.ps1'),'-Source',str(source),'-Destination',str(pdf)],
                    check=True,capture_output=True,timeout=180)
            with fitz.open(pdf) as lecture:
                if lecture.needs_pass:raise ValueError('Upload an unlocked copy of this password-protected PDF.')
                if not 1<=len(lecture)<=MAX_PAGES:raise ValueError('Use a lecture containing 1–120 pages/slides.')
                pages=[]
                for i,page in enumerate(lecture,1):
                    image=root/f'page_{i:04}.png'
                    scale = min(110/72,1800/max(page.rect.width,page.rect.height))
                    page.get_pixmap(matrix=fitz.Matrix(scale,scale),alpha=False).save(image)
                    text=page.get_text()
                    pages.append({'page':i,'source_id':f'{doc_id}:p{i}','doc_id':doc_id,
                        'heading':next((line.strip() for line in text.splitlines() if line.strip()),f'Page {i}')[:160],
                        'text':text,'image':str(image),'warnings':[],'assets':[], 'tables':[],
                        'notes':'','blocks':[],'embedded_images':[],'equation_candidates':[],'code_candidates':[]})
            document={'doc_id':doc_id,'sha256':sha,'name':display_name,'type':suffix[1:],
                      'source_path':str(source),'page_count':len(pages),'pages':pages,'warnings':[]}
        if document['page_count']>MAX_PAGES:raise ValueError('Use a lecture containing 1–120 pages/slides.')
        document.update(name=display_name,source_path=str(source))
        write(manifest,[document])
    document=read(manifest)[0]
    if document['sha256']!=sha or document['doc_id']!=doc_id:
        raise ValueError('Stored lecture manifest identity mismatch.')
    config = read(PROJECT/'full_lecture_config.json')
    config.update(doc_id=doc_id,job_name='upload_'+session_id[:12]+'_'+doc_id+'_q1',
                  document_manifest=str(manifest),guide_style='comprehensive_v2',quality_policy='source_quality_v1',
                  model='gemini-3.5-flash-lite',allow_model_fallback=False,pages_per_batch=4,generate_summary=True)
    config['max_requests']=max(config['max_requests'],12*math.ceil(document['page_count']/4)+48)
    # Size the local safety allowance for visual extraction + chapter synthesis
    # + overview + quiz parts and bounded recovery, rather than a fixed 32 calls.
    config['max_requests']=max(config['max_requests'],4*math.ceil(document['page_count']/4)+12)
    config['max_reserved_tokens']=max(config['max_reserved_tokens'],document['page_count']*60000+150000)
    config_path = root/'config.json'
    write(config_path,config)
    workflow = LectureWorkflow(config_path)
    reuse_verified_page_notes(workflow)
    # Reuse only page processing from a source/model-identical research job.
    # Its brief guide, quiz, approval, metric results and ledgers are not copied.
    if not workflow.state['sections']:
        baseline = read(PROJECT/'run/full_lectures/hough_full_v1/progress.json',{})
        if baseline.get('source_sha256')==sha and baseline.get('identity')==workflow.state['identity']:
            workflow.state['sections']=copy.deepcopy(baseline['sections'])
            workflow.state['page_processing_reused_from']='hough_full_v1'
            workflow.ns['GENERATIONS']=[s['record'] for s in workflow.state['sections']]
            workflow.update_notes()
            workflow.save()
    # Reuse completed materials for identical bytes/model/settings, never another
    # session's conversations, ratings, approval or request ledger.
    preview = read(PROJECT/'run/comprehensive_preview.json', {})
    if preview.get('job_root') and not workflow.state['artifacts']:
        previous_root = Path(preview['job_root'])
        previous = read(previous_root/'progress.json', {})
        if previous.get('configuration',{}).get('quality_policy')!=workflow.config.get('quality_policy'):
            reuse_trial_materials(workflow)
            return note_external_references(workflow)
        materials = previous.get('artifacts', {})
        if (previous.get('identity')==workflow.state['identity'] and
            previous.get('source_sha256')==sha and not workflow.state.get('guide_chapters')):
            reusable = {k:copy.deepcopy(v) for k,v in previous.get('guide_chapters',{}).items() if v.get('parsed')}
            if reusable:
                workflow.state['guide_chapters']=reusable
                workflow.state['chapters_reused_from']=previous_root.name
                workflow.ns['GENERATIONS'].extend(reusable.values())
                workflow.save()
        if (previous.get('identity')==workflow.state['identity'] and
            previous.get('source_sha256')==sha and
            materials.get('study_guide',{}).get('guide_version')=='comprehensive_v2' and
            materials.get('study_guide',{}).get('parsed') and materials.get('lecture_quiz',{}).get('parsed')):
            workflow.state['artifacts']=copy.deepcopy(materials)
            workflow.state['guide_chapters']=copy.deepcopy(previous.get('guide_chapters',{}))
            workflow.state['materials_reused_from']=previous_root.name
            coverage=read(previous_root/'guide_coverage.json')
            if coverage:write(workflow.root/'guide_coverage.json',coverage)
            workflow.save()
    reuse_trial_materials(workflow)
    return note_external_references(workflow)
