"""Render all student-facing response fields; guards take precedence."""
import re

def tutor_text(record, draft=False):
    if record.get('display_answer') and not draft:return record['display_answer']
    parsed=record.get('parsed') or {}
    answer=parsed.get('answer','').strip()
    followup=parsed.get('next_question','').strip()
    mode=record.get('mode') or record.get('task','').removeprefix('chat_')
    if mode=='practice' and followup and re.fullmatch(r'not yet attempted by (?:the )?student\.?',answer,re.I):
        answer=''
    if followup and ' '.join(followup.split()).casefold() not in ' '.join(answer.split()).casefold():
        return (answer+'\n\n' if answer else '')+'**Your turn:** '+followup
    return answer
