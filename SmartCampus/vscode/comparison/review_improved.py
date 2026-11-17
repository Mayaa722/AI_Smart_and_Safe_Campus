"""Derived, no-network review of actual v2 outputs. Originals are never edited."""
import copy
import json
import statistics
from collections import Counter
from pathlib import Path
from comparison.common import read,write,digest
from lecture_quality import apply_tutor_math_guard,quiz_editorial_findings,quiz_result

ROOT=Path(__file__).resolve().parents[1]/'run/comparison_v2'

def analyze():
    corpus=read(ROOT/'corpus.json');tasks=read(ROOT/'tutor_tasks.json')
    summary={'scope':'Development workflow pilot; same-model source audits are not independent instructor ratings.',
        'original_source_pages':sum(len(x['pages']) for x in corpus),'arms':{},
        'historical_baseline':read(ROOT/'historical_baseline_verification.json',{}),
        'ragas_scope':'New answer metrics judge original extracted text only, while generation saw images. Do not interpret as multimodal faithfulness.'}
    for arm in ['gemini35','qwen']:
        out=ROOT/'outputs'/arm
        if not out.exists():continue
        ledger=read(out/'request_ledger.json',{})
        attempts=[read(p) for p in sorted((out/'attempts').glob('*.json'))]
        records=[read(p) for p in sorted((out/'records').glob('*.json'))]
        result={'model':read(ROOT/'protocol.json')['arms'][arm],
            'attempts_all_development_revisions':ledger.get('attempts',0),
            'attempt_statuses':dict(Counter(x['status'] for x in attempts)),
            'ledger_attempts_without_saved_response_record':ledger.get('attempts',0)-len(attempts),
            'attempt_http_statuses':dict(Counter(str(x['http_status']) for x in attempts if 'http_status' in x)),
            'accepted_record_count_all_stage_ids':sum(bool(x.get('parsed')) for x in records),
            'completion':read(out/'completion.json',{}),'lectures':{},'tutor':[]}
        for lecture in corpus:
            name=lecture['id'];reviews=read(out/(name+'_chapter_reviews.json'),[])
            final_chapters=[];evidence=[]
            for entry in reviews:
                chapter=read(out/'records'/(entry['chapter_id']+'.json'),{})
                if not chapter.get('parsed'):continue
                final_chapters.append(chapter)
                for image in chapter['image_manifest']:
                    original=next(p for p in lecture['pages'] if p['source_id']==image['source_id'])
                    evidence.append({'source_id':image['source_id'],'original_image_matches':image['sha256']==original['image_sha256']})
            quiz=read(out/(name+'_quiz_checked.json'),read(out/(name+'_quiz.json'),{}))
            editorial=[{'item':i,'findings':quiz_editorial_findings(q)} for i,q in enumerate(quiz.get('questions',[]),1)]
            review_items=quiz.get('followup_reviews',[])
            current_key_checks=[]
            for item in review_items:
                audit=(item.get('history') or [item['original']])[-1]['audit']
                if not audit.get('parsed'):continue
                q=item['final'];current_key_checks.append(quiz_result(audit['parsed'],q,q['citations']))
            result['lectures'][name]={'expected_pages':len(lecture['pages']),
                'chapter_count':len(final_chapters),'pages_with_original_image_evidence':len(set(e['source_id'] for e in evidence)),
                'all_image_hashes_match':bool(evidence) and all(e['original_image_matches'] for e in evidence),
                'chapter_review_statuses':dict(Counter(x['result']['status'] for x in reviews)),
                'chapter_flags':[{'chapter':x['chapter_id'],'findings':x['result'].get('flags',[])} for x in reviews if x['result']['status']!='passed_automated_review'],
                'quiz_questions':len(quiz.get('questions',[])),
                'quiz_source_check_statuses':dict(Counter(x['status'] for x in current_key_checks)),
                'quiz_editorial_flags':editorial,'instructor_review':'not_collected'}
        for task in tasks:
            record=read(out/'records'/(task['id']+'.json'),{})
            if not record.get('parsed'):
                result['tutor'].append({'task':task['id'],'status':'missing'});continue
            guarded=copy.deepcopy(record);math_review=apply_tutor_math_guard(guarded)
            write(out/'guarded_display'/(task['id']+'.json'),guarded)
            result['tutor'].append({'task':task['id'],'status':'generated','math_review':math_review,
                'raw_output_hash':digest(record['parsed']),'display_is_guarded':bool(guarded.get('display_answer'))})
        timings=[r['seconds'] for r in attempts if r.get('status')=='completed' and r.get('seconds') is not None]
        result['median_successful_attempt_seconds_all_revisions']=statistics.median(timings) if timings else None
        result['total_attempt_compute_or_wait_seconds']=sum(r.get('seconds',0) or 0 for r in attempts)
        summary['arms'][arm]=result
    ragas=read(ROOT/'ragas_scores.json',{})
    summary['ragas_available']=sum(r.get('value') is not None for r in ragas.get('rows',[]))
    summary['ragas_attempted_rows']=len(ragas.get('rows',[]))
    summary['ragas_planned_answer_values']=24
    summary['ragas_schema_recovery_rows']=[{'arm':r['arm'],'task':r['task_id'],'metric':r['metric'],'status':r['status']}
        for r in ragas.get('rows',[]) if any(t.get('status')=='schema_transport_recovery' for t in r.get('transport_trace',[]))]
    summary['ragas_means_available_only']={}
    for arm in ['gemini35','qwen']:
        for metric in ['faithfulness','answer_relevancy']:
            values=[r['value'] for r in ragas.get('rows',[]) if r['arm']==arm and r['metric']==metric and r.get('value') is not None]
            summary['ragas_means_available_only'][arm+'_'+metric]={'n':len(values),'mean':statistics.mean(values) if values else None}
    write(ROOT/'analysis.json',summary)
    print(json.dumps(summary,indent=2))
    return summary

if __name__=='__main__':analyze()
