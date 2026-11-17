"""Checkpointed study chapters, with explicit structural coverage accounting."""
import json
import re
from datetime import datetime, timezone
from lecture_workflow import PROJECT, obj, STR, STRINGS, fingerprint, read, write

VERSION = 'comprehensive_v2'
CHAPTER_SCHEMA = obj({'title': STR, 'body': STR,
    'page_coverage': {'type':'array','items':obj({'source_id':STR,'heading':STR})},
    'concept_coverage': {'type':'array','items':obj({'concept_id':STR,'heading':STR})},
    'uncertainties': STRINGS})
OVERVIEW_SCHEMA = obj({'title':STR,'body':STR,'citations':STRINGS,'uncertainties':STRINGS})


def chapter_sections(w):
    """Group unwritten notes independently of transport batches; keep accepted chapters."""
    sections=sorted((s for s in w.state['sections'] if s['status']=='accepted'),
                    key=lambda s:int(s['source_ids'][0].split(':p')[1]))
    result=[]
    pending=[]
    def flush():
        if not pending:return
        if len(pending)==1:result.append(pending[0])
        else:
            parts=[s['record']['parsed'] for s in pending]
            parsed={'title':' / '.join(p['title'] for p in parts),
                    'pages':[p for part in parts for p in part['pages']],
                    'concepts':[c for part in parts for c in part['concepts']],
                    'uncertainties':[u for part in parts for u in part.get('uncertainties',[])]}
            hashes=[s['record']['output_hash'] for s in pending]
            result.append({'index':pending[0]['index'],
                'source_ids':[sid for s in pending for sid in s['source_ids']],
                'record':{'parsed':parsed,'output_hash':fingerprint({'chapter_source_groups':hashes}),
                          'derived_from_section_hashes':hashes}})
        pending.clear()
    for section in sections:
        identity=fingerprint({'version':VERSION,'section_hash':section['record']['output_hash']})
        if w.state.get('guide_chapters',{}).get(identity,{}).get('parsed'):
            flush();result.append(section);continue
        if sum(len(s['source_ids']) for s in pending)+len(section['source_ids'])>w.config.get('chapter_pages_per_batch',(4 if w.config.get('quality_policy') else 8)):flush()
        pending.append(section)
    flush()
    expected=[sid for s in sections for sid in s['source_ids']]
    actual=[sid for s in result for sid in s['source_ids']]
    if actual!=expected or len(set(actual))!=len(actual):
        raise ValueError('Chapter grouping must preserve every accepted page exactly once.')
    return result


def apply_source_clarifications(w):
    """Keep known source corrections in the standalone guide, preserving raw chapters."""
    record = w.state['artifacts'].get('study_guide', {})
    if record.get('guide_version') != VERSION or not record.get('parsed'):
        return
    notes = [n for n in read(PROJECT/'notebook_review_notes.json', [])
             if any(s.startswith(w.doc['doc_id']+':') for s in n['source_ids'])]
    marker = '<!-- source-clarifications -->'
    if notes and marker not in record['parsed']['answer']:
        annotation = '\n\n'+marker+'\n## Source corrections and teaching distinctions\n\n'
        annotation += 'Read these labelled source-review clarifications alongside the chapters below. Original lecture pages and raw model chapters are retained unchanged.\n\n'
        for note in notes:
            annotation += '### '+note['title']+'\n\n'+note['explanation']+' '+ ' '.join('['+s+']' for s in note['source_ids'])+'\n\n'
        body = record['parsed']['answer']
        header, separator, chapters = body.partition('\n\n## ')
        record['parsed']['answer'] = header+annotation+(separator+chapters if separator else '')
        record['source_clarifications'] = fingerprint(notes)
        record['output_hash'] = w.ns['digest'](record['parsed'])


