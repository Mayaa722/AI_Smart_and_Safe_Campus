"""Build traceable RAGAS samples from accepted notebook results; no API requests."""
import hashlib
import json
import re
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def prepare():
    records = json.loads((PROJECT / 'run/exports/generations.json').read_text(encoding='utf-8'))
    docs = json.loads((PROJECT / 'run/exports/sources.json').read_text(encoding='utf-8'))
    pages = {p['source_id']: p for d in docs for p in d['pages']}
    references = json.loads((HERE / 'references.json').read_text(encoding='utf-8'))
    samples = []
    for ref in references:
        candidates = [r for r in records if r['id'] == ref['generation_id'] and r['task'] == ref['task'] and r.get('parsed')
                      and r['status'] in {'live', 'cached_live'}]
        if not candidates:
            continue
        record = candidates[-1]
        parsed = record['parsed']
        if 'item_index' in ref:
            question = parsed['questions'][ref['item_index']]
            user_input = question['stem']
            response = question['options'][question['answer_index']] + '. ' + question['explanation']
            adaptation = 'MCQ converted to question + generated keyed answer/explanation; not a measure of distractor quality'
        else:
            user_input = record.get('student_question')
            if not user_input:
                match = re.search(r'\nINSTRUCTION: (.*?)\nHISTORY', record['prompt'], re.S)
                if not match:
                    raise ValueError('Cannot recover the actual generation instruction.')
                user_input = match.group(1)
            response = parsed['answer']
            adaptation = 'Saved answer field, without JSON formatting or the separate next-question field'
        ids = record['source_ids']
        sample = {
            'id': ref['id'], 'user_input': user_input, 'response': response,
            'retrieved_contexts': [pages[s]['text'] for s in ids],
            'reference': ref['reference'], 'reference_source_ids': ref['reference_source_ids'],
            'source_ids': ids, 'generation_id': record['id'], 'generation_output_hash': record['output_hash'],
            'context_hashes': [hashlib.sha256(pages[s]['text'].encode()).hexdigest() for s in ids],
            'task': record['task'], 'item_index': ref.get('item_index'), 'split': 'development',
            'context_selection': 'manual_pages', 'context_track': 'raw_extracted_text',
            'generation_status': record['status'], 'generator_model': record['model'],
            'reference_provenance': 'Agent-authored from lecture before judging; instructor review pending',
            'image_required': ref['image_required'], 'adaptation': adaptation,
            'limitations': ['Existing saved pilot used manually selected pages; does not benchmark automatic retrieval.',
                'Same-model judge is not an independent human assessment.'] + (
                ['Relevant formulas/geometry are in the source image and absent from extracted text. Text-only context metrics require source transcription or a separately labelled multimodal evaluation.']
                if ref['image_required'] else [])}
        sample['sample_hash'] = fingerprint(sample)
        samples.append(sample)
    (HERE / 'dataset.json').write_text(json.dumps(samples, indent=2, ensure_ascii=False), encoding='utf-8')
    print(f'Prepared {len(samples)} development samples from saved Gemini outputs; no API requests.')
    return samples


if __name__ == '__main__':
    prepare()
