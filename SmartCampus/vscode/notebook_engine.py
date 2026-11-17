"""Load the established tutor definitions without executing its pilot cells."""
import contextlib
import io
import os
import hashlib
from pathlib import Path

PROJECT = Path(__file__).resolve().parent


def initialize_notebook(namespace):
    """Prepare an independently run notebook cell without resetting its chat."""
    import json
    import pandas as pd
    from IPython.display import display, Markdown, Image
    from lecture_workflow import LectureWorkflow, read, render_guide
    from notebook_presentation import (link_sources, show_quiz, grade_quiz,
                                       show_quiz_feedback, show_tutor_response)

    namespace.update(Path=Path, json=json, pd=pd, display=display, Markdown=Markdown,
                     Image=Image, LectureWorkflow=LectureWorkflow, read=read,
                     render_guide=render_guide, link_sources=link_sources,
                     show_quiz=show_quiz, grade_quiz=grade_quiz,
                     show_quiz_feedback=show_quiz_feedback,
                     show_tutor_response=show_tutor_response)
    namespace.setdefault('PROJECT', PROJECT)
    namespace.setdefault('CONFIG_PATH', PROJECT / 'full_lecture_config.json')
    namespace.setdefault('ENABLE_WIDGETS', False)
    current=namespace.get('workflow')
    if current is None or Path(current.config_path).resolve()!=Path(namespace['CONFIG_PATH']).resolve():
        namespace['workflow'] = LectureWorkflow(namespace['CONFIG_PATH'])
    return namespace['workflow']


def load_engine(documents=None):
    for line in (PROJECT / '.env').read_text(encoding='utf-8-sig').splitlines():
        if '=' not in line or line.lstrip().startswith('#'):
            continue
        name, value = line.strip().split('=', 1)
        if name in {'GEMINI_API_KEY', 'GOOGLE_API_KEY', 'SMARTCAMPUS_FREE_TIER_VERIFIED',
                    'GROQ_API_KEY', 'SMARTCAMPUS_GROQ_FREE_TIER_VERIFIED'} and value.strip():
            os.environ[name] = value.strip().strip('"').strip("'")
    os.environ['SMARTCAMPUS_ROOT'] = str(PROJECT / 'run')
    os.environ['SMARTCAMPUS_INPUT_DIR'] = str(PROJECT.parent / 'inputs')
    core = PROJECT / 'tutor_core.py'
    ns = {'__file__': str(core), '__name__': 'smartcampus_tutor_core'}
    if documents is not None:
        ns['UPLOADED_DOCUMENTS'] = documents
    source=core.read_text(encoding='utf-8')
    ns['ENGINE_SOURCE_SHA256']=hashlib.sha256(source.encode('utf-8')).hexdigest()
    with contextlib.redirect_stdout(io.StringIO()):
        exec(compile(source, str(core), 'exec'), ns)
    return ns