def build_guide(w):
    completed = w.state['artifacts'].get('study_guide', {})
    if completed.get('guide_version') == VERSION and completed.get('parsed') and (not w.config.get('quality_policy') or completed.get('quality_policy')==w.config['quality_policy']):
        apply_source_clarifications(w)
        return True
    chapters = w.state.setdefault('guide_chapters', {})
    sections = chapter_sections(w)
    w.state['guide_source_groups']=[{'source_ids':s['source_ids'],
        'section_hash':s['record']['output_hash']} for s in sections]
    w.save()
    bodies, source_ids, uncertainties = [], [], []
    unresolved_reviews=[]
    concept_total = 0
    for ordinal,section in enumerate(sections,1):
        w.notify(f'Study chapter {ordinal}/{len(sections)}')
        parsed = section['record']['parsed']
        concepts = [{'concept_id':f"s{section['index']}:c{i}", **c}
                    for i,c in enumerate(parsed['concepts'],1)]
        concept_total += len(concepts)
        review_notes = [n for n in read(PROJECT/'notebook_review_notes.json', [])
                        if set(n['source_ids']) & set(section['source_ids'])]
        context = json.dumps({'section_title':parsed['title'], 'pages':parsed['pages'],
                              'concepts':concepts,'labelled_source_review_clarifications':review_notes}, ensure_ascii=False)
        if w.config.get('quality_policy'):
            from lecture_quality import page_evidence
            context=page_evidence(w,section['source_ids'])+'\nLABELLED MODEL-DERIVED NOTES (NOT INDEPENDENT EVIDENCE)\n'+context
        identity = fingerprint({'version':VERSION,'section_hash':section['record']['output_hash'],
                                **({'quality_policy':w.config['quality_policy']} if w.config.get('quality_policy') else {})})
        entry = chapters.get(identity, {})
        ids = [p['source_id'] for p in parsed['pages']]
        expected_concepts = {c['concept_id'] for c in concepts}

        def validate(result):
            if {p['source_id'] for p in result['page_coverage']} != set(ids) or len(result['page_coverage']) != len(ids):
                raise ValueError('Guide coverage: every section page needs one guide coverage entry.')
            if {c['concept_id'] for c in result['concept_coverage']} != expected_concepts or len(result['concept_coverage']) != len(expected_concepts):
                raise ValueError('Guide coverage: every extracted concept needs one guide coverage entry.')
            headings = {line.lstrip('#').strip() for line in result['body'].splitlines() if line.startswith('#')}
            if any(row['heading'].strip() not in headings for row in result['page_coverage']+result['concept_coverage']):
                raise ValueError('Guide coverage: entries must point to actual guide headings.')
            if any(c in result['body'] for c in ['\r','\t','\\']):
                raise ValueError('Guide contains unsupported escapes; use plain ASCII math.')
            citations = set(re.findall(r'[0-9a-f]{12}:p\d+',result['body']))
            required = {p['source_id'] for p in parsed['pages'] if p['kind']=='content'}
            if not required.issubset(citations) or citations-set(ids):
                raise ValueError('Guide coverage: missing inline content-page citations: '+str(sorted(required-citations))+
                                 '; outside-section citations: '+str(sorted(citations-set(ids))))

        if not entry.get('parsed'):
            print(f"Study chapter {ordinal}/{len(sections)}",flush=True)
            instruction = '''Write a self-contained, comprehensive but concise study chapter for ALL the supplied section notes.
The student should be able to learn from this chapter rather than needing the slides to understand it.
Explain every substantive concept, definition, distinction, formula (define variables and conditions), algorithm/method (ordered steps), diagram interpretation, application and limitation present in these notes.
Explain WHY and WHEN methods apply. Where the source presents alternatives, include a concise Markdown comparison table with differences, assumptions, strengths, weaknesses and appropriate uses. Do not force a comparison or invent unsupported pros/cons.
Include a small worked example when useful; label invented examples Additional teaching. Preserve source ambiguities and unreadable details explicitly instead of guessing. Explain diagram meaning verbally, not merely 'see slide'.
Answer questions posed by the lecture when a concise answer can be derived directly from supplied equations or figures; label the derivation Additional teaching if not explicitly stated. For title/credits/outline pages, briefly describe only their actual content, without teaching future topics not supplied here.
Apply the supplied labelled source-review clarifications when explaining an affected topic; distinguish corrected notation or an unsuitable objective from an algorithm/optimizer error. Preserve the source attribution and explicitly label any source typo.
Use descriptive Markdown headings, short paragraphs and focused bullets. Avoid duplication. Scale length to content: no fixed whole-lecture word cap. Aim for roughly 400–900 words for a substantive 8-page section, less for administrative material. Include revision takeaways.
Use plain ASCII math (rho, theta, sqrt, /, ^); no backslashes/control characters. The interface typesets equations.
Place exact [source_id] citations alongside source-supported explanations. Explicitly cite EVERY content-page ID, including title/transition pages when explaining their section. Write full source IDs individually, never page ranges. Administrative pages may be noted briefly without padding the chapter.
Return page_coverage with exactly one entry per supplied source_id and concept_coverage with exactly one entry per supplied concept_id. Each heading must exactly match an actual Markdown heading in body where that content is explained. These are coverage pointers, not claims of independently verified completeness.'''
            instruction+='\nKeep all schema, pointer, coverage-audit and pipeline commentary out of the student-facing body. Put only lecture teaching in body.'
            entry = w.request('comprehensive_chapter', [w.ns['PAGES'][sid] for sid in ids],
                              instruction, schema=CHAPTER_SCHEMA, context=context, images=bool(w.config.get('quality_policy')), validator=validate)
            chapters[identity] = entry
            w.save()
            if not entry.get('parsed'):
                return False
        if w.config.get('quality_policy'):
            from lecture_quality import review_chapter
            entry,passed=review_chapter(w,entry,ids,validator=validate)
            validate(entry['parsed'])
            chapters[identity]=entry;w.save()
            if not passed:
                unresolved_reviews.append({'chapter_id':entry['id'],'source_ids':ids,'review':entry.get('content_review',{})})
        result = entry['parsed']
        validate(result)
        chapter_body = result['body'].strip()
        first_line, separator, rest = chapter_body.partition('\n')
        if first_line.lstrip('#').strip()==result['title'].strip():
            chapter_body=rest.lstrip()
        chapter_body=re.sub(r'^(#{1,6})\s+',lambda m:'#'*min(6,len(m.group(1))+2)+' ',chapter_body,flags=re.M)
        bodies.append('## '+result['title']+'\n\n'+chapter_body)
        source_ids.extend(ids)
        uncertainties.extend(result['uncertainties'])
    overview = w.state.get('guide_overview',{})
    if w.config.get('quality_policy'):
        overview={'id':'source-roadmap-'+fingerprint(source_ids)[:16],
            'parsed':{'title':'Lecture roadmap','body':'Follow the source-checked chapters in lecture order:\n\n'+
                '\n'.join('- '+s['record']['parsed']['title']+' '+ ' '.join('['+sid+']' for sid in s['source_ids']) for s in sections),
                'citations':source_ids,'uncertainties':[]},'status':'deterministic_roadmap'}
        w.state['guide_overview']=overview
    if not overview.get('parsed'):
        concepts_context=json.dumps([{'title':s['record']['parsed']['title'],
            'concepts':s['record']['parsed']['concepts']} for s in sections],ensure_ascii=False)
        def validate_overview(result):
            if not result['body'].strip() or not result['citations']:
                raise ValueError('Guide coverage: overview needs an explanation and source citations.')
            if set(result['citations'])-set(source_ids):raise ValueError('Guide coverage: overview cites an unknown page.')
            if any(c in result['body'] for c in ['\r','\t','\\']):raise ValueError('Guide contains unsupported math escapes.')
        overview=w.request('comprehensive_overview',w.doc['pages'],
            'Create a concise whole-lecture study companion connecting ALL the supplied sections. '
            'Include a topic roadmap, comparison tables connecting alternative methods/representations across sections where applicable, '
            'key formulas with variables/conditions, useful definitions and two short worked examples labelled Additional teaching. '
            'Explain distinctions, assumptions, strengths, weaknesses and when to choose an approach. '
            'Do not invent source-supported pros/cons, or claim new examples appeared in the lecture. '
            'This accompanies detailed chapters; avoid restating them at length. Use Markdown and exact page citations. '
            'Use plain ASCII math, no backslashes or control characters. Preserve ambiguities and uncertainties.',
            schema=OVERVIEW_SCHEMA,context=concepts_context,images=False,validator=validate_overview)
        w.state['guide_overview']=overview
        w.save()
        if not overview.get('parsed'):return False
    uncertainties.extend(overview['parsed']['uncertainties'])
    body = ('# Comprehensive lecture study guide\n\n'+w.doc['name']+
        '\n\nThis guide follows the full lecture in ordered chapters. Source ambiguities and additional teaching are labelled.\n\n'+
        '## Whole-lecture connections and quick reference\n\n'+overview['parsed']['body']+'\n\n'+
        '\n\n'.join(bodies))
    previous = w.state['artifacts'].get('study_guide')
    if previous:
        w.state.setdefault('superseded_artifacts', []).append({'artifact':'study_guide','record':previous,
            'reason':'Replaced by comprehensive chapter synthesis; original retained.'})
    parsed = {'title':'Comprehensive study guide','answer':body,'citations':source_ids,
              'claims':[],'uncertainties':uncertainties,'next_question':''}
    assembled = {'id':'assembled-'+fingerprint([c['id'] for c in chapters.values() if c.get('parsed')]+[overview['id']])[:24],
        'status':'assembled_from_live_chapters','guide_version':VERSION,'parsed':parsed,
        'output_hash':w.ns['digest'](parsed),'warnings':['Coverage is structurally checked, not independently verified semantic completeness.']}
    # Reassembling identical cached chapters must not erase an external finding.
    if previous and previous.get('external_content_review',{}).get('parsed_fingerprint')==fingerprint(parsed):
        assembled['external_content_review']=previous['external_content_review']
    if w.config.get('quality_policy'):
        assembled['quality_policy']=w.config['quality_policy']
        assembled['content_review']={'status':'needs_review' if unresolved_reviews else 'passed_automated_review','policy':w.config['quality_policy'],
            'flagged_chapters':unresolved_reviews,
            'limitation':'All chapters are generated. Source-based model checks are not independent instructor approval.'}
    w.state['artifacts']['study_guide'] = assembled
    apply_source_clarifications(w)
    write(w.root/'guide_coverage.json',{'created_at':datetime.now(timezone.utc).isoformat(),
        'pages_accounted':len(set(source_ids)),'total_pages':w.doc['page_count'],
        'concepts_accounted':concept_total,'chapters':len(bodies),'status':'structural_coverage_complete',
        'limitation':'Pointers and citations are checked. Semantic correctness and completeness still need content review.'})
    w.save()
    return True
