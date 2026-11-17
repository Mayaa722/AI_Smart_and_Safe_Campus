"""Brief comparison plus detailed local evidence, never invents missing scores."""
import argparse
from collections import Counter
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import statistics
import sys
import zipfile
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lecture_workflow import PROJECT,read,write
from tutor_rendering import tutor_text
from lecture_quality import external_content_findings
ROOT=PROJECT/'run/lecture_comparison_v3'

def summarize():
    manifest=read(ROOT/'manifest.json'); rows=[]
    for arm in ['gemini','groq']:
        outcomes=read(ROOT/f'{arm}_results.json',{})
        for name,spec in manifest['lectures'].items():
            config=read(Path(spec['configs'][arm])); job=PROJECT/'run/full_lectures'/config['job_name']
            state=read(job/'progress.json',{}); generations=read(job/'generations.json',[])
            artifacts=state.get('artifacts',{}); cases=outcomes.get(name,{}).get('cases',{})
            failures=Counter()
            for g in generations:
                if g.get('parsed'):continue
                warning=' '.join(g.get('warnings',[]))
                category=next((s for s in ['503','429','400','Input exceeds','Incomplete','ValidationError','ConnectionError','quota'] if s in warning),'other')
                failures[category]+=1
            guide=artifacts.get('study_guide',{}); quiz=artifacts.get('lecture_quiz',{})
            summary=artifacts.get('lecture_summary',{})
            row={'lecture':name,'category':spec['category'],'arm':arm,'model':config['model'],
                'pages':spec['pages'],'pages_processed':state.get('coverage',{}).get('processed_pages',0),
                'guide_chapters':sum(bool(c.get('parsed')) for c in state.get('guide_chapters',{}).values()),
                'summary_generated':bool(summary.get('parsed')),'summary_version':summary.get('summary_version'),
                'guide_generated':bool(guide.get('parsed')),'guide_review':guide.get('content_review',{}).get('status','pending'),
                'guide_external_findings':external_content_findings(guide),
                'quiz_generated':len(quiz.get('parsed',{}).get('questions',[])),
                'quiz_review':quiz.get('content_review',{}).get('status','pending'),
                'quiz_external_findings':external_content_findings(quiz),
                'tutor_cases_with_output':sum(bool(c.get('parsed')) for c in cases.values()),'tutor_cases_planned':8,
                'generation_records':len(generations),'records_with_output':sum(bool(g.get('parsed')) for g in generations),
                'failed_records_by_type':dict(failures),'wall_seconds_observed':outcomes.get(name,{}).get('wall_seconds_total'),
                'scoring_mechanics':outcomes.get(name,{}).get('grading_check'),
                'tutor_modes':{k:{'status':c.get('status'),'presented_text':tutor_text(c),'source_review':c.get('source_review')} for k,c in cases.items()}}
            row['all_features_generated']=all([row['pages_processed']==row['pages'],row['summary_generated'],row['guide_generated'],row['quiz_generated']==8,row['tutor_cases_with_output']==8])
            row['ready_for_unreviewed_practice']=row['all_features_generated'] and row['guide_review']=='passed_automated_review' and row['quiz_review']=='passed_automated_review' and not row['guide_external_findings'] and not row['quiz_external_findings']
            rows.append(row)
    write(ROOT/'comparison_status.json',{'updated_at':datetime.now(timezone.utc).isoformat(),'rows':rows})
    lines=['# Free API lecture comparison - current evidence','',
       'Updated '+datetime.now(timezone.utc).strftime('%d %b %Y %H:%M UTC')+'. Incomplete cells are pending, not zero-quality scores.','',
       '| Lecture | API/model | Pages read | Summary | Full guide | Quiz | Tutor cases |',
       '|---|---|---:|---|---|---:|---:|']
    for r in rows:
        lines.append('| '+r['lecture'].title()+' ('+r['category']+') | '+('Gemini 3.5' if r['arm']=='gemini' else 'Groq Qwen 3.8 27B')+
           ' | '+str(r['pages_processed'])+'/'+str(r['pages'])+' | '+('Saved' if r['summary_generated'] else 'Pending')+
           ' | '+('Saved; source clarification' if r['guide_external_findings'] else 'Saved; '+r['guide_review'] if r['guide_generated'] else str(r['guide_chapters'])+' chapters; partial')+
           ' | '+str(r['quiz_generated'])+'/8'+('; flagged' if r['quiz_external_findings'] else '')+' | '+str(r['tutor_cases_with_output'])+'/8 |')
    source_pages=sum(s['pages'] for s in manifest['lectures'].values())
    gemini_pages=sum(r['pages_processed'] for r in rows if r['arm']=='gemini')
    lines+=['','**Scope and status:** All '+str(len(manifest['lectures']))+' attached lectures ('+str(source_pages)+' pages) are queued. Gemini has processed '+str(gemini_pages)+' pages and generated three short summaries. Both accounts reached daily free quotas. Groq returned real vision notes but has not completed any full lecture workflow. There is no defensible overall model winner yet. No paid tier or local model was used.','',
        '**Findings:** The IoT tutor answered its eight functional cases, including all four modes, visual reasoning, follow-up, unsupported questions and prompt injection. Quiz grading mechanics passed. Assistant source review found 7/8 IoT quiz items academically defensible; one repeats an oversimplified UDP source claim, and another has weak distractors. Raw outputs and automated approvals are preserved; the flagged quiz is gated. Software quiz keys reviewed: 8/8 defensible, but distractors are often too easy. These are development reviews, not instructor ratings.','',
        '**Fixed:** Separate summary tab; truncated-JSON recovery; source checkpoints; visible practice/Socratic questions and matching history; hyperbolic/derivative math rendering. Re-uploading identical tested PDFs reuses verified cached materials and preserves content flags without new generation calls.','',
        '**Metrics and judge:** Four paper-aligned RAGAS families remain. One IoT answer scored faithfulness 1.0, context precision ~1.0 and context recall 1.0; answer relevance was unscored after service failure. This is text-context evidence for one answer. New 12-criterion concept coverage and quiz validity address real gaps; automated coverage scores are still pending. A fixed Gemini judge confirmed the eight IoT tutor modes/cases, but same-family bias remains. LLM judgments support source review; they cannot establish academic truth or learning gains.','',
        '**Recommendation:** Keep Gemini 3.5 as the working baseline, based on completed functionality rather than a proven quality win. Resume only missing work after free quotas refill; finish a matched Groq workflow before deciding. Judge distractor quality and source correctness separately. Do not add more models yet. Linked videos were not ingested. Full evidence and timings are separate; this development run is not a clean speed benchmark.']
    (ROOT/'COMPARISON_BRIEF.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return rows

def package():
    output=PROJECT/'output/SmartCampus_Free_API_Lecture_Evidence.zip'; output.parent.mkdir(exist_ok=True)
    selected={}
    def add(path,arc):
        if path.exists() and path.is_file():selected[arc]=path.read_bytes()
    for path in ROOT.rglob('*'):
        if any(x in path.parts for x in ['smoke_export','ragas_runtime']):continue
        if path.suffix in ['.json','.md'] and path.name!='WORK_STATE.md':add(path,'comparison/'+str(path.relative_to(ROOT)).replace('\\','/'))
    manifest=read(ROOT/'manifest.json')
    for arm in ['gemini','groq']:
        for name,spec in manifest['lectures'].items():
            config=read(Path(spec['configs'][arm])); job=PROJECT/'run/full_lectures'/config['job_name']
            for filename in ['progress.json','generations.json','request_ledger.json','chat_request_ledger.json','content_quality.json','guide_coverage.json','STUDY_GUIDE.md','LECTURE_QUIZ.md','LECTURE_SUMMARY.md','LECTURE_NOTES.md']:
                add(job/filename,f'outputs/{arm}/{name}/{filename}')
            if arm=='gemini':add(Path(spec['file']) if Path(spec['file']).is_absolute() else PROJECT.parents[1]/'lectures for testing'/spec['file'],'sources/'+spec['file'])
    for name in ['lecture_trials.py','judge_lecture_trials.py','evaluate_lecture_trials.py','summarize_lecture_trials.py','lecture_reference_checks.json']:
        add(PROJECT/'comparison'/name,'implementation/comparison/'+name)
    for name in ['lecture_summary.py','tutor_rendering.py','lecture_workflow.py','lecture_quality.py','lecture_quiz.py','tutor_core.py','notebook_engine.py','streamlit_app.py','comprehensive_guide.py','lecture_upload.py','provider_availability.py','gemini_schema.py','math_rendering.py']:
        add(PROJECT/name,'implementation/'+name)
    secrets=[]
    for line in (PROJECT/'.env').read_text(encoding='utf-8-sig').splitlines():
        if '=' in line:
            key,value=line.split('=',1)
            if key.strip().endswith('API_KEY') and value.strip():secrets.append(value.strip().strip('\"').strip("'").encode())
    assert not any(secret in data for data in selected.values() for secret in secrets),'Private key detected; evidence not exported.'
    hashes={name:hashlib.sha256(data).hexdigest() for name,data in selected.items()}
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as z:
        for name,data in selected.items():z.writestr(name,data)
        z.writestr('MANIFEST_SHA256.json',json.dumps(hashes,indent=2))
    with zipfile.ZipFile(output) as z:assert z.testzip() is None
    write(ROOT/'evidence_export.json',{'file':str(output),'files':len(selected),'bytes':output.stat().st_size,
        'sha256':hashlib.sha256(output.read_bytes()).hexdigest(),'private_key_scan':'passed','zip_crc':'passed'})
    print(str(output))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--package',action='store_true');args=p.parse_args();summarize()
    if args.package:package()
