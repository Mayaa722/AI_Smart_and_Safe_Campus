"""Budgeted Gemini transport for official RAGAS metric classes.

Implements RAGAS' modern structured-LLM/embedding interfaces. Metric prompts and
scoring algorithms are supplied unchanged by RAGAS, not reimplemented here.
"""
import asyncio
import hashlib
import json
import os
import threading
import time
from pathlib import Path

os.environ['RAGAS_DO_NOT_TRACK'] = 'true'
os.environ['LANGSMITH_TRACING'] = 'false'

import requests
from ragas.embeddings.base import BaseRagasEmbedding
from ragas.llms.base import InstructorBaseRagasLLM

PROJECT = Path(__file__).resolve().parents[1]
RUNTIME = PROJECT / 'run/evaluation'
API_ROOT = 'https://generativelanguage.googleapis.com/v1beta/models/'


def read_json(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temp.replace(path)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def load_key():
    for line in (PROJECT / '.env').read_text(encoding='utf-8-sig').splitlines():
        if '=' not in line or line.lstrip().startswith('#'):
            continue
        name, value = line.strip().split('=', 1)
        if name in {'GEMINI_API_KEY', 'GOOGLE_API_KEY', 'SMARTCAMPUS_FREE_TIER_VERIFIED'} and value.strip():
            os.environ[name] = value.strip().strip('"').strip("'")
    if os.environ.get('SMARTCAMPUS_FREE_TIER_VERIFIED') != '1':
        raise ValueError('The existing free-tier verification setting is missing.')
    key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
    if not key:
        raise ValueError('No local Gemini key is configured.')
    return key


class ProviderFailure(RuntimeError):
    def __init__(self, code):
        self.code = code
        explanation = {429: 'rate or quota limit', 503: 'service unavailable',
                       500: 'provider internal error', 400: 'request rejected',
                       403: 'access denied', 404: 'model or endpoint unavailable'}.get(code, 'provider error')
        super().__init__(f'Gemini HTTP {code}: {explanation}; no automatic retry.')


class Transport:
    def __init__(self, config):
        self.config = config
        self.key = load_key()
        self.lock = threading.Lock()
        self.occurrences = {}
        self.trace = []
        self.active_metric = None
        self.active_sample = None

    def post(self, model, method, payload, replicate=False):
        serialized = json.dumps(payload, ensure_ascii=False)
        if len(serialized) > self.config['max_prompt_chars']:
            raise ValueError('Evaluation prompt exceeds the configured character limit.')
        base_key = fingerprint({'model': model, 'method': method, 'payload': payload})
        with self.lock:
            occurrence = self.occurrences.get(base_key, 0)
            self.occurrences[base_key] = occurrence + 1
            cache_key = fingerprint([base_key, occurrence if replicate else 0])
            cache = RUNTIME / 'cache' / (cache_key + '.json')
            trace = {'sample': self.active_sample, 'metric': self.active_metric,
                     'request_hash': cache_key, 'model': model, 'method': method,
                     'response_schema_field':next((k for k in ['responseSchema','responseJsonSchema'] if k in payload.get('generationConfig',{})),None)}
            if cache.exists():
                data = read_json(cache, {})
                self.trace.append({**trace, 'status': 'cached', 'usage': data.get('usageMetadata')})
                return data
            from provider_availability import current,message,remember
            cooldown=current(model)
            if cooldown:raise RuntimeError('Provider cooldown: '+message(cooldown))
            ledger_path = RUNTIME / 'request_ledger.json'
            ledger = read_json(ledger_path, {'attempts': 0, 'reserved_tokens': 0,
                                           'generation_timestamps': [], 'requests': []})
            reserve = len(serialized) + (self.config['max_output_tokens'] if method == 'generateContent' else 0)
            if ledger['attempts'] >= self.config['max_network_requests']:
                raise ValueError('Persistent evaluation request budget exhausted; generation ledger is unchanged.')
            if ledger['reserved_tokens'] + reserve > self.config['max_reserved_tokens']:
                raise ValueError('Persistent evaluation reservation budget exhausted.')
            now = time.time()
            recent = [t for t in ledger['generation_timestamps'] if now - t < 60]
            if method == 'generateContent' and len(recent) >= self.config['max_generation_requests_per_minute']:
                delay = max(0, 60 - (now - recent[0])) + 0.3
                print(f'  Pacing judge calls: waiting {delay:.0f}s', flush=True)
                time.sleep(delay)
                now = time.time()
            ledger['attempts'] += 1
            ledger['reserved_tokens'] += reserve
            if method == 'generateContent':
                ledger['generation_timestamps'].append(now)
            entry = {**trace, 'attempt': ledger['attempts'], 'timestamp': now,
                     'reserved_tokens': reserve, 'status': 'attempted'}
            ledger['requests'].append(entry)
            write_json(ledger_path, ledger)
            started = time.perf_counter()
            try:
                # The key is a header, never a URL, log field, notebook value or exported setting.
                response = requests.post(API_ROOT + model + ':' + method,
                    headers={'x-goog-api-key': self.key}, json=payload, timeout=120)
            except requests.RequestException:
                entry.update(status='network_error', seconds=time.perf_counter() - started)
                write_json(ledger_path, ledger)
                self.trace.append(dict(entry))
                raise RuntimeError('Evaluation network request failed; no automatic retry.') from None
            entry['seconds'] = time.perf_counter() - started
            if response.status_code != 200:
                entry.update(status='provider_error', http_status=response.status_code)
                try:
                    error = response.json().get('error', {})
                    entry['provider_status'] = error.get('status')
                    entry['provider_message'] = str(error.get('message', '')).replace(self.key, '[REDACTED]')[:1600]
                    # Preserve quota scope without keys or arbitrary response contents.
                    entry['quota_details'] = [
                        {k:v for k,v in detail.items() if k in {'@type','violations','retryDelay'}}
                        for detail in error.get('details', [])
                        if isinstance(detail, dict) and any(k in detail for k in ['violations','retryDelay'])]
                    if response.status_code==429:remember(model,error)
                except (ValueError, AttributeError):
                    pass
                write_json(ledger_path, ledger)
                self.trace.append(dict(entry))
                raise ProviderFailure(response.status_code)
            data = response.json()
            entry.update(status='completed', usage=data.get('usageMetadata'), model_version=data.get('modelVersion'))
            write_json(ledger_path, ledger)
            self.trace.append(dict(entry))
            write_json(cache, data)
            return data


class GeminiJudge(InstructorBaseRagasLLM):
    def __init__(self, transport):
        self.transport = transport

    def generate(self, prompt, response_model):
        cfg = self.transport.config
        schema = response_model.model_json_schema()
        mode = cfg.get('schema_mode', 'json_schema')
        schema_field = 'responseJsonSchema'
        if mode == 'google_native':
            definitions = schema.get('$defs', {})
            supported = {'type','description','enum','properties','required','items','nullable','minItems','maxItems','minimum','maximum','format'}
            def convert(node):
                if isinstance(node, list):
                    return [convert(value) for value in node]
                if not isinstance(node, dict):
                    return node
                if '$ref' in node:
                    name = node['$ref'].removeprefix('#/$defs/')
                    if name not in definitions:
                        raise ValueError('Unsupported structured-output schema reference.')
                    return convert(definitions[name])
                result = {}
                for key,value in node.items():
                    if key not in supported:
                        continue
                    result[key] = {name:convert(child) for name,child in value.items()} if key=='properties' else convert(value)
                return result
            schema = convert(schema)
            schema_field = 'responseSchema'
        elif mode != 'json_schema':
            raise ValueError('Unknown judge schema mode.')
        payload = {'contents': [{'role': 'user', 'parts': [{'text': prompt}]}],
            'generationConfig': {'temperature': cfg['judge_temperature'],
                'maxOutputTokens': cfg['max_output_tokens'], 'responseMimeType': 'application/json',
                schema_field: schema}}
        if 'thinking_budget' in cfg:
            payload['generationConfig']['thinkingConfig'] = {'thinkingBudget':cfg['thinking_budget']}
        try:
            data = self.transport.post(cfg['judge_model'], 'generateContent', payload, replicate=True)
        except ProviderFailure as exc:
            if exc.code!=503 or schema_field!='responseSchema' or not getattr(self.transport,'schema_503_fallback',False):
                raise
            # Opt-in recovery after observed native-schema 503s. Keep the judge,
            # RAGAS prompt and Pydantic response model; retain each real request
            # under its own payload hash instead of aliasing the cache.
            alternate={**payload,'generationConfig':{k:v for k,v in payload['generationConfig'].items() if k!='responseSchema'}}
            alternate['generationConfig']['responseJsonSchema']=response_model.model_json_schema()
            self.transport.trace.append({'status':'schema_transport_recovery','sample':self.transport.active_sample,
                'metric':self.transport.active_metric,'from':'responseSchema','to':'responseJsonSchema'})
            data=self.transport.post(cfg['judge_model'],'generateContent',alternate,replicate=True)
        candidates = data.get('candidates') or []
        if not candidates or candidates[0].get('finishReason') != 'STOP':
            raise ValueError('Judge output incomplete or blocked; score remains missing.')
        text = ''.join(p.get('text', '') for p in candidates[0].get('content', {}).get('parts', [])
                       if not p.get('thought'))
        return response_model.model_validate_json(text)

    async def agenerate(self, prompt, response_model):
        return await asyncio.to_thread(self.generate, prompt, response_model)


class GeminiEmbeddings(BaseRagasEmbedding):
    def __init__(self, transport):
        super().__init__()
        self.transport = transport

    def embed_text(self, text, **kwargs):
        return self.embed_texts([text])[0]

    def embed_texts(self, texts, **kwargs):
        model = self.transport.config['embedding_model']
        data = self.transport.post(model, 'batchEmbedContents', {'requests': [
            {'model': 'models/' + model, 'content': {'parts': [{'text': text}]},
             'taskType': 'SEMANTIC_SIMILARITY'} for text in texts]})
        vectors = [row['values'] for row in data.get('embeddings', [])]
        if len(vectors) != len(texts) or not all(vectors):
            raise ValueError('Embedding response missing vectors; relevancy remains missing.')
        return vectors

    async def aembed_text(self, text, **kwargs):
        return await asyncio.to_thread(self.embed_text, text, **kwargs)

    async def aembed_texts(self, texts, **kwargs):
        return await asyncio.to_thread(self.embed_texts, texts, **kwargs)
