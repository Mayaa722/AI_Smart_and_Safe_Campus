"""A concise overview, separate from the comprehensive study guide.

Every page must have been processed first. This synthesis uses labelled model
notes, not a second independent reading of all source images.
"""
import json
from lecture_workflow import obj, STR, STRINGS, fingerprint, all_citations, write

VERSION = 'lecture_summary_v2'
SCHEMA = obj({'title':STR, 'body':STR, 'citations':STRINGS, 'uncertainties':STRINGS})

def build_summary(w):
    if not w.coverage()['processing_complete']:
        raise ValueError('Read every lecture page before creating the lecture summary.')
    sections=[s['record']['parsed'] for s in sorted(w.state['sections'],key=lambda s:s['index']) if s['status']=='accepted']
    identity=fingerprint({'version':VERSION,'source':w.doc['sha256'],'model':w.ns['CFG']['model'],'sections':sections})
    previous=w.state['artifacts'].get('lecture_summary',{})
    if previous.get('parsed') and previous.get('summary_identity')==identity:
        return True
    # Compact section concepts bound synthesis size without clipping the end of
    # a long lecture. Each accepted source batch contributes equally.
    context='MODEL-DERIVED SOURCE NOTES, NOT INDEPENDENT VERIFICATION:\n'+json.dumps([
        {'title':s['title'],'pages':[{'source_id':p['source_id'],'summary':p['summary'],
          'kind':p['kind'],'uncertainties':p['uncertainties']} for p in s['pages']]} for s in sections],ensure_ascii=False)
    allowed={p['source_id'] for p in w.doc['pages']}
    def validate(data):
        if not data['body'].strip() or not data['citations'] or set(all_citations(data))-allowed:
            raise ValueError('Summary needs a body and valid source citations.')
        if len(data['body'].split())>850:
            raise ValueError('Summary exceeds 850 words; shorten without dropping major topics.')
    record=w.request('lecture_summary',w.doc['pages'],
        'Write a SHORT lecture overview of 350-650 words (hard maximum 850), distinct from the detailed study guide. '
        'Cover the beginning, middle AND end; group related concepts under clear headings. Include the main definitions, '
        'relationships, essential formulas with domain restrictions, and a compact comparison table only if useful. '
        'Explain the takeaway of important diagrams rather than inventing their geometry. Cite exact source IDs in the body. '
        'Preserve source uncertainty and distinguish a source claim from additional teaching. No new worked exercises or quiz. '
        'The overview is for quick revision; the full guide remains the detailed resource. Body is student-facing Markdown. '
        'Use a small set of genuinely supporting citations (typically 8-15), not a list of every page. '
        'These are compact page summaries: do not reconstruct an absent formula or detail by guessing. '
        'Use plain ASCII equations for the existing math renderer.',schema=SCHEMA,context=context,images=False,validator=validate)
    record['summary_identity']=identity
    record['summary_version']=VERSION
    record['evidence_policy']='Synthesis of all accepted visual page notes; not independent source verification or instructor approval.'
    if previous and previous.get('output_hash')!=record.get('output_hash'):
        w.state.setdefault('superseded_artifacts',[]).append({'artifact':'lecture_summary','record':previous})
    w.state['artifacts']['lecture_summary']=record
    w.save()
    if record.get('parsed'):
        (w.root/'LECTURE_SUMMARY.md').write_text('# '+record['parsed']['title']+'\n\n'+record['parsed']['body']+'\n\n'+
            '\n'.join('Uncertainty: '+u for u in record['parsed']['uncertainties']),encoding='utf-8')
        return True
    return False
