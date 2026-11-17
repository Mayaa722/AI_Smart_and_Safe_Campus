"""Versioned visual, small-section comparison. Runs Qwen only inside Colab CUDA."""
import argparse
import base64
import copy
import gc
import json
import os
import sys
import time
from pathlib import Path
from datetime import datetime,timezone

if __package__:
    from comparison.common import read,write,digest,SYSTEM
else:
    from common import read,write,digest,SYSTEM
from lecture_quality import (obj,arr,S,COVERAGE_REVIEW,QUIZ_REVIEW,COVERAGE_INSTRUCTION,
    QUIZ_INSTRUCTION,coverage_result,quiz_result,normalize_citations)

CHAPTER=obj({'title':S,'body':S,'page_coverage':arr(obj({'source_id':S,'kind':{'type':'string','enum':['content','administrative','unclear']}})),
             'uncertainties':arr(S)})
QUESTION=obj({'stem':S,'options':{'type':'array','items':S,'minItems':4,'maxItems':4},
              'answer_index':{'type':'integer','minimum':0,'maximum':3},'explanation':S,'citations':arr(S),'topic':S})
QUIZ=obj({'questions':{'type':'array','items':QUESTION,'minItems':2,'maxItems':2}})
ANSWER=obj({'answer':S,'citations':arr(S),'uncertainties':arr(S)})

def constrained(schema,ids):
    schema=copy.deepcopy(schema)
    def walk(x):
        if isinstance(x,dict):
            props=x.get('properties',{})
            if 'source_id' in props:props['source_id']={'type':'string','enum':ids}
            for k in ['citations','source_ids']:
                if k in props:props[k]={'type':'array','items':{'type':'string','enum':ids},'minItems':1}
            for v in x.values():walk(v)
        elif isinstance(x,list):
            for v in x:walk(v)
    walk(schema);return schema

