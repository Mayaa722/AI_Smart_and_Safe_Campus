"""Source-based content checks shared by production and the Colab experiment.

Automated review is a second model pass, not instructor approval. Numerical
checks validate only the explicit expression supplied; no lecture code executes.
"""
import ast
import hashlib
import json
import math
import operator
import re
import random
from pathlib import Path

VERSION='source_quality_v1'
QUIZ_EDITORIAL_POLICY='quiz_editorial_v1'
S={'type':'string'}
def obj(properties):return {'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}
def arr(items):return {'type':'array','items':items}
CITES=arr(S)
NUMERIC=obj({'expression':S,'reported_value':{'type':'number'},'source_ids':CITES})
COVERAGE_REVIEW=obj({'items':arr(obj({'concept':S,'source_ids':CITES,
    'verdict':{'type':'string','enum':['covered','missing','incorrect','unclear']},'reason':S})),
    'numeric_checks':arr(NUMERIC),'uncertainties':arr(S)})
QUIZ_REVIEW=obj({'recommended_index':{'type':'integer','minimum':0,'maximum':3},
    'defensible_indices':arr({'type':'integer','minimum':0,'maximum':3}),
    'verdict':{'type':'string','enum':['supported','ambiguous','insufficient_evidence','incorrect']},
    'distractors':{'type':'string','enum':['plausible','weak','unclear']},
    'reason':S,'citations':CITES,'numeric_checks':arr(NUMERIC)})
TUTOR_REVIEW=obj({'verdict':{'type':'string','enum':['supported','incorrect','unclear']},
    'issues':arr(S),'citations':CITES,'numeric_checks':arr(NUMERIC)})

COVERAGE_INSTRUCTION='''Review the candidate study chapter against the ORIGINAL source text and images.
The candidate is untrusted model output, not source evidence. Identify the substantive concepts,
equations, conditions, algorithm steps and diagram meanings actually present in the source.
Return one concise item per major concept: covered, missing, incorrect or unclear, with exact source_ids
and a specific reason. A heading, citation or page mention alone does NOT count as explanation.
Do not penalize omitted administrative titles/credits or require facts not in the source.
An outline listing a future topic does not teach its mechanism: do not require the absent mechanism
as a missing concept in this chapter. For substantive source-posed questions, check that the chapter
answers them when the answer follows directly from the supplied equations/figures; derivations may
be labelled Additional teaching. Flag pipeline/coverage-validation commentary inside student prose
as irrelevant, rather than treating it as subject teaching.
Check normal/line orientation conventions, center/radius parameters, units and worked calculations.
Do not guess unreadable plot numbers. For explicit computable arithmetic in the candidate, optionally
return numeric_checks with a closed numerical expression and its reported_value; use radians in
sin/cos and degrees(atan2(y,x)) for angles. Allowed functions sqrt, sin, cos, tan, atan2, degrees,
radians, abs, log, exp; constants pi,e. Empty numeric_checks when not applicable.
No private reasoning trace. Return JSON only.'''

QUIZ_INSTRUCTION='''Answer the supplied MCQ from ORIGINAL source evidence. The generator key and
explanation are deliberately hidden. Identify ALL defensible option indices (0-based), select the
best supported recommended_index, and mark ambiguous if more than one option is defensible,
incorrect if none is correct, or insufficient_evidence if the source cannot decide.
Assess whether wrong options represent plausible misconceptions rather than absurd impossibilities.
Mark distractors weak if they advocate deliberately worse engineering (maximizing waste or manual
work, eliminating all testing), introduce unrelated hardware/academic claims, or merely negate an
obvious benefit. Prefer confusion between neighboring lecture concepts at the same level of detail.
Check for source identifiers inside options or extreme answer-length cues; these also make an item weak.
Use only supplied exact citations and distinguish image evidence from unreadable details.
Optionally provide explicit numeric_checks for calculations, using closed numerical expressions only.
Return a short reason, not a private reasoning trace. Return JSON only.'''

def fingerprint(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def normalize_citations(value):
    """Only format normalization; never manufacture fields, keys or source IDs."""
    if isinstance(value,dict):
        return {k:([x[1:-1] if isinstance(x,str) and re.fullmatch(r'\[[0-9a-f]{12}:p\d+\]',x) else x for x in v]
                   if k in {'citations','source_ids'} and isinstance(v,list) else normalize_citations(v)) for k,v in value.items()}
    if isinstance(value,list):return [normalize_citations(x) for x in value]
    return value

def quiz_for_practice(quiz,seed):
    """Stable balanced option positions in a display copy; raw model output unchanged."""
    rng=random.Random(fingerprint(seed));targets=[i%4 for i in range(len(quiz['questions']))];rng.shuffle(targets)
    result=json.loads(json.dumps(quiz));permutations=[]
    for question,target in zip(result['questions'],targets):
        original_key=question['answer_index'];others=[i for i in range(4) if i!=original_key];rng.shuffle(others)
        order=others[:];order.insert(target,original_key)
        question['options']=[question['options'][i] for i in order];question['answer_index']=target
        mapping={chr(65+old):chr(65+new) for new,old in enumerate(order)}
        question['explanation']=re.sub(r'\b([Oo]ption|[Cc]hoice|[Aa]nswer)\s+([ABCD])\b',
            lambda m:m[1]+' '+mapping[m[2]],question['explanation'])
        permutations.append(order)
    result['display_order']={'permutations':permutations,'source_quiz_hash':fingerprint(quiz),
        'note':'Presentation-only reordering; the correct option text is preserved.'}
    return result

def external_content_findings(record):
    """Do not silently override a source review of these exact question bytes."""
    review=record.get('external_content_review',{})
    if review.get('parsed_fingerprint')!=fingerprint(record.get('parsed')):return []
    return review.get('findings',[]) if review.get('status')=='needs_review' else []

def external_quiz_findings(record):
    return external_content_findings(record)


def quiz_editorial_findings(question):
    """Conservative review flags, not a measurement of calibrated difficulty."""
    findings=[];options=question['options'];key=question['answer_index']
    if any(re.search(r'\[[0-9a-f]{12}:p\d+',option) for option in options):
        findings.append('Source identifiers inside options can reveal the key; citations belong in metadata and explanations.')
    lengths=sorted(len(option.split()) for option in options)
    if lengths[-1]>2.5*max(1,(lengths[1]+lengths[2])/2):
        findings.append('One option is much longer than its alternatives; inspect answer-length cues.')
    patterns=[r'(?:maximiz\w+|increas\w+)\s+(?:manual|waiting|waste|risk|errors)',
        r'\b(?:manual|inconsisten\w*)\b.{0,65}\bmaximiz\w*',
        r'(?:eliminat\w+|bypass\w+|ignor\w+|prevent\w+)\b.{0,45}\b(?:testing|version control|traceability|monitoring|repositories)',
        r'\b(?:eliminat\w+|replac\w+|bypass\w+)\b.{0,35}\b(?:all|any|entirely|the need)\b',
        r'lose all traceability',r'completely eliminat\w+.{0,25}(?:testing|deployment)',
        r'(?:save|saving) storage space']
    for index,option in enumerate(options):
        if index!=key and any(re.search(pattern,option,re.I) for pattern in patterns):
            findings.append('Option '+chr(65+index)+' uses language often associated with an implausible distractor; review it against the stem.')
    return findings

def numerical_value(expression):
    if len(expression)>180:raise ValueError('Expression too long')
    tree=ast.parse(expression.replace('^','**'),mode='eval')
    if sum(1 for _ in ast.walk(tree))>65:raise ValueError('Expression too complex')
    binary={ast.Add:operator.add,ast.Sub:operator.sub,ast.Mult:operator.mul,ast.Div:operator.truediv,ast.Pow:operator.pow}
    functions={k:getattr(math,k) for k in ['sqrt','sin','cos','tan','atan2','degrees','radians','log','exp']};functions['abs']=abs
    def calc(n):
        if isinstance(n,ast.Constant) and type(n.value) in [int,float]:return float(n.value)
        if isinstance(n,ast.Name) and n.id in ['pi','e']:return getattr(math,n.id)
        if isinstance(n,ast.UnaryOp) and isinstance(n.op,(ast.UAdd,ast.USub)):return calc(n.operand)*(1 if isinstance(n.op,ast.UAdd) else -1)
        if isinstance(n,ast.BinOp) and type(n.op) in binary:
            x,y=calc(n.left),calc(n.right)
            if isinstance(n.op,ast.Pow) and (abs(y)>12 or abs(x)>1e9):raise ValueError('Exponent out of bounds')
            return binary[type(n.op)](x,y)
        if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id in functions and not n.keywords:
            return functions[n.func.id](*[calc(a) for a in n.args])
        raise ValueError('Unsupported numerical expression')
    value=calc(tree.body)
    if not isinstance(value,(int,float)) or not math.isfinite(value):raise ValueError('Nonfinite result')
    return value

def numerical_audit(checks,allowed):
    rows=[]
    for item in checks:
        row=dict(item)
        try:
            if not item['source_ids'] or set(item['source_ids'])-set(allowed):raise ValueError('Invalid check evidence')
            expected=numerical_value(item['expression']);reported=float(item['reported_value'])
            if not math.isfinite(reported):raise ValueError('Nonfinite reported value')
            tolerance=max(.02,.01*abs(expected))
            row.update(calculated=expected,tolerance=tolerance,status='consistent' if abs(expected-reported)<=tolerance else 'needs_review')
        except (ValueError,TypeError,KeyError,OverflowError,ZeroDivisionError,SyntaxError) as exc:
            row.update(status='unverified',reason=str(exc))
        rows.append(row)
    return rows


def line_intercept_consistency(answer):
    """Narrow internal geometry check; never verifies numbers read from an image.

    Applies only to explicitly labelled positive intercepts, rho, and a normal
    angle in degrees. Other phrasings remain unverified by this particular check.
    """
    text=answer.replace('\\rho','rho').replace('\\theta','theta').replace('ρ','rho').replace('θ','theta')
    text=re.sub(r'[${}\\()]',' ',text)
    if not re.search(r'normal.form|perpendicular distance',text,re.I):return {'status':'not_applicable'}
    number=r'(\d+(?:\.\d+)?)'
    qualifier=r'\s*(?:(?:is|=|:|approximately|approx\.?|about|around|roughly|estimated|at|of)\s*){0,5}'
    def intercept(axis):
        found=re.search(axis+r'[- ]intercept'+qualifier+number+r'(?:\s*or\s*'+number+r')?',text,re.I)
        if not found and re.search(r'line intersects',text,re.I):
            found=re.search(axis+r'[- ]axis\s+at'+qualifier+r'(?:'+axis+r'\s*=\s*)?'+number,text,re.I)
        if not found:return None
        values=[float(v) for v in found.groups() if v is not None]
        return sorted(values)
    a=intercept('x');b=intercept('y')
    rho=re.search(r'\brho\b'+qualifier+number,text,re.I)
    if not rho:
        rho=re.search(r'\brho\b[^.!?]{0,65}?\blength\s+of'+qualifier+number,text,re.I)
    theta=re.search(r'\btheta\b'+qualifier+number+r'\s*(?:degrees|degree|deg\b|°)',text,re.I)
    if not all([a,b,rho,theta]) or min(a+b)<=0:return {'status':'not_applicable'}
    distances=[x*y/math.hypot(x,y) for x in a for y in b]
    angles=[math.degrees(math.atan2(x,y)) for x in a for y in b]
    reported_rho=float(rho[1]);reported_theta=float(theta[1])
    consistent=(min(distances)-.15<=reported_rho<=max(distances)+.15 and
                min(angles)-3<=reported_theta<=max(angles)+3)
    return {'status':'consistent' if consistent else 'needs_review','x_intercept_range':a,'y_intercept_range':b,
        'reported_rho':reported_rho,'reported_theta_degrees':reported_theta,
        'rho_range_from_intercepts':[min(distances),max(distances)],
        'normal_angle_range_degrees':[min(angles),max(angles)],
        'tolerance':{'distance':.15,'angle_degrees':3},
        'limitation':'Internal consistency only; positive-intercept normal-angle convention. Intercept readings still require original-image verification.'}


def apply_tutor_math_guard(record):
    answer=(record.get('parsed') or {}).get('answer','')
    result=line_intercept_consistency(answer)
    record['local_math_review']=result
    if result['status']=='needs_review':
        citations=' '.join('['+sid+']' for sid in record.get('source_ids',[]))
        record['display_answer']=(
            'The numerical estimates in the generated draft are internally inconsistent, so they are withheld pending review of the original plot. '
            'The original draft is retained for inspection.\n\n'
            '**Additional teaching — consistency method.** For positive axis intercepts $a$ and $b$, '
            'the line is $x/a+y/b=1$. In normal form $x\\cos\\theta+y\\sin\\theta=\\rho$, '
            '$\\rho=ab/\\sqrt{a^2+b^2}$ and $\\theta=\\operatorname{atan2}(a,b)$. '
            'Here $\\theta$ is the normal angle; convert it from radians to degrees when required. '
            'Verify the intercepts on the original plot before substituting numbers. '+citations)
        record.setdefault('content_review',{})['status']='needs_review'
    return result

def coverage_result(review,allowed):
    items=review['items']
    if not items or any(not i['source_ids'] or set(i['source_ids'])-set(allowed) for i in items):
        raise ValueError('Semantic review needs source-linked concept decisions')
    numeric=numerical_audit(review['numeric_checks'],allowed)
    flags=[i for i in items if i['verdict']!='covered']
    return {'status':'passed_automated_review' if not flags and all(n['status']=='consistent' for n in numeric) else 'needs_review',
            'concepts_checked':len(items),'flags':flags,'numeric_checks':numeric,
            'limitation':'Same-model source comparison and selected arithmetic checks; not independent instructor approval or proof of completeness.'}

def quiz_result(review,question,allowed):
    if not review['citations'] or set(review['citations'])-set(allowed):raise ValueError('Quiz audit needs valid evidence')
    numeric=numerical_audit(review['numeric_checks'],allowed)
    unique=review['defensible_indices']==[review['recommended_index']]
    match=review['recommended_index']==question['answer_index']
    editorial=quiz_editorial_findings(question)
    passed=review['verdict']=='supported' and unique and match and review['distractors']=='plausible' and all(n['status']=='consistent' for n in numeric) and not editorial
    return {'status':'passed_automated_review' if passed else 'needs_review','key_matches':match,
            'unique_supported_answer':unique,'audit':review,'numeric_checks':numeric,'editorial_findings':editorial,
            'limitation':'Key-hidden model audit, not independent human validation; no silent key substitution.'}

def page_evidence(w,ids):
    return '\n\n'.join('['+sid+'] ORIGINAL EXTRACTED TEXT\n'+w.ns['PAGES'][sid]['text'] for sid in ids)

def evidence_fingerprint(w,ids):
    return fingerprint([{'source_id':sid,'text':w.ns['PAGES'][sid]['text'],
        'image_sha256':hashlib.sha256(Path(w.ns['PAGES'][sid]['image']).read_bytes()).hexdigest()} for sid in ids])

def review_chapter(w,entry,ids,validator=None):
    """Persist review by output hash; repair only this chapter at most once."""
    reviews=w.state.setdefault('content_reviews',{})
    if all(w.page_notes.get(sid,{}).get('kind')=='administrative' for sid in ids):
        entry['content_review']={'result':{'status':'passed_automated_review','concepts_checked':0,
            'limitation':'Source extraction marked every page administrative; no substantive completeness claim.'}}
        return entry,True
    for revision in range(2):
        key=fingerprint({'version':VERSION,'kind':'chapter','output':entry['parsed'],'ids':ids,
                         'evidence':evidence_fingerprint(w,ids),'audit_instruction':COVERAGE_INSTRUCTION,
                         'audit_schema':COVERAGE_REVIEW,'model':w.ns['CFG']['model']})
        saved=reviews.get(key)
        if saved is None:
            context=page_evidence(w,ids)+'\nCANDIDATE CHAPTER (NOT EVIDENCE)\n'+entry['parsed']['body']
            record=w.request('chapter_source_review',[w.ns['PAGES'][sid] for sid in ids],COVERAGE_INSTRUCTION,
                             schema=COVERAGE_REVIEW,context=context,images=True)
            if not record.get('parsed'):return entry,False
            try:result=coverage_result(record['parsed'],ids)
            except ValueError as exc:result={'status':'needs_review','reason':str(exc)}
            saved={'record':record,'result':result,'source_output_hash':entry.get('output_hash'),'policy':VERSION}
            reviews[key]=saved;w.save()
        if saved['result']['status']=='passed_automated_review':
            entry['content_review']=saved;return entry,True
        if revision==0:
            from comprehensive_guide import CHAPTER_SCHEMA
            context=page_evidence(w,ids)+'\nPREVIOUS CHAPTER\n'+json.dumps(entry['parsed'])+'\nREVIEW FINDINGS\n'+json.dumps(saved['result'])
            revised=w.request('chapter_targeted_repair',[w.ns['PAGES'][sid] for sid in ids],
                'Correct only this study chapter using original sources and the review findings. Preserve valid material. '
                'Keep all page and concept coverage IDs and headings from the candidate, adding actual missing explanations. '
                'Answer source questions when their answers follow from the provided evidence, labelling new derivations Additional teaching. '
                'Keep schemas, coverage pointers and validation commentary out of the student chapter body. '
                'Use ASCII math, full source citations and label extra examples Additional teaching. Return the full chapter JSON.',
                schema=CHAPTER_SCHEMA,context=context,images=True,validator=validator)
            if not revised.get('parsed'):return entry,False
            w.state.setdefault('quality_revisions',[]).append({'original':entry,'replacement_id':revised['id'],'review_key':key})
            entry=revised
    entry['content_review']=saved
    w.state['last_processing_error']={'task':'chapter_source_review','warnings':['Content review needs instructor attention; saved chapter is a draft.'],
                                     'source_ids':ids,'generation_id':entry['id'],'model':entry.get('model')}
    return entry,False

def review_quiz(w,record):
    reviews=w.state.setdefault('quiz_content_reviews',{})
    questions=[];results=[]
    for ordinal,original in enumerate(record['parsed']['questions'],1):
        question=dict(original)
        # A source-audit outage must not discard an already generated repair.
        # Reuse only the replacement of this exact original question, then audit
        # it under the current evidence/instruction fingerprint before release.
        for saved_revision in reversed(w.state.get('quiz_item_revisions',[])):
            if saved_revision.get('item')==ordinal and saved_revision.get('original')==original:
                question=dict(saved_revision['replacement']);break
        for revision in range(2):
            ids=list(dict.fromkeys(question['citations']))
            key=fingerprint({'version':VERSION,'question':question,'evidence':evidence_fingerprint(w,ids),
                             'audit_instruction':QUIZ_INSTRUCTION,'audit_schema':QUIZ_REVIEW,'editorial_policy':QUIZ_EDITORIAL_POLICY,'model':w.ns['CFG']['model']})
            saved=reviews.get(key)
            if saved is None:
                candidate={'stem':question['stem'],'options':question['options']}
                context=page_evidence(w,ids)+'\nQUESTION (GENERATOR KEY AND EXPLANATION HIDDEN)\n'+json.dumps(candidate)
                audit=w.request('quiz_source_review',[w.ns['PAGES'][sid] for sid in ids],QUIZ_INSTRUCTION,
                                schema=QUIZ_REVIEW,context=context,images=True)
                if not audit.get('parsed'):
                    w.save();return record,False
                result=quiz_result(audit['parsed'],question,ids)
                saved={'record':audit,'result':result,'question_hash':key};reviews[key]=saved;w.save()
            if saved['result']['status']=='passed_automated_review':break
            if revision==0:
                schema=w.ns['MCQ_SCHEMA']['properties']['questions']['items']
                context=page_evidence(w,ids)+'\nCANDIDATE\n'+json.dumps(question)+'\nREVIEW\n'+json.dumps(saved['result'])
                def valid_item(item):
                    if not all(r['structural_valid'] for r in w.ns['validate_mcqs']({'questions':[item]},ids)):
                        raise ValueError('Quiz structure validation failed: repaired question.')
                repaired=w.request('quiz_item_repair',[w.ns['PAGES'][sid] for sid in ids],
                    'Replace this single flawed question using the source and review. Do not copy the previous options. '
                    'Wrong choices must use real concepts from this lecture with subtly wrong relationships or assumptions. '
                    'Avoid unrelated hardware, programming syntax, academic-only claims, absolutes or obviously absurd alternatives. '
                    'Use four distinct plausible misconception options, '
                    'Use parallel option lengths and grammar. Put no citations or source identifiers inside options; keep them in citations and the explanation. '
                    'exactly one defensible answer, correct answer_index, supported explanation, exact citations. '
                    'Do not use all/none-of-the-above or absurd alternatives. Return one question object.',
                    schema=schema,context=context,images=True,validator=valid_item)
                if not repaired.get('parsed'):
                    w.save();return record,False
                question=repaired['parsed']
                w.state.setdefault('quiz_item_revisions',[]).append({'item':ordinal,'original':original,'replacement':question,'record_id':repaired['id']})
        questions.append(question);results.append(saved['result'])
    updated=dict(record,parsed={'questions':questions})
    updated['output_hash']=w.ns['digest'](updated['parsed'])
    updated['content_review']={'policy':VERSION,'editorial_policy':QUIZ_EDITORIAL_POLICY,'status':'passed_automated_review' if all(r['status']=='passed_automated_review' for r in results) else 'needs_review','items':results}
    structural=w.ns['validate_mcqs'](updated['parsed'],list(w.ns['PAGES']))
    stems=[' '.join(q['stem'].casefold().split()) for q in questions]
    if len(set(stems))!=len(stems) or not all(r['structural_valid'] for r in structural):
        updated['content_review']['status']='needs_review'
        updated['content_review']['structure_warning']='Revised quiz failed structure or duplicate-stem checks.'
    if questions!=record['parsed']['questions']:
        w.state.setdefault('superseded_artifacts',[]).append({'artifact':'lecture_quiz','record':record,'reason':'Source-review item repairs; original preserved.'})
    w.state['artifacts']['lecture_quiz']=updated;w.save()
    return updated,updated['content_review']['status']=='passed_automated_review'

def review_tutor(w,record,question,ids):
    local_math=apply_tutor_math_guard(record)
    from tutor_rendering import tutor_text
    context=page_evidence(w,ids)+'\nQUESTION\n'+question+'\nCANDIDATE RESPONSE INCLUDING NEXT QUESTION (NOT EVIDENCE)\n'+tutor_text(record,draft=True)
    audit=w.request('tutor_source_review',[w.ns['PAGES'][sid] for sid in ids],
        'Check this answer against original text and images for this QUESTION ONLY. Do not demand coverage of unrelated page topics. '
        'Flag incorrect equations, normal/line angle confusion, unsupported numbers, inconsistent arithmetic or missing necessary conditions. '
        'Mark supported when the answer is source-supported or correctly abstains because evidence is missing; unclear for unreadable evidence. '
        'Give short issues and exact citations. numeric_checks may contain explicit closed arithmetic expressions and reported_value, '
        'using sqrt/sin/cos/tan/atan2/degrees/radians/abs/log/exp and pi,e only. No private reasoning trace.',
        schema=TUTOR_REVIEW,context=context,images=True)
    if not audit.get('parsed'):
        record['content_review']={'status':'unverified','reason':'Source review service unavailable; answer retained as a draft.'}
        if local_math['status']=='needs_review':record['content_review']['status']='needs_review'
        record.setdefault('display_answer','The answer was generated, but its source check could not finish. It is retained as an unverified draft below. Check the original lecture pages before relying on it.')
        return
    data=audit['parsed'];numbers=numerical_audit(data['numeric_checks'],ids)
    passed=(data['verdict']=='supported' and not data['issues'] and bool(data['citations'])
            and not set(data['citations'])-set(ids) and all(n['status']=='consistent' for n in numbers)
            and local_math['status']!='needs_review')
    record['content_review']={'status':'passed_automated_review' if passed else 'needs_review','audit':data,
        'numeric_checks':numbers,'review_record_id':audit['id'],'limitation':'Automated source check, not instructor approval.'}
    if not passed:
        record.setdefault('display_answer','The generated answer needs source or mathematical review. The draft is retained below for inspection; use the cited original pages to resolve the flagged points.')
