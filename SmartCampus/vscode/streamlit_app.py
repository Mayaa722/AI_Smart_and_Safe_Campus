"""Run with tools/start_trial.ps1. No LLM requests occur merely by opening the app."""
import json
import hashlib
import uuid

import pandas as pd
import streamlit as st

from lecture_workflow import PROJECT, read, render_guide
from notebook_presentation import grade_quiz
from trial_support import (csv_download, log_event, request_tutor, save_feedback,
                           source_ids, source_markdown,
                           prepare_lecture, tutor_error)
from math_rendering import math_markdown
from tutor_rendering import tutor_text
from lecture_upload import ingest_upload
from lecture_worker import start_worker, worker_status
from lecture_quality import quiz_for_practice,fingerprint,quiz_editorial_findings,external_quiz_findings,external_content_findings
from provider_availability import current as provider_cooldown,message as cooldown_message

st.set_page_config(page_title='SmartCampus · Learning trial', page_icon='🎓', layout='wide')
st.markdown('''<style>
.block-container {max-width: 1250px; padding-top: 2rem;}
[data-testid="stMetric"] {background: #f2f5fa; padding: 1rem; border-radius: 12px;}
div[data-testid="stForm"] {border-radius: 14px;}
</style>''', unsafe_allow_html=True)

if 'trial_id' not in st.session_state:
    st.session_state.trial_id = uuid.uuid4().hex
with st.sidebar:
    st.title('🎓 SmartCampus')
    st.caption('YOUR LECTURE WORKSPACE')
    uploaded = st.file_uploader('Upload a lecture', type=['pdf','pptx'], accept_multiple_files=False,
                               help='One PDF or PowerPoint lecture, up to 100 MB and 120 pages/slides.')
    st.caption('Upload another file to start studying different material. Each workspace uses only its active lecture.')
if uploaded is None:
    st.session_state.pop('workflow', None)
    st.session_state.pop('active_upload_hash', None)
    st.session_state.pop('active_study', None)
    st.title('What would you like to study?')
    st.write('Upload one lecture to create a comprehensive study guide, practise with a quiz, and ask a tutor grounded in that material.')
    st.info('PDF and PPTX supported. Uploading reads the file locally; creating study materials or asking the tutor sends relevant content to Gemini.')
    st.stop()
file_hash = hashlib.sha256(uploaded.getvalue()).hexdigest()
if st.session_state.get('active_upload_hash') != file_hash:
    try:
        with st.spinner('Reading your lecture and preparing its original page images…'):
            candidate = ingest_upload(uploaded.name, uploaded.getvalue(), st.session_state.trial_id)
        st.session_state.workflow = candidate
        st.session_state.active_upload_hash = file_hash
        st.session_state.active_study = {'messages':[], 'quiz_result':None,
                                         'conversation_id':uuid.uuid4().hex}
    except Exception as error:
        st.error(str(error) if isinstance(error,ValueError) else
                 'This lecture could not be imported. For PowerPoint, try exporting to PDF and uploading that copy.')
        st.stop()
workflow = st.session_state.workflow
if not workflow.config.get('quality_policy'):
    st.info('A new edition adds source, quiz-answer and mathematical checks while keeping your saved materials.')
    if st.button('Prepare an edition with content checks'):
        st.session_state.workflow=ingest_upload(uploaded.name,uploaded.getvalue(),st.session_state.trial_id)
        st.session_state.active_study={'messages':[],'quiz_result':None,'conversation_id':uuid.uuid4().hex}
        st.rerun()
workflow.state=read(workflow.root/'progress.json',workflow.state)
workflow.update_notes()
lecture_state = st.session_state.active_study
prefix = workflow.config['job_name'] + '_'
session = 'upload_chat_' + lecture_state['conversation_id']
approval = read(workflow.root / 'instructor_approval.json', {})
cooldown=provider_cooldown(workflow.config.get('model','gemini-3.5-flash-lite'))
editorial_flags=any(quiz_editorial_findings(q) for q in workflow.state['artifacts'].get('lecture_quiz',{}).get('parsed',{}).get('questions',[]))



