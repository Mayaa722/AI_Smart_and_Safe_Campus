"""Post-hoc source-based key audit. Does not expose the generated key to the judge.

An experimental review stage, not a paper metric or a replacement for instructor review.
Original benchmark records and keys are never edited.
"""
import json
import sys
import time
from pathlib import Path
import requests
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from comparison.common import read,write,digest
from comparison.gemini_runner import load_key
from notebook_engine import load_engine

ROOT=Path(__file__).resolve().parents[1]/'run/comparison_v1'


def normalize_items(raw):
    """Normalize explicit JSON field aliases/brackets only; never change the chosen key."""
    items=json.loads(raw)['items']
    for item in items:
        if 'rationale' not in item and 'brief rationale' in item:item['rationale']=item['brief rationale']
        if 'citations' not in item and 'exact citations' in item:item['citations']=item['exact citations']
        if isinstance(item.get('citations'),list):
            item['citations']=[v.strip().removeprefix('[').removesuffix(']') for v in item['citations']]
    return items


def disagreements(items,questions):
    return [{'item':i['item'],'generated_key':questions[i['item']-1]['answer_index'],
             'audit_key':i['recommended_index'],'verdict':i['verdict'],'rationale':i['rationale']}
            for i in items if i['recommended_index']!=questions[i['item']-1]['answer_index']]


def normalize_saved():
    for path in (ROOT/'quiz_audits').glob('*.json'):
        if path.name=='request_ledger.json':continue
        record=read(path,{})
        if not record.get('raw') or record.get('status')=='completed':continue
        arm,lecture=path.stem.split('_',1)
        questions=read(ROOT/'outputs'/arm/(lecture+'_quiz.json'))['parsed']['questions']
        ids={sid for q in questions for sid in q['citations']}
        items=normalize_items(record['raw'])
        assert [i['item'] for i in items]==list(range(1,9))
        assert all(i['recommended_index'] in range(4) and i.get('citations') and set(i['citations'])<=ids for i in items)
        write(path.parent/'format_history'/path.name,record)
        record.update(status='completed',items=items,key_disagreements=disagreements(items,questions),
            format_normalization='Explicit brief rationale/exact citations aliases and enclosing brackets only; no keys or substantive text changed.')
        record.pop('error',None);write(path,record)
        print(path.stem,record['key_disagreements'])


def audit():
    key=load_key();ns=load_engine();out=ROOT/'quiz_audits';out.mkdir(exist_ok=True)
    ledger=read(out/'request_ledger.json',{'attempts':0,'requests':[]})
    for arm in ['gemini38','gemini35','qwen']:
        for lecture in ['hough','mlops']:
            source=read(ROOT/'outputs'/arm/(lecture+'_quiz.json'),{})
            if not source.get('parsed'):continue
            dest=out/(arm+'_'+lecture+'.json')
            if read(dest,{}).get('status')=='completed':continue
            if ledger['attempts']>=12:return
            questions=source['parsed']['questions']
            ids=list(dict.fromkeys(sid for q in questions for sid in q['citations']))
            context='\n\n'.join('['+sid+']\n'+ns['PAGES'][sid]['text'] for sid in ids)
            blinded=[{'item':i,'stem':q['stem'],'options':q['options']} for i,q in enumerate(questions,1)]
            prompt=('Independently answer these MCQs using the supplied source excerpts. The generator key and rationale are hidden. '
                    'Return eight items: item (1-based), recommended_index (0-based), verdict (supported, ambiguous, or insufficient_evidence), '
                    'brief rationale and exact citations. Do not invent missing visual evidence. Return JSON {"items":[...]}.\n'
                    +json.dumps(blinded)+'\nSOURCES\n'+context)
            settings={'temperature':0,'maxOutputTokens':2800,'responseMimeType':'application/json'}
            payload={'contents':[{'role':'user','parts':[{'text':prompt}]}],'generationConfig':settings}
            entry={'id':arm+'_'+lecture,'request_hash':digest(payload),'at':time.time()}
            ledger['attempts']+=1;ledger['requests'].append(entry);write(out/'request_ledger.json',ledger)
            record={'source_generation_id':source['id'],'source_output_hash':digest(source['parsed']),
                'judge_model':'gemini-3.5-flash-lite','generator_key_hidden':True,
                'scope':'post-hoc experimental audit; independent instructor confirmation required','status':'error'}
            try:
                r=requests.post('https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent',
                    headers={'x-goog-api-key':key},json=payload,timeout=(15,180))
                record['http_status']=r.status_code
                if r.status_code!=200:raise RuntimeError('Provider HTTP '+str(r.status_code))
                d=r.json();c=d['candidates'][0]
                raw=''.join(x.get('text','') for x in c['content']['parts'] if not x.get('thought'))
                record['raw']=raw;record['usage']=d.get('usageMetadata')
                if c.get('finishReason')!='STOP':raise ValueError('Incomplete audit response')
                items=normalize_items(raw)
                if len(items)!=8 or [i['item'] for i in items]!=list(range(1,9)):raise ValueError('Audit item count/order invalid')
                if any(i['recommended_index'] not in range(4) or not i.get('citations') or set(i['citations'])-set(ids) for i in items):
                    raise ValueError('Invalid audit key/citations')
                record.update(status='completed',items=items,
                    key_disagreements=disagreements(items,questions))
            except Exception as exc:record['error']=str(exc).replace(key,'[REDACTED]')[:300]
            write(dest,record);entry['status']=record['status'];write(out/'request_ledger.json',ledger)
            print(arm,lecture,record['status'],record.get('key_disagreements',record.get('error')),flush=True)
            time.sleep(10)


if __name__=='__main__':
    if '--normalize-saved' in sys.argv:normalize_saved()
    else:audit()
