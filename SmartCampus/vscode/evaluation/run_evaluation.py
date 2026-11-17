"""Evaluate saved Gemini responses with the four paper-aligned RAGAS metrics.

Default invocation is a zero-request plan. --run explicitly starts the judge.
"""
import argparse
import asyncio
import csv
import importlib.metadata
import io
import json
import math
import os
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

os.environ['RAGAS_DO_NOT_TRACK'] = 'true'
os.environ['LANGSMITH_TRACING'] = 'false'

from prepare_dataset import prepare
from providers import (GeminiEmbeddings, GeminiJudge, ProviderFailure, RUNTIME,
                       Transport, fingerprint, read_json, write_json)
from ragas.metrics.collections import AnswerRelevancy, ContextPrecision, ContextRecall, Faithfulness

HERE = Path(__file__).resolve().parent
RESULTS = HERE / 'results'
NAMES = ['faithfulness', 'context_precision', 'context_recall', 'answer_relevancy']


async def score_with_recovery(metric, args, transport, config):
    """Retry only a transient failed metric; retain its accepted subrequests."""
    occurrences=dict(transport.occurrences)
    limit=config.get('max_transient_retries',0)
    for attempt in range(limit+1):
        try:
            return await metric.ascore(**args)
        except ProviderFailure as exc:
            if exc.code not in {500,502,503,504} or attempt>=limit:
                raise
            delay=config.get('retry_delay_seconds',35)
            print(f'  Provider HTTP {exc.code}; logged metric retry {attempt+1}/{limit} after {delay}s. Completed subrequests remain cached.',flush=True)
            transport.occurrences=dict(occurrences)
            await asyncio.sleep(delay)