def show_source_picker(ids, key):
    ids = list(dict.fromkeys(s for s in ids if s in workflow.ns['PAGES']))
    if not ids:
        return
    with st.expander('View original source pages'):
        sid = st.selectbox('Choose cited page', ids, key=prefix+key,
                          format_func=lambda s, pages=workflow.ns['PAGES']: f"Page {pages[s]['page']}")
        page = workflow.ns['PAGES'][sid]
        st.image(page['image'], caption=f"Original lecture · page {page['page']}", width='stretch')


def create_materials():
    try:
        start_worker(workflow)
        st.rerun()
    except (RuntimeError,ValueError) as error:
        st.error(str(error))


@st.fragment(run_every=4)
def processing_progress():
    status=worker_status(workflow.root)
    if not status:return
    progress=read(workflow.root/'progress.json',{})
    done=progress.get('coverage',{}).get('processed_pages',0)
    chapters=sum(bool(c.get('parsed')) for c in progress.get('guide_chapters',{}).values())
    if status.get('state')=='running':
        st.info(status.get('message','Processing lecture…'))
        st.progress(done/max(1,workflow.doc['page_count']),text=f'{done}/{workflow.doc["page_count"]} pages saved · {chapters} chapters saved')
        st.caption('Processing continues in the background if you refresh or change tabs. Temporary failures retry automatically within the saved request budget.')
    elif st.session_state.get(prefix+'worker_update')!=status.get('updated_at'):
        st.session_state[prefix+'worker_update']=status.get('updated_at')
        st.rerun()
    elif status.get('state') in ['paused','review_required']:st.warning(status.get('message'))


with st.sidebar:
    st.caption(workflow.doc['name'])
    st.write(f"{workflow.doc['page_count']} pages · {workflow.doc['type'].upper()}")
    if workflow.doc.get('external_references'):
        st.caption('This lecture contains external links. Linked videos and web pages are not included in the study materials; only the uploaded pages are processed.')
    if approval:
        st.success('Guide and quiz reviewed by you')
    elif not workflow.state['artifacts']:
        st.info('Lecture loaded · study materials not complete yet')
    else:
        st.info('Generated materials · awaiting content review')
    budget_display = st.empty()
    if cooldown:st.warning(cooldown_message(cooldown))
    st.caption('This is the local tutor allowance, separate from Google’s quota. Opening saved materials and grading ready quizzes use no API calls.')
    if st.button('Start a new conversation', width='stretch'):
        workflow.ns['HISTORY'].pop((session, workflow.doc['doc_id']), None)
        lecture_state['messages'] = []
        lecture_state.pop('pending_retry', None)
        lecture_state.pop('last_error', None)
        st.rerun()
    st.caption('One active lecture at a time. Uploading different material starts a fresh conversation and quiz attempt.')

st.title('Your lecture, ready to explore')
st.write('Study the complete guide, practise with your quiz, and ask questions about this uploaded lecture.')
summary_tab, guide_tab, source_tab, quiz_tab, tutor_tab, results_tab = st.tabs(
    ['Lecture summary', 'Study guide', 'Lecture & sources', 'Quiz', 'AI tutor', 'Results & feedback'])

with summary_tab:
    st.subheader('Lecture at a glance')
    st.caption('A short overview for quick revision. Open Study guide for detailed explanations and worked examples.')
    summary=workflow.state['artifacts'].get('lecture_summary',{})
    if summary.get('parsed'):
        st.markdown(source_markdown(summary['parsed']['body'],workflow))
        for uncertainty in summary['parsed'].get('uncertainties',[]):st.warning(uncertainty)
        st.caption('Generated from the saved visual page notes. Instructor review remains a separate step.')
        show_source_picker(summary.get('source_ids',[]),'summary_source')
        st.download_button('Download lecture summary',summary['parsed']['body'],'LECTURE_SUMMARY.md','text/markdown')
    elif not workflow.coverage()['processing_complete']:
        st.info('Prepare the lecture in Study guide first. The summary uses notes from every page.')
    else:
        st.info('Your lecture is processed. Create its short summary once; reopening it uses no API requests.')
        if st.button('Create / resume lecture summary',key=prefix+'summary',disabled=bool(cooldown) or worker_status(workflow.root).get('state')=='running'):
            from lecture_summary import build_summary
            try:
                with st.spinner('Summarising the whole lecture…'):
                    complete=build_summary(workflow)
                if complete:st.rerun()
                else:st.warning(tutor_error(workflow.state.get('last_processing_error',{}).get('warnings',[])))
            except (ValueError,RuntimeError) as error:st.warning(str(error))