class Runner:
    def __init__(self,root,arm):
        self.root=Path(root);self.arm=arm;self.protocol=read(self.root/'protocol.json')
        self.out=self.root/'outputs'/arm;self.out.mkdir(parents=True,exist_ok=True)
        self.ledger=read(self.out/'request_ledger.json',{'attempts':0,'requests':[]})
        self.model_id=self.protocol['arms'][arm]
        self.hardware={};self.key=None
        if arm=='qwen':
            assert 'google.colab' in sys.modules,'Local inference is allowed only in Google Colab.'
            import torch,transformers,importlib.metadata
            from transformers import AutoProcessor,AutoModelForMultimodalLM,BitsAndBytesConfig
            assert torch.cuda.is_available(),'GPU required; CPU inference disabled.'
            revision=self.protocol['local_revision']
            self.processor=AutoProcessor.from_pretrained(self.model_id,revision=revision,trust_remote_code=False)
            started=time.perf_counter()
            self.model=AutoModelForMultimodalLM.from_pretrained(self.model_id,revision=revision,trust_remote_code=False,
                device_map={'':0},dtype=torch.float16,attn_implementation='sdpa',
                quantization_config=BitsAndBytesConfig(load_in_4bit=True,bnb_4bit_quant_type='nf4',
                    bnb_4bit_compute_dtype=torch.float16,bnb_4bit_use_double_quant=True)).eval()
            assert all(str(v) not in ['cpu','disk'] for v in getattr(self.model,'hf_device_map',{}).values())
            assert all(p.device.type=='cuda' for p in self.model.parameters()),'Every model parameter must remain on the Colab GPU'
            self.hardware={'gpu':torch.cuda.get_device_name(0),'vram_bytes':torch.cuda.get_device_properties(0).total_memory,
                'torch':torch.__version__,'transformers':transformers.__version__,'revision':revision,
                'load_seconds':time.perf_counter()-started,'quantization':'NF4 double quantization FP16 compute',
                'xgrammar':importlib.metadata.version('xgrammar'),'platform':'Google Colab GPU','cpu_offload':False}
            write(self.out/'environment.json',self.hardware)
        else:
            import requests
            env={}
            for line in (Path(__file__).resolve().parents[1]/'.env').read_text(encoding='utf-8-sig').splitlines():
                if '=' in line and not line.lstrip().startswith('#'):
                    k,v=line.split('=',1);env[k.strip()]=v.strip().strip('"').strip("'")
            assert env.get('SMARTCAMPUS_FREE_TIER_VERIFIED')=='1'
            self.key=env.get('GEMINI_API_KEY') or env.get('GOOGLE_API_KEY');assert self.key

    def call(self,ident,kind,pages,instruction,schema,extra='',limit=1800,validator=None):
        import jsonschema
        ids=[p['source_id'] for p in pages];schema=constrained(schema,ids)
        evidence='\n\n'.join('['+p['source_id']+'] ORIGINAL TEXT\n'+p['text'] for p in pages)
        prompt=instruction+'\nORIGINAL SOURCE EVIDENCE\n'+evidence+'\nEND EVIDENCE\n'+extra+'\nJSON SCHEMA\n'+json.dumps(schema)
        identity=digest({'model':self.model_id,'revision':self.protocol.get('local_revision') if self.arm=='qwen' else None,
                         'prompt':prompt,'images':[p['image_sha256'] for p in pages],'schema':schema,'limit':limit,'protocol':self.protocol})
        path=self.out/'records'/(ident+'.json');saved=read(path,{})
        if saved.get('identity')==identity and saved.get('parsed') and (self.arm!='qwen' or
            (saved.get('structured_decoding')=='xgrammar' and saved.get('stop_token_id')==self.processor.tokenizer.eos_token_id)):return saved
        if self.arm!='qwen':
            from provider_availability import current,message
            cooldown=current(self.model_id)
            if cooldown:raise RuntimeError('Provider cooldown: '+message(cooldown))
        previous=[]
        correction=saved.get('error','') if saved.get('identity')==identity and saved.get('status')=='failed' else ''
        for attempt in range(3):
            if self.ledger['attempts']>=self.protocol['max_requests'][self.arm]:raise RuntimeError('Persistent experiment request cap reached')
            if self.arm!='qwen':
                recent=[r['time'] for r in self.ledger['requests'] if time.time()-r['time']<60]
                if len(recent)>=6:time.sleep(max(0,60-(time.time()-recent[0]))+.2)
            self.ledger['attempts']+=1
            self.ledger['requests'].append({'time':time.time(),'id':ident,'identity':identity,'attempt':self.ledger['attempts']})
            write(self.out/'request_ledger.json',self.ledger)
            attempt_prompt=prompt+('\nPREVIOUS ATTEMPT VALIDATION ERROR\n'+correction+'\nCorrect this error. Use only the exact supplied source IDs, including citations inside prose.' if correction else '')
            record={'id':ident,'kind':kind,'model':self.model_id,'identity':identity,'at':datetime.now(timezone.utc).isoformat(),
                'prompt':attempt_prompt,'base_prompt':prompt,'validation_feedback':correction,'schema':schema,'source_ids':ids,'image_manifest':[{'source_id':p['source_id'],'sha256':p['image_sha256']} for p in pages],
                'max_output_tokens':limit,'parsed':None,'status':'attempted','attempt':self.ledger['attempts']}
            started=time.perf_counter()
            try:
                if self.arm=='qwen':
                    import torch
                    import xgrammar as xgr
                    from xgrammar.contrib.hf import LogitsProcessor
                    content=[]
                    for p in pages:content.extend([{'type':'text','text':'Source ['+p['source_id']+']'},
                        {'type':'image','image':str(self.root/p['image'])}])
                    content.append({'type':'text','text':attempt_prompt})
                    messages=[{'role':'system','content':[{'type':'text','text':SYSTEM}]},{'role':'user','content':content}]
                    inputs=self.processor.apply_chat_template(messages,add_generation_prompt=True,tokenize=True,
                        return_dict=True,return_tensors='pt',enable_thinking=False).to('cuda')
                    n=inputs['input_ids'].shape[-1];record['input_tokens']=n
                    if n>14000:raise ValueError('Input admission cap exceeded; no silent truncation')
                    if not hasattr(self,'grammar_compiler'):
                        vocab=self.model.get_output_embeddings().weight.shape[0]
                        info=xgr.TokenizerInfo.from_huggingface(self.processor.tokenizer,vocab_size=vocab)
                        self.grammar_compiler=xgr.GrammarCompiler(info)
                    processors=[LogitsProcessor(self.grammar_compiler.compile_json_schema(json.dumps(schema)))]
                    stop_id=self.processor.tokenizer.eos_token_id
                    assert stop_id is not None,'Explicit chat end token required'
                    record['stop_token_id']=stop_id
                    torch.cuda.reset_peak_memory_stats();torch.cuda.synchronize()
                    with torch.inference_mode():
                        output=self.model.generate(**inputs,max_new_tokens=limit,do_sample=False,use_cache=True,logits_processor=processors,
                            eos_token_id=stop_id,pad_token_id=self.processor.tokenizer.pad_token_id)
                    torch.cuda.synchronize();generated=output[0,n:]
                    raw=self.processor.decode(generated,skip_special_tokens=True)
                    record.update(output_tokens=len(generated),output_limit_reached=len(generated)>=limit,
                        peak_vram_bytes=torch.cuda.max_memory_allocated(),structured_decoding='xgrammar')
                    del inputs,output,generated;gc.collect();torch.cuda.empty_cache()
                else:
                    import requests
                    parts=[{'text':attempt_prompt}]
                    for p in pages:parts.extend([{'text':'Source ['+p['source_id']+']'},
                        {'inlineData':{'mimeType':'image/jpeg','data':base64.b64encode((self.root/p['image']).read_bytes()).decode()}}])
                    settings={'temperature':0,'maxOutputTokens':limit,'responseMimeType':'application/json','responseJsonSchema':schema}
                    response=requests.post('https://generativelanguage.googleapis.com/v1beta/models/'+self.model_id+':generateContent',
                        headers={'x-goog-api-key':self.key},json={'systemInstruction':{'parts':[{'text':SYSTEM}]},
                        'contents':[{'role':'user','parts':parts}],'generationConfig':settings},timeout=(15,180))
                    record['http_status']=response.status_code
                    if response.status_code!=200:raise RuntimeError('Provider HTTP '+str(response.status_code)+': '+str(response.json().get('error',{}).get('message',''))[:160].replace(self.key,'[REDACTED]'))
                    data=response.json();candidate=data['candidates'][0]
                    raw=''.join(x.get('text','') for x in candidate['content']['parts'] if not x.get('thought'))
                    record.update(usage=data.get('usageMetadata'),finish_reason=candidate.get('finishReason'))
                    if candidate.get('finishReason')!='STOP':record['raw']=raw;raise ValueError('Output was not completed')
                record['raw']=raw
                result=normalize_citations(json.loads(raw.strip()))
                jsonschema.validate(result,schema)
                unknown=set(__import__('re').findall(r'[0-9a-f]{12,13}:p\d+',json.dumps(result)))-set(ids)
                if unknown:raise ValueError('Unknown cited source IDs: '+str(unknown))
                if validator:validator(result)
                record.update(parsed=result,status='completed')
            except Exception as exc:
                record.update(status='failed',error=(type(exc).__name__+': '+str(exc))[:500])
                if self.key:record['error']=record['error'].replace(self.key,'[REDACTED]')
            record['seconds']=time.perf_counter()-started
            history=self.out/'attempts'/(ident+'_'+str(record['attempt'])+'.json');write(history,record);write(path,record)
            print(self.arm,ident,record['status'],round(record['seconds'],1),record.get('error','')[:100],flush=True)
            if record.get('parsed'):return record
            previous.append(record)
            if record.get('raw') and not record.get('http_status',200)>=400:
                correction=record.get('error','')
            if record.get('http_status') in [401,403,429]:raise RuntimeError('Provider access/quota stop; checkpoints retained')
            if attempt<2:
                if record.get('output_limit_reached'):limit=min(3200,limit+700)
                if self.arm!='qwen':time.sleep(8*(attempt+1))
        return record

    def run(self):
        corpus=read(self.root/'corpus.json');all_tasks=read(self.root/'tutor_tasks.json')
        for lecture in corpus:
            name=lecture['id'];pages=lecture['pages'];chapters=[];reviews=[]
            for i in range(0,len(pages),4):
                section=pages[i:i+4];ids=[p['source_id'] for p in section];ident=name+'_chapter_'+str(i//4+1).zfill(2)
                def valid(result):
                    if [x['source_id'] for x in result['page_coverage']]!=ids:raise ValueError('Each page must be accounted for once in original order')
                    if not result['body'].strip():raise ValueError('Empty chapter')
                instruction=('Write a comprehensive but summarized study chapter for these source pages. Explain substantive definitions, '
                    'equations with variable meanings and conditions, method steps, diagram meaning and limitations. Include a comparison '
                    'table where source alternatives merit one. Aim 250-450 words for substantive content; much shorter for covers or repeated illustrations. '
                    'No unsupported numbers read from diagrams. Use ASCII math and exact [source_id] citations. Normal angle means angle '
                    'of the normal, not the line. Preserve center-coordinate conventions. Label new examples Additional teaching. '
                    'For title/credits/outline pages, briefly state only their actual information; do not invent teaching of future topics. '
                    'Answer lecture questions when answers follow directly from the supplied equations or figures; label new derivations Additional teaching. '
                    'Keep schema, pointer, coverage-audit and pipeline commentary out of the student-facing body. '
                    'page_coverage must list every supplied source_id in order, exactly once.')
                chapter=self.call(ident,'chapter',section,instruction,CHAPTER,limit=2000,validator=valid)
                if not chapter.get('parsed'):raise RuntimeError('Chapter interface failed; saved partial work: '+ident)
                for revision in range(2):
                    audit=self.call(ident+'_review_'+str(revision),'coverage_review',section,COVERAGE_INSTRUCTION,COVERAGE_REVIEW,
                        extra='CANDIDATE (NOT SOURCE EVIDENCE)\n'+chapter['parsed']['body'],limit=1300)
                    if not audit.get('parsed'):raise RuntimeError('Coverage audit unavailable; checkpoint retained')
                    verdict=coverage_result(audit['parsed'],ids)
                    if verdict['status']=='passed_automated_review' or revision==1:break
                    chapter=self.call(ident+'_repair','chapter_repair',section,instruction+' Correct the following findings while preserving valid content.',
                        CHAPTER,extra='CANDIDATE\n'+json.dumps(chapter['parsed'])+'\nFINDINGS\n'+json.dumps(verdict),limit=2400,validator=valid)
                    if not chapter.get('parsed'):raise RuntimeError('Chapter repair unavailable')
                chapters.append(chapter);reviews.append({'chapter_id':chapter['id'],'review':audit,'result':verdict})
                write(self.out/(name+'_chapter_reviews.json'),reviews)
                body='# '+lecture['name']+'\n\n'+'\n\n'.join('## '+c['parsed']['title']+'\n\n'+c['parsed']['body'] for c in chapters)
                (self.out/(name+'_STUDY_GUIDE.partial.md')).write_text(body,encoding='utf-8')
            questions=[];quiz_reviews=[]
            for part in range(4):
                first=part*len(pages)//4;last=(part+1)*len(pages)//4
                group=pages[first:last]
                # Quiz sees full original text of its quarter, and the same image subset
                # in both arms. Guides have already seen every original image.
                texts='\n\n'.join('['+p['source_id']+']\n'+p['text'] for p in group)
                # Use the central four pages as declared images, retain all quarter text.
                visual=group[max(0,len(group)//2-2):max(0,len(group)//2-2)+4]
                instruction=('Generate exactly two MCQs spanning distinct substantive concepts in the supplied quarter of the lecture. '
                    'Four plausible misconception choices, exactly one defensible answer_index (0-based), source-supported explanation. '
                    'Avoid negative NOT/EXCEPT stems, all/none alternatives, absurd options and questions already asked. '
                    'Cite only IDs in this request image/source set; use those pages for substantive questions. '
                    'Do not infer precise unreadable plot values. Label novel applications Additional teaching.')
                quiz=self.call(name+'_quiz_'+str(part+1),'quiz',visual,instruction,QUIZ,
                    extra='OTHER QUARTER TEXT (background only; cite supplied image IDs)\n'+texts+'\nPREVIOUS STEMS\n'+json.dumps([q['stem'] for q in questions]),limit=1700)
                if not quiz.get('parsed'):raise RuntimeError('Quiz interface failed')
                for j,original in enumerate(quiz['parsed']['questions']):
                    q=original
                    for rev in range(2):
                        evidence=[p for p in pages if p['source_id'] in q['citations']]
                        aid=name+'_quiz_'+str(part+1)+'_item_'+str(j+1)+'_review_'+str(rev)
                        audit=self.call(aid,'quiz_review',evidence,QUIZ_INSTRUCTION,QUIZ_REVIEW,
                            extra='QUESTION (KEY AND RATIONALE HIDDEN)\n'+json.dumps({'stem':q['stem'],'options':q['options']}),limit=800)
                        if not audit.get('parsed'):raise RuntimeError('Quiz audit unavailable')
                        result=quiz_result(audit['parsed'],q,[p['source_id'] for p in evidence])
                        if result['status']=='passed_automated_review' or rev==1:break
                        repaired=self.call(aid+'_repair','quiz_repair',evidence,
                            'Replace this flawed MCQ. Exactly four plausible choices, one defensible supported key and a correct explanation. '
                            'Fix the review issues; return one complete question object.',QUESTION,
                            extra='CANDIDATE\n'+json.dumps(q)+'\nREVIEW\n'+json.dumps(result),limit=1000)
                        if not repaired.get('parsed'):raise RuntimeError('Quiz repair unavailable')
                        q=repaired['parsed']
                    questions.append(q);quiz_reviews.append({'original':original,'final':q,'audit':audit,'result':result})
                    write(self.out/(name+'_quiz_reviews.json'),quiz_reviews)
            write(self.out/(name+'_quiz.json'),{'questions':questions,'review_status':'passed_automated_review' if all(x['result']['status']=='passed_automated_review' for x in quiz_reviews) else 'needs_instructor_review'})
            quiz_text='# '+lecture['name']+' quiz - automated review draft\n\n'
            for n,q in enumerate(questions,1):quiz_text+=str(n)+'. '+q['stem']+'\n\n'+'\n'.join(chr(65+k)+'. '+v for k,v in enumerate(q['options']))+'\n\nKey: '+chr(65+q['answer_index'])+'. '+q['explanation']+'\n\n'
            (self.out/(name+'_QUIZ.md')).write_text(quiz_text,encoding='utf-8')
            (self.out/(name+'_STUDY_GUIDE.md')).write_text(body,encoding='utf-8')
            write(self.out/(name+'_guide.json'),{'chapter_ids':[c['id'] for c in chapters],
                'pages_processed':len(pages),'semantic_review_status':'passed_automated_review' if all(r['result']['status']=='passed_automated_review' for r in reviews) else 'needs_instructor_review',
                'instructor_review':'not_collected','note':'Same-model audit does not establish independent correctness.'})
            for task in [t for t in all_tasks if t['lecture']==name]:
                chosen=[p for p in pages if p['source_id'] in task['source_ids']]
                self.call(task['id'],'tutor',chosen,task['instruction'],task['schema'],limit=1000)
            print('LECTURE FINISHED',self.arm,name,flush=True)
        write(self.out/'completion.json',{'status':'generation_and_automated_review_finished','model':self.model_id,
            'requires_instructor_review':True,'protocol_hash':digest(self.protocol),'at':datetime.now(timezone.utc).isoformat()})
        print('FINISHED IMPROVED',self.arm,flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--arm',choices=['gemini35','qwen'],required=True)
    a=p.parse_args();Runner(a.root,a.arm).run()
