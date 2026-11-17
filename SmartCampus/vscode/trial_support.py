"""Local Streamlit trial utilities; reuse the measured tutor without new defaults."""
import csv
import io
import json
import re
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from lecture_workflow import PROJECT, read
from notebook_presentation import link_sources
from math_rendering import math_markdown

TRIAL_LOCK = threading.RLock()
FEEDBACK_FIELDS = ['created_at', 'trial_code', 'role', 'ease_starting_conversation',
                   'response_relevance_accuracy', 'perceived_resistance_to_hallucination',
                   'overall_recommendation', 'most_useful_features', 'comments']


def source_markdown(text, workflow):
    """Keep formula escaping, replacing filesystem links with page labels for web."""
    # Escape prose without escaping underscores/operators inside LaTeX.
    parts = re.split(r'(\$\$[\s\S]*?\$\$|(?<!\\)\$[^$\n]+\$)', math_markdown(text))
    for i in range(0, len(parts), 2):
        linked = link_sources(parts[i], workflow)
        parts[i] = re.sub(r'\[(p\. \d+)\]\(<[^>]+>\)', r'**[\1]**', linked)
    return ''.join(parts)


def lecture_catalog():
    return read(PROJECT / 'run/exports/sources.json', [])


def lecture_workflow(doc_id):
    from lecture_workflow import LectureWorkflow, write
    config = read(PROJECT / 'full_lecture_config.json')
    if doc_id == config['doc_id']:
        return LectureWorkflow()
    if doc_id not in {d['doc_id'] for d in lecture_catalog()}:
        raise ValueError('Choose an imported lecture.')
    config.update(doc_id=doc_id, job_name='ui_' + doc_id)
    path = PROJECT / 'run/ui_lectures' / doc_id / 'config.json'
    if not path.exists():
        write(path, config)
    return LectureWorkflow(path)


def prepare_lecture(workflow, progress=None):
    if not TRIAL_LOCK.acquire(blocking=False):
        raise RuntimeError('Another lecture request is running. Please try again shortly.')
    descriptor=None
    lock=workflow.root/'generation.lock'
    try:
        try:descriptor=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
        except FileExistsError:
            from lecture_worker import process_alive
            try:owner=int(lock.read_text())
            except (ValueError,OSError):owner=None
            if owner is None or process_alive(owner):
                raise RuntimeError('This lecture is already being processed. Refresh its saved progress shortly.') from None
            lock.unlink(missing_ok=True)
            descriptor=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
        os.write(descriptor,str(os.getpid()).encode())
        workflow.progress_callback=progress
        workflow.state = read(workflow.root / 'progress.json', workflow.state)
        workflow.ns['GENERATIONS'] = read(workflow.root / 'generations.json', [])
        return workflow.process() and workflow.artifacts()
    finally:
        workflow.progress_callback=None
        if descriptor is not None:
            os.close(descriptor)
            lock.unlink(missing_ok=True)
        TRIAL_LOCK.release()


def tutor_error(warnings):
    text = ' '.join(map(str, warnings))
    if 'provider_cooldown:' in text:return text.split('provider_cooldown:',1)[1].strip()
    if any(term in text for term in ['Content review needs','Quiz source review needs','content checks','source review needs']):
        return 'Content checks found a concern. Saved work is intact; inspect the source-review findings before using the material.'
    if 'provider_http_503' in text:
        return 'Gemini is temporarily busy (HTTP 503). No answer was returned. Wait briefly, then retry once.'
    if 'ConnectionError' in text or 'ProxyError' in text:
        return ('Connection error: this app could not establish a connection to Google. '
                'This is different from Gemini reporting high demand. Check the app server’s network access, then retry once.')
    if 'SSLError' in text:
        return 'A secure connection to Google could not be verified. Check the server’s certificate/network configuration.'
    if 'Timeout' in text:
        return 'The request timed out before an answer arrived. Wait briefly, then retry once.'
    if 'provider_http_429' in text:
        return 'Google returned a rate or quota limit (HTTP 429). Wait for the applicable limit to reset before retrying.'
    return 'No answer was returned. ' + (text or 'Please try again later.')


def source_ids(text, workflow):
    return list(dict.fromkeys(s for s in re.findall(r'[0-9a-f]{12}:p\d+', text)
                              if s in workflow.ns['PAGES']))


def request_tutor(workflow, question, mode, session):
    from lecture_worker import worker_status
    if worker_status(workflow.root).get('state')=='running':
        raise RuntimeError('Your study materials are being prepared. Ask the tutor when processing finishes.')
    # Sessions keep separate conversation histories. Serialize persistent job writes.
    if not TRIAL_LOCK.acquire(blocking=False):
        raise RuntimeError('Another question is being answered. Please try again shortly.')
    try:
        workflow.state = read(workflow.root / 'progress.json', workflow.state)
        workflow.ns['GENERATIONS'] = read(workflow.root / 'generations.json', [])
        workflow.update_notes()
        return workflow.tutor(question, mode, session=session)
    finally:
        TRIAL_LOCK.release()


def log_event(workflow, event):
    destination = workflow.root / 'trial_ui'
    destination.mkdir(exist_ok=True)
    event = {'created_at': datetime.now(timezone.utc).isoformat(), **event}
    with TRIAL_LOCK, (destination / 'events.jsonl').open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(event, ensure_ascii=False) + '\n')


def save_feedback(workflow, values):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', values.get('trial_code', '')):
        raise ValueError('Use an anonymous code of 1–40 letters, numbers, underscores or hyphens.')
    if not any(values.get(k) for k in FEEDBACK_FIELDS[3:]):
        raise ValueError('Add at least one rating or comment before saving.')
    row = {k: values.get(k, '') for k in FEEDBACK_FIELDS}
    row['created_at'] = datetime.now(timezone.utc).isoformat()
    destination = workflow.root / 'trial_ui'
    destination.mkdir(exist_ok=True)
    with TRIAL_LOCK:
        path = destination / 'feedback.csv'
        exists = path.exists() and path.stat().st_size > 0
        with path.open('a', encoding='utf-8', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=FEEDBACK_FIELDS)
            if not exists:
                writer.writeheader()
            writer.writerow(row)
    return row


def csv_download(path):
    """Neutralize spreadsheet formula interpretation only in the download copy."""
    with path.open(encoding='utf-8', newline='') as stream:
        rows = list(csv.reader(stream))
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    for row in rows:
        writer.writerow(["'" + v if v.lstrip().startswith(('=', '+', '-', '@')) else v
                         for v in row])
    return output.getvalue().encode('utf-8-sig')