with guide_tab:
    st.subheader('Whole-lecture study guide')
    processing_progress()
    st.caption('Your qualitative content review is recorded for this baseline.' if approval else 'Saved materials for the selected lecture; generation and review status are shown separately.')
    guide = workflow.state['artifacts'].get('study_guide', {})
    if external_content_findings(guide):
        st.warning('Source review found a lecture simplification that needs clarification. Read the correction below alongside the generated guide.')
        for finding in external_content_findings(guide):
            st.markdown(source_markdown(finding.get('clarification',finding['reason']),workflow))
    if guide.get('content_review'):
        if guide['content_review']['status']=='needs_review':
            st.warning('The full guide is generated, with flagged sections that need source review. Inspect Source and content checks in Results & feedback.')
        else:st.caption('Automated source checks passed. Instructor review is a separate step; these checks do not guarantee completeness.')
    text = render_guide(guide) if guide.get('parsed') else 'The full study guide will appear here once generation finishes.'
    st.markdown(source_markdown(text, workflow))
    if not guide.get('parsed'):
        st.info('Create your study materials to cover the lecture in ordered chapters, with explanations, worked examples and useful comparison tables.')
        _plan = workflow.plan()
        _chapters = sum(bool(c.get('parsed')) for c in workflow.state.get('guide_chapters',{}).values())
        if worker_status(workflow.root).get('state')!='running':
            st.caption(f"Pages processed: {workflow.coverage()['processed_pages']}/{workflow.doc['page_count']} · Study chapters saved: {_chapters}. Completed steps are reused when you resume.")
        if _chapters:
            st.warning('The guide is incomplete. Saved chapters below are a partial draft, not coverage of the full lecture yet.')
            with st.expander('Read completed chapters — partial draft'):
                for chapter in workflow.state.get('guide_chapters',{}).values():
                    if chapter.get('parsed'):
                        st.markdown('### '+chapter['parsed']['title'])
                        st.markdown(source_markdown(chapter['parsed']['body'],workflow))
        if st.button('Create / resume study guide and quiz', type='primary', key=prefix+'prepare',
                     disabled=bool(cooldown) or worker_status(workflow.root).get('state')=='running'):
            create_materials()
    elif not workflow.state['artifacts'].get('lecture_quiz',{}).get('parsed'):
        st.info('The guide is ready. The quiz still needs to finish.')
        if st.button('Finish quiz generation', key=prefix+'finish_quiz',disabled=bool(cooldown) or worker_status(workflow.root).get('state')=='running'):
            create_materials()
    elif workflow.config.get('quality_policy') and (workflow.state['artifacts']['lecture_quiz'].get('content_review',{}).get('status')!='passed_automated_review' or editorial_flags):
        st.warning('The quiz is a saved draft with unfinished content checks. Inspect the review findings in Quiz.')
        if st.button('Resume quiz checks',key=prefix+'resume_quiz_checks',disabled=bool(cooldown) or worker_status(workflow.root).get('state')=='running'):
            create_materials()
    if workflow.state.get('last_processing_error') and worker_status(workflow.root).get('state')!='running':
        failure=workflow.state['last_processing_error']
        st.error(tutor_error(failure.get('warnings',[])))
        with st.expander('Last failed step'):
            st.write({'step':failure['task'],'model':failure.get('model'),
                      'source_pages':failure.get('source_ids',[])})
    if (workflow.root/'generation.lock').exists():
        st.info('This lecture is being processed. Completed pages and chapters are saved as it runs.')
        if st.button('Refresh saved progress',key=prefix+'refresh'):st.rerun()
    _coverage = read(workflow.root/'guide_coverage.json')
    if _coverage:
        with st.expander('Guide coverage and remaining uncertainty'):
            st.write(f"{_coverage['pages_accounted']}/{_coverage['total_pages']} pages accounted for across {_coverage['chapters']} study chapters; {_coverage['concepts_accounted']} extracted concepts mapped to guide headings.")
            st.caption(_coverage['limitation'])
            if guide.get('parsed',{}).get('uncertainties'):
                for uncertainty in guide['parsed']['uncertainties']:st.write('• '+uncertainty)
    show_source_picker(source_ids(text, workflow), 'guide_source')
    with st.expander('Source and teaching clarifications'):
        for note in read(PROJECT / 'notebook_review_notes.json', []):
            if any(s.startswith(workflow.doc['doc_id'] + ':') for s in note['source_ids']):
                st.markdown('**' + note['title'] + '**')
                st.markdown(source_markdown(note['explanation'] + ' ' + ' '.join(
                    '[' + s + ']' for s in note['source_ids']), workflow))
    guide_file = workflow.root / 'STUDY_GUIDE.md'
    if guide_file.exists():
        st.download_button('Download study guide', guide_file.read_bytes(), 'STUDY_GUIDE.md', 'text/markdown')

