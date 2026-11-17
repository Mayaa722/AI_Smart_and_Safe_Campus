"""Small checkpointed quiz requests covering the full uploaded lecture."""
import json
from lecture_workflow import PROJECT,read,fingerprint


def build_quiz(w):
    count=w.config['mcq_count']
    existing=w.state['artifacts'].get('lecture_quiz',{})
    if existing.get('parsed') and len(existing['parsed']['questions'])==count:
        from lecture_quality import external_quiz_findings
        if external_quiz_findings(existing):
            w.state['last_processing_error']={'task':'external_quiz_review','warnings':['Quiz source review needs attention: a source-linked external content finding remains unresolved.'],
                'source_ids':list(w.page_notes),'model':w.ns['CFG']['model'],'generation_id':existing['id']}
            w.save();return False
        if w.config.get('quality_policy'):
            from lecture_quality import review_quiz,quiz_editorial_findings,QUIZ_EDITORIAL_POLICY
            if (existing.get('content_review',{}).get('status')=='passed_automated_review' and
                existing['content_review'].get('editorial_policy')==QUIZ_EDITORIAL_POLICY and
                not any(quiz_editorial_findings(q) for q in existing['parsed']['questions'])):return True
            _,passed=review_quiz(w,existing)
            w.save();return passed
        return True
    sections=sorted((s for s in w.state['sections'] if s['status']=='accepted'),key=lambda s:int(s['source_ids'][0].split(':p')[1]))
    midpoint=(len(sections)+1)//2
    groups=[sections[:midpoint],sections[midpoint:] or sections]
    counts=[(count+1)//2,count//2]
    parts=w.state.setdefault('quiz_parts',{})
    questions=[]
    records=[]
    for i,(group,target) in enumerate(zip(groups,counts),1):
        if not target:continue
        ids=list(dict.fromkeys(sid for s in group for sid in s['source_ids']))
        key=fingerprint({'version':'split_quiz_v1','sections':[s['record']['output_hash'] for s in group],
                         'part':i,'target':target})
        record=parts.get(key,{})
        if not record.get('parsed'):
            w.notify(f'Quiz part {i}/{len(groups)} · {target} questions')
            context=json.dumps({'lecture_sections':[{'title':s['record']['parsed']['title'],
                'concepts':s['record']['parsed']['concepts']} for s in group],
                'source_clarifications':[n for n in read(PROJECT/'notebook_review_notes.json',[]) if set(n['source_ids'])&set(ids)],
                'previous_question_stems':[q['stem'] for q in questions]},ensure_ascii=False)
            def validate(parsed):
                previous={' '.join(q['stem'].casefold().split()) for q in questions}
                if any(' '.join(q['stem'].casefold().split()) in previous for q in parsed['questions']):
                    raise ValueError('Quiz structure validation failed: duplicate question from an earlier part.')
            original_count=w.config['mcq_count']
            w.config['mcq_count']=target
            try:
                record=w.request('lecture_quiz',[w.ns['PAGES'][sid] for sid in ids],
                    f'Create exactly {target} MCQs for part {i} of this lecture quiz. Cover distinct actual topics in the supplied source notes. '
                    'Mix recall, application and reasoning. Each question must have exactly four plausible choices and one defensible answer, with a concise explanatory rationale and exact source citations. '
                    'Use parallel grammar and similar option lengths. Wrong choices should confuse real neighboring lecture concepts, not advocate absurdly bad practices. '
                    'Do not put citations or source identifiers inside answer options; put citations only in the citations field and explanation. '
                    'Do not repeat the previous question stems. Label new applications Additional teaching. Difficulty is uncalibrated.',
                    schema=w.ns['MCQ_SCHEMA'],context=context,images=False,validator=validate)
            finally:w.config['mcq_count']=original_count
            parts[key]=record
            w.save()
            if not record.get('parsed'):return False
        questions.extend(record['parsed']['questions'])
        records.append(record)
    parsed={'questions':questions}
    if len(questions)!=count or not all(r['structural_valid'] for r in w.ns['validate_mcqs'](parsed,list(w.ns['PAGES']))):
        raise ValueError('Quiz structure validation failed after combining the parts.')
    w.state['artifacts']['lecture_quiz']={'id':'assembled-quiz-'+fingerprint([r['id'] for r in records])[:20],
        'status':'assembled_from_live_parts','parsed':parsed,'output_hash':w.ns['digest'](parsed),
        'part_ids':[r['id'] for r in records],'models':sorted({r['model'] for r in records}),
        'warnings':['Question quality still requires source review; difficulty labels are uncalibrated.']}
    if w.config.get('quality_policy'):
        from lecture_quality import review_quiz
        _,passed=review_quiz(w,w.state['artifacts']['lecture_quiz'])
        if not passed:
            w.state['last_processing_error']={'task':'quiz_source_review','warnings':['Quiz source review needs attention. The quiz is saved as a draft and cannot be graded yet.'],
                'source_ids':list(w.page_notes),'generation_id':w.state['artifacts']['lecture_quiz']['id'],'model':w.ns['CFG']['model']}
            w.save();return False
    w.state.pop('last_processing_error',None)
    w.save()
    return True
