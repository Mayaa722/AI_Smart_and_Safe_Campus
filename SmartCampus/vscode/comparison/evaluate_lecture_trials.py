"""Four official paper-aligned RAGAS metrics on new live retrieval/answers.

One direct question per lecture per arm: at most 24 score values, FOUR metrics.
Contexts are ORIGINAL EXTRACTED TEXT, not a multimodal faithfulness assessment.
"""
import argparse
import asyncio
import math
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lecture_workflow import PROJECT,read,write,fingerprint
import evaluation.providers as providers
from evaluation.providers import Transport,GeminiJudge,GeminiEmbeddings
from ragas.metrics.collections import Faithfulness,AnswerRelevancy,ContextPrecision,ContextRecall

ROOT=PROJECT/'run/lecture_comparison_v3'
REFERENCES={
 'iot':'An HTTP application request is carried inside a TCP segment, which is carried in an IP packet inside an Ethernet frame. TCP source/destination ports identify application endpoints; the illustrated HTTP server listens on port 80 while the client uses an ephemeral port. IP source and destination addresses identify hosts and support routing. The data-link layer frames carry the IP packet over a link; Ethernet adds link-layer framing and its trailer.',
 'calculus':'From cosh(x)=(exp(x)+exp(-x))/2 and sinh(x)=(exp(x)-exp(-x))/2, subtraction of their squares gives ((exp(x)+exp(-x))^2-(exp(x)-exp(-x))^2)/4=1. Applying the quotient rule to tanh(x)=sinh(x)/cosh(x) gives (cosh(x)^2-sinh(x)^2)/cosh(x)^2=1/cosh(x)^2=sech(x)^2. cosh(x) is nonzero for every real x.',
 'software':'User requirements describe services and operational constraints in language and diagrams for customers. System requirements are more detailed descriptions for developers. Functional requirements specify system services or behaviour, while non-functional requirements constrain properties such as performance, reliability or usability and may affect overall architecture. A goal is a general intention such as ease of use; a testable requirement specifies an objectively measurable condition, such as training time and an error-rate threshold. These distinctions classify different aspects rather than making user requirements synonymous with functional requirements.'}

async def run(arms):
    providers.RUNTIME=ROOT/'ragas_runtime'
    config={'ragas_version':'0.4.3','judge_model':'gemini-3.5-flash-lite','embedding_model':'gemini-embedding-001',
        'judge_temperature':0,'max_output_tokens':3000,'schema_mode':'google_native','max_network_requests':110,
        'max_reserved_tokens':4000000,'max_prompt_chars':65000,'max_generation_requests_per_minute':2,
        'answer_relevancy_strictness':3}
    transport=Transport(config);transport.schema_503_fallback=True
    judge=GeminiJudge(transport);embedding=GeminiEmbeddings(transport)
    metrics={'faithfulness':Faithfulness(llm=judge),'answer_relevancy':AnswerRelevancy(llm=judge,embeddings=embedding,strictness=3),
        'context_precision':ContextPrecision(llm=judge),'context_recall':ContextRecall(llm=judge)}
    manifest=read(ROOT/'manifest.json')
    scores=read(ROOT/'ragas_scores.json',{'metrics':list(metrics),'config':config,'rows':[],
        'scope':'Original extracted text only; no visual faithfulness claim. All current retrieval is measured anew, no inherited estimates.',
        'reference_policy':'Assistant-authored development references grounded in supplied sources, pending instructor validation.',
        'judge_bias':'Fixed Gemini35 judge for both generators; same-family bias remains. No scores establish learning gain.'})
    write(ROOT/'ragas_references.json',REFERENCES)
    for arm in arms:
        outcomes=read(ROOT/f'{arm}_results.json',{})
        for name,spec in manifest['lectures'].items():
            if name not in REFERENCES:
                scores['references_pending']=sorted(set(scores.get('references_pending',[])+[name]))
                write(ROOT/'ragas_scores.json',scores)
                continue
            case=outcomes.get(name,{}).get('cases',{}).get('direct',{})
            if not case.get('parsed'):continue
            source_manifest=read(Path(read(Path(spec['configs'][arm]))['document_manifest']))[0]
            pages={p['source_id']:p for p in source_manifest['pages']}
            contexts=[pages[x['source_id']]['text'] for x in case['retrieval']['selected']]
            response=case['parsed']['answer']
            sample={'arm':arm,'lecture':name,'question':case['question'],'answer':response,'contexts':contexts,
                    'reference':REFERENCES[name],'generation_id':case['generation_id']}
            write(ROOT/'ragas_samples'/f'{name}_{arm}.json',sample)
            for metric in metrics:
                args={'user_input':case['question']}
                if metric in ['faithfulness','answer_relevancy']:args['response']=response
                if metric!='answer_relevancy':args['retrieved_contexts']=contexts
                if metric in ['context_precision','context_recall']:args['reference']=REFERENCES[name]
                key=fingerprint({'args':args,'metric':metric,'config':config})
                if any(x['cache_key']==key and x.get('value') is not None for x in scores['rows']):continue
                transport.active_metric=metric;transport.active_sample=name+'_'+arm
                row={'arm':arm,'lecture':name,'metric':metric,'cache_key':key,'value':None,'status':'error'}
                trace_start=len(transport.trace)
                try:
                    value=float((await metrics[metric].ascore(**args)).value)
                    if not math.isfinite(value):raise ValueError('Nonfinite metric')
                    row.update(value=value,status='completed')
                except Exception as exc:row['error']=str(exc).replace(transport.key,'[REDACTED]')[:250]
                row['transport_trace']=transport.trace[trace_start:]
                scores['rows']=[x for x in scores['rows'] if x['cache_key']!=key]+[row]
                write(ROOT/'ragas_scores.json',scores)
                print(arm,name,metric,row['status'],row['value'],flush=True)
                if '429' in row.get('error',''):return

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--arms',default='gemini,groq');a=p.parse_args();asyncio.run(run(a.arms.split(',')))