with source_tab:
    st.subheader('Original lecture and page notes')
    page_number = st.selectbox('Lecture page', range(1, workflow.doc['page_count'] + 1),
                               index=min(25,workflow.doc['page_count']-1), format_func=lambda n: f'Page {n}', key=prefix+'lecture_page')
    page = workflow.doc['pages'][page_number - 1]
    left, right = st.columns([1.2, 1])
    with left:
        st.image(page['image'], caption=f'Original page {page_number}', width='stretch')
    with right:
        note = workflow.page_notes.get(page['source_id'], {})
        st.markdown('#### Page explanation')
        st.markdown(source_markdown(note.get('summary') or page.get('text') or 'No generated page note yet. Inspect the original page image.', workflow))
        if note.get('uncertainties'):
            st.warning('; '.join(note['uncertainties']))
        with st.expander('Visual transcription'):
            st.caption('Model-derived search aid; use the original image to check formulas and diagrams.')
            st.markdown(source_markdown(note.get('visual_transcription') or 'No generated transcription yet.', workflow))
        with st.expander('Original extracted text'):
            st.text(page.get('text') or 'This page has no extractable text; inspect the image.')
    notes_file = workflow.root / 'LECTURE_NOTES.md'
    if notes_file.exists():
        st.download_button('Download full lecture notes', notes_file.read_bytes(), 'LECTURE_NOTES.md', 'text/markdown')

