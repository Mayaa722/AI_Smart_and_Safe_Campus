"""Official text-context RAGAS on actual v2 direct answers, never multimodal claims."""
import argparse,asyncio,csv,importlib.metadata,math,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from comparison.common import read,write,digest
import evaluation.providers as providers
from evaluation.providers import Transport,GeminiJudge,GeminiEmbeddings
from ragas.metrics.collections import Faithfulness,AnswerRelevancy

ROOT=Path(__file__).resolve().parents[1]/'run/comparison_v2'

async def evaluate(arms,task_ids=None,recover_schema503=False):
    assert importlib.metadata.version('ragas')=='0.4.3'
    providers.RUNTIME=ROOT/'judge_runtime'
    config={'ragas_version':'0.4.3','judge_model':'gemini-3.5-flash-lite','embedding_model':'gemini-embedding-001',
        'judge_temperature':0,'max_output_tokens':2500,'schema_mode':'google_native','max_network_requests':120,
        'max_reserved_tokens':4000000,'max_prompt_chars':50000,'max_generation_requests_per_minute':4,
        'answer_relevancy_strictness':3}
    t=Transport(config);t.schema_503_fallback=recover_schema503
    judge=GeminiJudge(t);embedding=GeminiEmbeddings(t)
    metrics={'faithfulness':Faithfulness(llm=judge),'answer_relevancy':AnswerRelevancy(llm=judge,embeddings=embedding,strictness=3)}
    tasks=[task for task in read(ROOT/'tutor_tasks.json') if task['kind']=='direct']
    if task_ids:tasks=[task for task in tasks if task['id'] in task_ids]
    old_tasks={task['id']:task for task in read(ROOT.parent/'comparison_v1/package/tasks.json')}
    for task in tasks:
        assert task['retrieved_contexts']==old_tasks[task['id']]['retrieved_contexts']
        assert task['reference']==old_tasks[task['id']]['reference']
    scores=read(ROOT/'ragas_scores.json',{'config':config,'rows':[],
        'scope':'Original extracted text only. Generation also saw images; a low text-support score does not establish that image-supported mathematics is wrong.',
        'references':'Assistant-authored development references; independent instructor validation pending.'})
    done={r['cache_key']:r for r in scores['rows']}
    inherited=[dict(r,origin='Inherited estimate for exactly identical fixed original-text retrieval, reference and question; not a new production retrieval measurement')
        for r in read(ROOT.parent/'comparison_v1/ragas_scores.json')['rows'] if r['arm']=='shared_retrieval']
    scores['shared_retrieval']=inherited
    samples=[]
    for arm in arms:
        for task in tasks:
            record=read(ROOT/'outputs'/arm/'records'/(task['id']+'.json'),{})
            if not record.get('parsed'):continue
            sample={'arm':arm,'task':task['id'],'question':task['question'],'answer':record['parsed']['answer'],
                'retrieved_contexts':task['retrieved_contexts'],'reference':task['reference'],
                'generation_id':record['id'],'generation_identity':record['identity'],'generation_images':record['image_manifest'],
                'judge_context':'original extracted text only; no image faithfulness claim'}
            samples.append(sample)
            existing=read(ROOT/'captured_ragas_samples.json',[])
            write(ROOT/'captured_ragas_samples.json',[s for s in existing if (s['arm'],s['task'])!=(arm,task['id'])]+[sample])
            for name in metrics:
                args={'user_input':sample['question'],'response':sample['answer']}
                if name=='faithfulness':args['retrieved_contexts']=sample['retrieved_contexts']
                key=digest({'metric':name,'args':args,'config':config})
                if done.get(key,{}).get('value') is not None:continue
                t.active_metric=name;t.active_sample=arm+'_'+task['id']
                row={'arm':arm,'task_id':task['id'],'metric':name,'cache_key':key,'value':None,'status':'error',
                     'output_hash':digest(record['parsed']),'scope':'original_extracted_text'}
                trace_start=len(t.trace)
                try:
                    result=await metrics[name].ascore(**args);value=float(result.value)
                    if not math.isfinite(value):raise ValueError('Nonfinite score')
                    row.update(value=value,status='completed')
                except Exception as exc:row['error']=str(exc).replace(t.key,'[REDACTED]')[:300]
                row['transport_trace']=t.trace[trace_start:]
                scores['rows']=[r for r in scores['rows'] if r['cache_key']!=key]+[row];done[key]=row
                write(ROOT/'ragas_scores.json',scores)
                print(arm,task['id'],name,row.get('value'),row.get('error',''),flush=True)
                if '429' in row.get('error',''):raise RuntimeError('Judge quota stop; recorded scores preserved, no quota reset.')
    with (ROOT/'ragas_scores.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=['arm','task_id','metric','value','status','error','scope'],extrasaction='ignore');w.writeheader();w.writerows(scores['rows'])

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--arms',default='gemini35,qwen');p.add_argument('--tasks');p.add_argument('--recover-schema503',action='store_true');a=p.parse_args()
    asyncio.run(evaluate(a.arms.split(','),a.tasks.split(',') if a.tasks else None,a.recover_schema503))
