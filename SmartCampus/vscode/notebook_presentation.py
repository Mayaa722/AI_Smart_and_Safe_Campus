"""Portable notebook presentation: Markdown first; no widget renderer required."""
import re
from pathlib import Path
from urllib.parse import quote
from IPython.display import display, Markdown
from math_rendering import math_markdown
from tutor_rendering import tutor_text


def show_tutor_response(record, retrieval, workflow):
    display({'status': record['status'],
             'selected_pages': [s['page'] for s in retrieval['selected']]})
    if record.get('parsed'):
        if record.get('content_review',{}).get('status') not in {None,'passed_automated_review'}:
            display(Markdown('**Review required:** inspect the cited original pages before relying on this answer.'))
        display(Markdown(math_markdown(link_sources(tutor_text(record), workflow))))
        if record.get('display_answer'):
            display({'original_generated_draft':tutor_text(record,draft=True),'local_math_review':record.get('local_math_review')})
    elif any('provider_http_503' in str(w) for w in record.get('warnings', [])):
        display(Markdown('**Gemini is temporarily busy (HTTP 503).** '
                         'Your question reached the service, but no answer was returned. '
                         'Wait briefly, then rerun this cell to try again. '
                         'The attempt is recorded in the existing request budget.'))
    else:
        display(record.get('warnings') or ['No answer was returned.'])


def link_sources(text, workflow):
    text=re.sub(r'(?<=\w)_(?=\w)',r'\\_',text)
    text=re.sub(r'(?<=[\w)])\*(?=[\w(])',r'\\*',text)
    def replace(match):
        ids=re.findall(r'[0-9a-f]{12}:p\d+',match.group(1))
        links=[]
        for sid in ids:
            page=workflow.ns['PAGES'].get(sid)
            if not page or not page.get('image'):
                links.append('['+sid+']');continue
            image=Path(page['image'])
            try:path=image.relative_to(Path(__file__).resolve().parent).as_posix()
            except ValueError:path=image.as_posix()
            links.append(f"[p. {page['page']}](<{quote(path,safe='/.:')}>)")
        return ' '.join(links)
    return re.sub(r'\[((?:[0-9a-f]{12}:p\d+)(?:,\s*[0-9a-f]{12}:p\d+)*)\]',replace,text)


def show_quiz(quiz,workflow):
    def literal(value):return value.replace('*',r'\*').replace('_',r'\_')
    for i,q in enumerate(quiz['questions'],1):
        options='\n\n'.join(f"**{chr(65+j)}.** {literal(option)}" for j,option in enumerate(q['options']))
        display(Markdown(f"### {i}. {literal(q['stem'])}\n\n{options}"))


def grade_quiz(quiz,answers):
    questions=quiz['questions']
    if not isinstance(answers,list) or len(answers)!=len(questions):
        raise ValueError(f'Enter exactly {len(questions)} answers as letters A, B, C or D.')
    if any(not isinstance(a,str) or a.strip().upper() not in {'A','B','C','D'} for a in answers):
        raise ValueError('Answer every question with A, B, C or D before submitting.')
    rows=[{'item':i,'selected':answer.strip().upper(),'key':chr(65+q['answer_index']),
           'correct':answer.strip().upper()==chr(65+q['answer_index']),
           'explanation':q['explanation'],'citations':q['citations']}
          for i,(q,answer) in enumerate(zip(questions,answers),1)]
    return {'score':sum(r['correct'] for r in rows),'total':len(rows),'items':rows}


def show_quiz_feedback(result,workflow):
    display(Markdown(f"**Score against the source-reviewed draft key: {result['score']}/{result['total']}**"))
    for row in result['items']:
        text=f"**{row['item']}. {'Correct' if row['correct'] else 'Review'} — key {row['key']}**\n\n{row['explanation']}"
        display(Markdown(link_sources(text,workflow)))