with quiz_tab:
    st.subheader('Check your understanding')
    st.caption('Submit every answer to see your score, explanations and original source pages. The quiz belongs to the selected lecture.')
    quiz_record = workflow.state['artifacts'].get('lecture_quiz_refined',workflow.state['artifacts'].get('lecture_quiz', {}))
    quiz = quiz_record.get('parsed')
    editorial_findings=[{'item':i,'findings':quiz_editorial_findings(q)} for i,q in enumerate((quiz or {}).get('questions',[]),1)]
    external_findings=external_quiz_findings(quiz_record)
    if quiz and (external_findings or (workflow.config.get('quality_policy') and (quiz_record.get('content_review',{}).get('status')!='passed_automated_review' or any(row['findings'] for row in editorial_findings)))):
        st.warning('This quiz is saved as a draft. Answer-key, ambiguity and source checks must finish before it can be graded.')
        with st.expander('Quiz review findings'):
            for finding in external_findings:st.write('Question '+str(finding['item'])+': '+finding['reason'])
            for row in editorial_findings:
                for finding in row['findings']:st.write('Question '+str(row['item'])+': '+finding)
            for item in quiz_record.get('content_review',{}).get('items',[]):
                if item.get('status')!='passed_automated_review':st.write(item.get('audit',{}).get('reason','Review pending'))
        with st.expander('Inspect saved draft — for content review'):
            st.caption('Unverified draft. These questions are not available for a scored practice attempt yet.')
            for i,q in enumerate(quiz['questions'],1):
                st.markdown('**'+str(i)+'. '+q['stem']+'**')
                for j,option in enumerate(q['options']):st.write(chr(65+j)+'. '+option)
                st.markdown(source_markdown('Proposed key: '+chr(65+q['answer_index'])+'. '+q['explanation'],workflow))
        quiz=None
    if quiz:
        quiz=quiz_for_practice(quiz,{'source':workflow.doc['sha256'],'conversation':lecture_state['conversation_id']})
        quiz_hash=fingerprint(quiz)
        if lecture_state.get('quiz_hash')!=quiz_hash:
            lecture_state['quiz_result']=None;lecture_state['quiz_hash']=quiz_hash
        with st.form(prefix+'lecture_quiz_'+quiz_hash[:10]):
            answers = []
            for i, question in enumerate(quiz['questions'], 1):
                st.markdown(f"**{i}. {source_markdown(question['stem'], workflow)}**")
                answers.append(st.radio(f'Answer to question {i}', ['A', 'B', 'C', 'D'],
                    index=None, key=prefix+quiz_hash[:10]+f'_quiz_{i}', label_visibility='collapsed',
                    format_func=lambda letter, q=question, render=math_markdown: f"{letter}. {render(q['options'][ord(letter)-65])}"))
            quiz_submitted = st.form_submit_button('Submit quiz', type='primary')
        if quiz_submitted:
            try:
                result = grade_quiz(quiz, answers)
                lecture_state['quiz_result'] = result
                log_event(workflow, {'event': 'quiz_submitted', 'session_id': session,
                                     'score': result['score'], 'total': result['total'],
                                     'presented_quiz_hash':quiz_hash,'source_quiz_hash':quiz['display_order']['source_quiz_hash']})
            except ValueError:
                st.warning(f"Please answer all {len(quiz['questions'])} questions before submitting.")
        result = lecture_state['quiz_result']
        if result:
            st.success(f"Your score: {result['score']} / {result['total']}")
            for row in result['items']:
                with st.expander(f"Question {row['item']} · {'Correct' if row['correct'] else 'Review'}"):
                    st.write(f"Your submitted answer: {row['selected']} · Key: {row['key']}")
                    st.markdown(source_markdown(row['explanation'], workflow))
                    show_source_picker(row['citations'], f"quiz_source_{row['item']}")
    else:
        st.info('The saved quiz needs content review before practice.' if quiz_record.get('parsed') else
                'No quiz has been generated for this lecture yet. Use the preparation controls in Study guide when ready.')