def export_report(report):
    results = RESULTS
    results.mkdir(exist_ok=True)
    grouped = {}
    for row in report['scores']:
        group = grouped.setdefault(row['scope'], {name: [] for name in NAMES})
        if row['status'] in {'completed', 'cached'}:
            group[row['metric']].append(row['value'])
    report['summary_by_scope'] = {scope: {name: {'mean': statistics.mean(values) if values else None,
        'n_scored': len(values)} for name, values in metrics.items()} for scope, metrics in grouped.items()}
    write_json(results / 'latest.json', report)
    buffer = io.StringIO(newline='')
    columns = ['sample_id', 'metric', 'value', 'status', 'scope', 'generation_id', 'reason']
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction='ignore', lineterminator='\n')
    writer.writeheader()
    writer.writerows(report['scores'])
    (results / 'scores.csv').write_text(buffer.getvalue(), encoding='utf-8-sig')
    lines = ['# Paper-aligned RAGAS evaluation', '', f"Run: {report['created_at']}", '',
        'These are development pilot results. They are not the paper\'s scores or a held-out benchmark.', '',
        '| Sample | Metric | Score | Status |', '|---|---|---:|---|']
    for row in report['scores']:
        value = f"{row['value']:.4f}" if row['value'] is not None else 'Not evaluated'
        lines.append(f"| {row['sample_id']} | {row['metric']} | {value} | {row['status']} |")
    lines += ['', '## Interpretation', '',
        '- Scores use the official RAGAS classes; context precision uses a reference answer.',
        '- Contexts are the exact original supplied pages, in original order. No oracle context substitution.',
        '- Context-selection method and source provenance are recorded per sample. Manual-page cases do not establish automatic retrieval performance.',
        '- MCQ rows, when present, score the keyed answer and explanation only; distractor quality, ambiguity and calibrated difficulty are not measured.',
        '- References were authored from the lecture; instructor validation is pending. All current cases are development cases.',
        '- Formula/diagram cases need source transcription or a separately labelled multimodal evaluation before text-only context scores can be interpreted.',
        '- Hint/Socratic responses may have lower answer relevancy by design. Report modes separately.',
        '- Gemini is both the generator family and judge; correlated errors and judge variability remain possible.',
        '- Human feedback is a separate study. No participant ratings have been invented.', '',
        '## Runtime', '', f"Judge: {report['config']['judge_model']}; embeddings: {report['config']['embedding_model']}; RAGAS: {report['versions']['ragas']}.",
        'A separate persistent evaluation budget is used; the tutor generation ledger is preserved.',
        'See latest.json for exact samples, provenance, request traces and per-metric denominators.']
    (results / 'REPORT.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


async def evaluate(samples, config, requested_metrics, allow_visual=False):
    transport = Transport(config)
    judge = GeminiJudge(transport)
    embeddings = GeminiEmbeddings(transport)
    metrics = {'faithfulness': Faithfulness(llm=judge), 'context_precision': ContextPrecision(llm=judge),
        'context_recall': ContextRecall(llm=judge),
        'answer_relevancy': AnswerRelevancy(llm=judge, embeddings=embeddings,
                                         strictness=config['answer_relevancy_strictness'])}
    report = {'created_at': datetime.now(timezone.utc).isoformat(), 'status': 'running',
        'paper': 'https://ijcai.org/proceedings/2026/984', 'protocol': 'paper_metrics_v1',
        'config': config, 'versions': {p: importlib.metadata.version(p) for p in ['ragas', 'requests', 'pydantic', 'numpy']},
        'samples': samples, 'scores': [], 'requests_this_run': transport.trace}
    deferred = None
    for sample in samples:
        transport.active_sample = sample['id']
        for name in requested_metrics:
            transport.active_metric = name
            row = {'sample_id': sample['id'], 'metric': name, 'value': None, 'status': 'not_evaluated',
                'generation_id': sample['generation_id'], 'scope': '/'.join([
                    sample['split'], sample['context_selection'], sample['context_track'], sample['task']]), 'reason': None}
            print(f"{sample['id']} / {name}", flush=True)
            if sample['image_required'] and name != 'answer_relevancy' and not allow_visual:
                row.update(status='needs_visual_context', reason='Relevant evidence is absent from extracted text; no judge request made.')
            elif deferred:
                row.update(status='deferred', reason=deferred)
            else:
                cache_key = fingerprint({'protocol': report['protocol'], 'sample': sample, 'metric': name,
                    'ragas': report['versions']['ragas'], 'judge_model': config['judge_model'],
                    'embedding_model': config['embedding_model'], 'temperature': config['judge_temperature'],
                    'schema_mode': config.get('schema_mode','json_schema'),
                    'thinking_budget': config.get('thinking_budget'),
                    'max_output_tokens': config['max_output_tokens'], 'strictness': config['answer_relevancy_strictness']})
                cache_path = RUNTIME / 'metric_cache' / (cache_key + '.json')
                cached = read_json(cache_path, None)
                if cached is None:
                    for directory in config.get('read_metric_cache_dirs',[]):
                        shared_path = Path(directory) / (cache_key + '.json')
                        cached = read_json(shared_path, None)
                        if cached is not None:
                            row['cache_source'] = str(shared_path)
                            break
                if cached is not None:
                    row.update(value=cached['value'], status='cached')
                else:
                    args = {k: sample[k] for k in {
                        'faithfulness': ['user_input', 'response', 'retrieved_contexts'],
                        'context_precision': ['user_input', 'retrieved_contexts', 'reference'],
                        'context_recall': ['user_input', 'retrieved_contexts', 'reference'],
                        'answer_relevancy': ['user_input', 'response']}[name]}
                    try:
                        result = await score_with_recovery(metrics[name],args,transport,config)
                        value = float(result.value)
                        if not math.isfinite(value):
                            raise ValueError('RAGAS returned a non-finite score; preserve it as missing.')
                        if name != 'answer_relevancy' and not 0 <= value <= 1:
                            raise ValueError('RAGAS score outside its documented range.')
                        row.update(value=value, status='completed')
                        write_json(cache_path, {'value': value, 'metric': name, 'sample_hash': sample['sample_hash'],
                                               'created_at': report['created_at']})
                    except Exception as exc:
                        reason = str(exc).replace(transport.key, '[REDACTED]')[:300]
                        row.update(status='error', reason=reason)
                        if (isinstance(exc, ProviderFailure) and exc.code in {401,403,404,429}) or 'budget exhausted' in reason:
                            deferred = reason
            if sample['image_required'] and allow_visual and name != 'answer_relevancy':
                row['scope'] += '/visual_evidence_missing'
                row['reason'] = 'Explicit text-only diagnostic; relevant image evidence missing. Do not report as multimodal faithfulness.'
            report['scores'].append(row)
            report['evaluation_ledger'] = read_json(RUNTIME / 'request_ledger.json', {})
            export_report(report)
            print(f"  {row['status']}: {row['value'] if row['value'] is not None else row['reason']}", flush=True)
    report['status'] = 'completed' if all(r['status'] in {'completed', 'cached'} for r in report['scores']) else 'partial'
    export_report(report)
    write_json(RESULTS / (report['created_at'].replace(':', '-').replace('+', '_') + '.json'), report)
    return report


def main():
    global RUNTIME, RESULTS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='Explicitly enable budgeted Gemini judge/embedding calls.')
    parser.add_argument('--ids', help='Comma-separated development case IDs; default is one text-supported pilot.')
    parser.add_argument('--dataset', type=Path, help='JSON samples captured from another actual run; no context reconstruction.')
    parser.add_argument('--config', type=Path, default=HERE / 'config.json')
    parser.add_argument('--runtime-dir', type=Path, help='Explicit named-run budget/cache directory; existing ledgers are not reset.')
    parser.add_argument('--output-dir', type=Path, help='Write a distinct report for a new experiment, preserving earlier results.')
    parser.add_argument('--metrics', help='Comma-separated metric names; default is all four.')
    parser.add_argument('--allow-visual-text-bias', action='store_true', help='Diagnostic only: score text even when image evidence is missing.')
    args = parser.parse_args()
    config = read_json(args.config, {})
    if args.runtime_dir:
        import providers
        RUNTIME = args.runtime_dir.resolve()
        providers.RUNTIME = RUNTIME
    if args.output_dir:
        RESULTS = args.output_dir.resolve()
    if importlib.metadata.version('ragas') != config['ragas_version']:
        raise ValueError('Install the pinned RAGAS version before evaluation.')
    samples = read_json(args.dataset, None) if args.dataset else prepare()
    if not isinstance(samples, list) or not samples:
        raise ValueError('Dataset must be a nonempty JSON list of captured samples.')
    sample_ids = []
    for sample in samples:
        for field in ['id', 'user_input', 'response', 'reference', 'generation_id', 'split',
                      'context_selection', 'context_track', 'task']:
            if not isinstance(sample.get(field), str) or not sample[field].strip():
                raise ValueError('Each captured sample needs a nonempty ' + field)
        contexts = sample.get('retrieved_contexts')
        if not isinstance(contexts, list) or not contexts or any(not isinstance(c, str) or not c.strip() for c in contexts):
            raise ValueError('Supply actual nonempty contexts, preserving the generation-time rank order.')
        if type(sample.get('image_required')) is not bool:
            raise ValueError('Declare image_required explicitly for every sample.')
        sample_ids.append(sample['id'])
        sample.pop('sample_hash', None)
        sample['sample_hash'] = fingerprint(sample)
    if len(set(sample_ids)) != len(sample_ids):
        raise ValueError('Dataset contains duplicate sample IDs.')
    ids = args.ids.split(',') if args.ids else (sample_ids if args.dataset else config['default_sample_ids'])
    if len(set(ids)) != len(ids):
        raise ValueError('Choose distinct sample IDs; repeated cases would inflate the denominator.')
    unknown = set(ids) - {s['id'] for s in samples}
    if unknown:
        raise ValueError('Unknown case IDs: ' + ', '.join(sorted(unknown)))
    selected = [s for case_id in ids for s in samples if s['id'] == case_id]
    metrics = args.metrics.split(',') if args.metrics else config['metrics']
    if not metrics or set(metrics) - set(NAMES) or len(set(metrics)) != len(metrics):
        raise ValueError('Choose distinct metrics from: ' + ', '.join(NAMES))
    if not args.run:
        print(json.dumps({'mode': 'plan_only_no_requests', 'samples': ids, 'metrics': metrics, 'config': config,
            'note': 'Use --run to evaluate. Each sample can require multiple judge and embedding requests; caches are reused.'}, indent=2))
        return
    RUNTIME.mkdir(exist_ok=True, parents=True)
    lock = RUNTIME / 'evaluation.lock'
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError('Another evaluator holds the lock. If it crashed, check for a running evaluator before removing evaluation.lock.') from None
    try:
        os.write(descriptor, str(os.getpid()).encode())
        report = asyncio.run(evaluate(selected, config, metrics, args.allow_visual_text_bias))
        print('Report:', RESULTS / 'REPORT.md', flush=True)
        return 0 if report['status'] == 'completed' else 2
    finally:
        os.close(descriptor)
        lock.unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, RuntimeError) as exc:
        print('Evaluation stopped:', str(exc), file=sys.stderr)
        sys.exit(2)
