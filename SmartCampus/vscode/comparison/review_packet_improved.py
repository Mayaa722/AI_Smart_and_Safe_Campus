"""Source-linked v2 review packet with genuinely blank instructor ratings."""
import csv,re
from pathlib import Path
from comparison.common import read,write,digest

ROOT=Path(__file__).resolve().parents[1]/'run/comparison_v2'

def build():
    target=ROOT/'supervisor_review';target.mkdir(exist_ok=True)
    corpus=read(ROOT/'corpus.json');tasks=read(ROOT/'tutor_tasks.json')
    # A fixed label mapping is reproducible, not a claim of successful blinding.
    labels={'B':'gemini35','A':'qwen'};write(ROOT/'review_label_key.json',labels)
    rows=[];keys=[];parts=['# Improved-workflow source review\n\n'
        'Review outputs against the original slides before unmasking models. Ratings below remain blank until a real instructor supplies them. '
        'Labels reduce branding cues but cannot guarantee blinding. These are development outputs, not a held-out test set.\n\n'
        'Rate factual/source accuracy, important concept coverage, mathematical correctness, and study usability from 1 (poor) to 5 (excellent). '
        'For quizzes, answer first without seeing the generator key, then inspect the separate key-reveal file. Record ambiguity and weak distractors. '
        'Use NA when a criterion does not apply. Do not count successful JSON or page mentions as concept mastery.\n']
    for label,arm in labels.items():
        out=ROOT/'outputs'/arm
        for lecture in corpus:
            name=lecture['id'];parts.append('\n## Approach '+label+' / '+lecture['name']+'\n')
            pages={p['source_id']:p for p in lecture['pages']}
            def links(text):
                def replace(m):
                    ids=re.findall(r'[0-9a-f]{12}:p\d+',m[1]);items=[]
                    for sid in ids:
                        if sid in pages:items.append('['+sid+'](<../'+Path(pages[sid]['image']).as_posix()+'>)')
                    return ' '.join(items) or m[0]
                return re.sub(r'\[((?:[0-9a-f]{12}:p\d+)(?:,\s*[0-9a-f]{12}:p\d+)*)\]',replace,text)
            def row(kind,ident,value):
                rows.append({'approach':label,'lecture':name,'kind':kind,'item':ident,'output_hash':digest(value),
                    'source_accuracy_1_5':'','concept_coverage_1_5':'','math_correctness_1_5_or_NA':'',
                    'study_usability_1_5':'','reviewer_answer_for_quiz':'','ambiguity_or_weak_distractors':'','comments':''})
            reviews=read(out/(name+'_chapter_reviews.json'),[])
            for review in reviews:
                chapter=read(out/'records'/(review['chapter_id']+'.json'),{})
                if not chapter.get('parsed'):continue
                parsed=chapter['parsed'];parts.append('\n### '+chapter['id']+' / '+parsed['title']+'\n\n'+links(parsed['body'])+'\n')
                row('guide',chapter['id'],parsed)
            quiz=read(out/(name+'_quiz_checked.json'),read(out/(name+'_quiz.json'),{}))
            for i,q in enumerate(quiz.get('questions',[]),1):
                ident=name+'_quiz_'+str(i)
                parts.append('\n### '+ident+'\n\n'+q['stem']+'\n\n'+'\n'.join(chr(65+j)+'. '+text for j,text in enumerate(q['options']))+'\n\nSource pages: '+links(' '.join('['+sid+']' for sid in q['citations']))+'\n')
                row('quiz',ident,q);keys.append({'approach':label,'lecture':name,'item':ident,'model_key':chr(65+q['answer_index']),'explanation':q['explanation']})
            for task in [t for t in tasks if t['lecture']==name]:
                record=read(out/'records'/(task['id']+'.json'),{})
                if not record.get('parsed'):continue
                parts.append('\n### '+task['id']+'\n\nQuestion/task: '+task.get('question',task['instruction'])+'\n\n'+links('\n\n'.join(str(v) for k,v in record['parsed'].items() if k not in ['citations','uncertainties']))+'\n')
                row(task['kind'],task['id'],record['parsed'])
    (target/'MASKED_OUTPUTS.md').write_text('\n'.join(parts),encoding='utf-8')
    # Never overwrite ratings already supplied by an instructor.
    path=target/'INSTRUCTOR_REVIEW.csv'
    existing=[]
    if path.exists():
        with path.open(encoding='utf-8-sig',newline='') as f:existing=list(csv.DictReader(f))
    old={(r['approach'],r['lecture'],r['kind'],r['item'],r['output_hash']):r for r in existing}
    for i,row in enumerate(rows):
        key=tuple(row[k] for k in ['approach','lecture','kind','item','output_hash'])
        if key in old:rows[i]=old[key]
    if rows:
        with path.open('w',encoding='utf-8-sig',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    write(ROOT/'quiz_key_reveal.json',keys)
    print('Source-linked review rows:',len(rows),'Human ratings not fabricated.')

if __name__=='__main__':build()