with tutor_tab:
    st.subheader('Ask your lecture tutor')
    st.caption('Choose a direct answer, a hint, a Socratic exchange or a practice activity. Answers use retrieved original lecture pages.')
    for i, message in enumerate(lecture_state['messages']):
        with st.chat_message('user'):
            st.markdown(source_markdown(message['question'], workflow))
            st.caption('Mode: ' + message['mode'])
        with st.chat_message('assistant'):
            review=message.get('content_review') or {}
            if review and review.get('status')!='passed_automated_review':
                st.warning('This answer needs source or mathematical review. Check the cited original pages.')
                for issue in review.get('audit',{}).get('issues',[]):st.caption(issue)
            st.markdown(source_markdown(message['answer'], workflow))
            if message.get('raw_answer'):
                with st.expander('Original generated draft — review required'):
                    st.markdown(source_markdown(message['raw_answer'], workflow))
            show_source_picker(message['source_ids'], f'turn_{i}_source')
    with st.form(prefix+'tutor_question'):
        question = st.text_area('Your question', key=prefix+'tutor_question_text', height=100,
            placeholder='For example: Why does normal form work well for vertical lines?')
        mode = st.selectbox('Teaching mode', ['direct', 'hint', 'socratic', 'practice'],
            format_func=lambda value: {'direct': 'Direct answer', 'hint': 'Hint',
                'socratic': 'Socratic questions', 'practice': 'Practice activity'}[value], key=prefix+'teaching_mode')
        ask = st.form_submit_button('Ask Gemini', type='primary',disabled=bool(cooldown))
    request = None
    if ask:
        if question.strip():
            request = (question.strip(), mode)
        else:
            st.warning('Write a question first.')
    if lecture_state.get('pending_retry'):
        if st.button('Retry last question once'):
            request = lecture_state['pending_retry']
    if request:
        question, mode = request
        try:
            with st.spinner('Reading relevant lecture pages and asking Gemini…'):
                record, retrieval = request_tutor(workflow, question, mode, session)
            log_event(workflow, {'event': 'tutor_attempt', 'session_id': session,
                'mode': mode, 'generation_id': record['id'], 'status': record['status']})
            if record.get('parsed'):
                lecture_state['messages'].append({'question': question, 'mode': mode,
                    'answer': tutor_text(record),
                    'raw_answer': tutor_text(record,draft=True) if record.get('display_answer') else None,
                    'local_math_review':record.get('local_math_review'), 'source_ids': record['source_ids'],
                    'generation_id': record['id'],'content_review':record.get('content_review')})
                lecture_state.pop('pending_retry', None)
                lecture_state.pop('last_error', None)
            else:
                lecture_state['last_error'] = tutor_error(record.get('warnings', []))
                lecture_state['pending_retry'] = (question, mode)
            st.rerun()
        except RuntimeError as error:
            st.warning(str(error))
        except Exception as error:
            st.error('The request could not finish. Your saved lecture materials are still available.')
            log_event(workflow, {'event': 'tutor_exception', 'session_id': session,
                                 'error_type': type(error).__name__})
    if lecture_state.get('last_error'):
        st.warning(lecture_state['last_error'])
    if not lecture_state['messages']:
        st.info('No live answers in this conversation yet. You can inspect saved examples below while Gemini is busy.')
    with st.expander('Saved tutor examples — no new API call'):
        st.caption('Previously recorded examples, separate from this live conversation.')
        for example in read(workflow.root / 'acceptance_checks.json', []):
            st.markdown('**' + example['case'] + '**')
            st.markdown(source_markdown(example.get('answer') or 'No accepted answer.', workflow))
            show_source_picker(example.get('source_ids', []), 'example_' + example['case'])
    if lecture_state['messages']:
        st.download_button('Download this conversation',
            json.dumps(lecture_state['messages'], indent=2, ensure_ascii=False),
            'smartcampus_conversation.json', 'application/json', key=prefix+'conversation_download')

