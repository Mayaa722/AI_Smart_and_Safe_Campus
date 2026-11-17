"""Optional supervised-development repairs; never replace matched benchmark outputs.

Run only through the existing Colab GPU Runner. The source cards are assistant
review feedback, not independent instructor ratings or unseen benchmark inputs.
"""
import json
if __package__:
    from comparison.improved import CHAPTER,QUESTION
    from comparison.common import read,write,digest
else:
    from improved import CHAPTER,QUESTION
    from common import read,write,digest

def repair_candidates(runner):
    assert runner.arm=='qwen','This repair module is for the Colab GPU arm only.'
    cards=read(runner.root/'source_review_cards.json')
    assert digest(read(runner.root/'corpus.json'))==cards['corpus_digest'],'Review cards belong to different source material.'
    corpus={x['id']:x for x in read(runner.root/'corpus.json')}
    results=[]
    for card in cards['items']:
        lecture=corpus[card['lecture']]
        if card['kind']=='chapter':
            guide=read(runner.out/(card['lecture']+'_guide.json'))
            original=read(runner.out/'records'/(guide['chapter_ids'][card['item']-1]+'.json'))
            candidate=original['parsed'];ids=original['source_ids'];schema=CHAPTER
            def validate(parsed):
                if [p['source_id'] for p in parsed['page_coverage']]!=ids:
                    raise ValueError('Retain the exact ordered original source pages.')
        else:
            quiz=read(runner.out/(card['lecture']+'_quiz_checked.json'))
            candidate=quiz['questions'][card['item']-1];ids=candidate['citations'];schema=QUESTION
            validate=None
        pages=[p for p in lecture['pages'] if p['source_id'] in ids]
        record=runner.call('reviewed_'+card['id'],'external_source_review_repair',pages,
            'Produce a corrected candidate using the ORIGINAL evidence and the explicit external review findings. '
            'The previous model answer is untrusted. Preserve valid information and remove the identified error. '
            'Label new mathematical derivations Additional teaching rather than claiming the slide wrote them literally. '
            'Use concise ASCII math, exact source IDs, no invented plot readings and no pipeline commentary. '
            'For an MCQ replace the entire item when its old choices have no defensible answer. Use four parallel, plausible '
            'lecture-based alternatives, exactly one correct key and an explanatory rationale. Put citations in metadata '
            'and explanations, never inside options. For a chapter retain all supplied page_coverage entries.',
            schema,extra='ORIGINAL CANDIDATE\n'+json.dumps(candidate)+'\nEXTERNAL REVIEW (ASSISTANT-AUTHORED DEVELOPMENT FEEDBACK)\n'+json.dumps(card),
            limit=1600,validator=validate)
        result={'card':card,'record':record,'status':'candidate_generated' if record.get('parsed') else 'unavailable',
            'instructor_review':'not_collected','scope':'Post-comparison supervised repair; excluded from original benchmark scores.'}
        write(runner.out/'reviewed_candidates'/(card['id']+'.json'),result);results.append(result)
        write(runner.out/'reviewed_candidates/index.json',results)
    return results