with results_tab:
    quality=read(workflow.root/'content_quality.json',{})
    if quality:
        st.subheader('Source and content checks')
        labels={'pending':'Not complete','passed_automated_review':'Automated checks passed','needs_review':'Review required'}
        guide_status=quality.get('guide','pending')
        if any(c.get('review',{}).get('status')=='needs_review' for c in quality.get('chapter_reviews',[])):
            guide_status='needs_review'
        st.write('Study guide: '+labels.get(guide_status,guide_status)+' · Quiz: '+labels.get(quality.get('quiz','pending'),quality.get('quiz','pending')))
        st.caption('These checks assess source support, concept explanations, quiz answers and selected calculations. Instructor review remains separate; automated checks do not guarantee correctness or completeness.')
        for chapter in quality.get('chapter_reviews',[]):
            for flag in chapter.get('review',{}).get('flags',[]):
                st.warning(flag.get('concept','Concept')+': '+flag.get('reason','Review the original source.'))
    st.subheader('This lecture’s progress and evaluation')
    coverage = workflow.coverage()
    metrics = read(workflow.root / 'suite_results/latest.json', {})
    scores_saved = metrics.get('scores', [])
    case_count = len({r['sample_id'] for r in scores_saved})
    a, b, c = st.columns(3)
    a.metric('Pages processed', f"{coverage['processed_pages']} / {coverage['total_pages']}")
    b.metric('Development questions', str(case_count))
    c.metric('RAGAS metric values', str(sum(r.get('value') is not None for r in scores_saved)))
    st.caption('Page processing measures coverage. Saved development measurements belong to this lecture and are not an overall accuracy score or a held-out benchmark.')
    if not metrics:
        st.info('No RAGAS evaluation has been recorded for these uploaded study materials. Earlier research scores are kept separately.')
    labels = {'faithfulness': 'Faithfulness', 'context_precision': 'Context precision',
              'context_recall': 'Context recall', 'answer_relevancy': 'Answer relevancy'}
    for scope, scores in metrics.get('summary_by_scope', {}).items():
        st.markdown('**' + ('Assistant source-image transcription' if
            'assistant_source_image_transcription' in scope else 'Original extracted text') + '**')
        rows = [{'Metric': labels.get(name, name), 'Mean': round(value['mean'], 3),
                 'Scored cases': value['n_scored']} for name, value in scores.items()]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width='stretch')
    with st.expander('Metric definitions and limits'):
        st.markdown('''- **Faithfulness:** support for answer claims in the supplied judge context.
- **Context precision:** how early relevant evidence appears in the selected context.
- **Context recall:** reference-answer claims supported by that context.
- **Answer relevancy:** semantic alignment with the question.

These match the paper's metric families, with documented project settings. They do not measure every page's correctness, quiz quality or learning gains. The source-image transcription track still awaits separate verification. Browsing this report never runs a judge.''')
    with st.expander('Generation details'):
        st.write({'models_used_for_accepted_outputs':workflow.state.get('generation_models_used',[]),
                  'current_model':workflow.state.get('runtime_model',workflow.ns['CFG']['model']) if workflow.config.get('allow_model_fallback',False) else workflow.ns['CFG']['model'],
                  'schema_transport':workflow.ns['CFG'].get('schema_mode','json_schema')})
        st.caption('This edition keeps Gemini 3.5 fixed and records bounded retries. Earlier benchmark outputs remain separate.' if not workflow.config.get('allow_model_fallback',False) else 'Any model fallback is recorded with its output.')
    st.divider()
    st.subheader('How your feedback improves the system')
    st.write('Feedback is saved for review. It does not automatically train the LLM, change prompts, alter retrieval, or increase metric scores. We use reports to identify a specific problem, make a targeted change, and compare it with the saved baseline before adopting it.')
    st.caption('Examples: an incorrect answer prompts a source/reference check; irrelevant pages suggest a retrieval issue; confusing controls suggest a UI change. Quiz scores describe this attempt, not measured learning gains.')
    st.subheader('Feedback on this trial')
    st.caption('Real feedback only. Use an anonymous code. These local trial responses are separate from the earlier content approval and from a formal study.')
    dimensions = {
        'ease_starting_conversation': 'It was easy to start a conversation with the tutor.',
        'response_relevance_accuracy': 'Responses were relevant and appeared accurate.',
        'perceived_resistance_to_hallucination': 'The tutor stayed grounded in the supplied material.',
        'overall_recommendation': 'I would recommend this tutor for studying.'}
    with st.form(prefix+'trial_feedback'):
        code = st.text_input('Anonymous trial code', value='trial_' + st.session_state.trial_id[:8])
        role = st.selectbox('Your role', ['student', 'instructor', 'other'])
        ratings = {name: st.radio(label, ['Not rated', '1', '2', '3', '4', '5'],
            horizontal=True, key=prefix+'rating_' + name) for name, label in dimensions.items()}
        st.caption('1 = strongly disagree · 5 = strongly agree. Leave Not rated if you could not assess it.')
        useful = st.text_area('Most useful features')
        comments = st.text_area('Anything confusing, incorrect or missing?')
        feedback_submitted = st.form_submit_button('Save trial feedback')
    if feedback_submitted:
        try:
            save_feedback(workflow, {'trial_code': code.strip(), 'role': role,
                **{name: '' if v == 'Not rated' else v for name, v in ratings.items()},
                'most_useful_features': useful.strip(), 'comments': comments.strip()})
            st.success('Your feedback was saved locally. Thank you.')
        except ValueError as error:
            st.warning(str(error))
    feedback_file = workflow.root / 'trial_ui/feedback.csv'
    if feedback_file.exists():
        st.download_button('Download local trial feedback', csv_download(feedback_file),
                           'smartcampus_trial_feedback.csv', 'text/csv')

ledger = read(workflow.root / 'chat_request_ledger.json', {'attempts': 0, 'reserved_tokens': 0})
remaining = max(0, workflow.config.get('chat_max_requests', 20) - ledger['attempts'])
budget_display.metric('Local tutor allowance left', remaining)
