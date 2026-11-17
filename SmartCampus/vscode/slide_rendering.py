"""Reuse source-hash-checked slide images after notebook extraction/cache reloads."""
import json
from pathlib import Path


def apply_registered_renders(documents, project):
    root=Path(project)
    manifest=root/'run/rendered_slides/exports.json'
    if not manifest.exists():return documents
    rows=json.loads(manifest.read_text(encoding='utf-8-sig'))
    if isinstance(rows,dict):rows=[rows]
    records={r['doc_id']:r for r in rows if r.get('status')=='exported'}
    validation_path=root/'run/rendered_slides/validation.json'
    verified={r['doc_id']:r for r in json.loads(validation_path.read_text(encoding='utf-8'))} if validation_path.exists() else {}
    for document in documents:
        row=records.get(document['doc_id'])
        if not row or document['type']!='pptx':continue
        pdf=Path(row['pdf_path'])
        if row['slide_count']!=document['page_count']:
            raise ValueError('Registered slide rendering has a different page count.')
        images=[pdf.parent/f'page_{i:04}.png' for i in range(1,document['page_count']+1)]
        if not all(image.is_file() and image.stat().st_size>0 for image in images):
            raise ValueError('Registered slide images are missing; rerender before visual processing.')
        # doc_id is the source SHA prefix, tied to the original extraction manifest.
        if document['sha256'][:12]!=row['doc_id'] or verified.get(row['doc_id'],{}).get('source_sha256')!=document['sha256']:
            raise ValueError('Slide image source identity mismatch.')
        for page,image in zip(document['pages'],images):
            page['image']=str(image)
            page['warnings']=[w for w in page.get('warnings',[]) if not w.startswith('No complete slide image:')]
        document['warnings']=[w for w in document.get('warnings',[]) if not w.startswith(('Full-slide renderer unavailable','PPTX rendering failed'))]
        document['rendering']={**document.get('rendering',{}),'renderer':'Microsoft PowerPoint read-only PDF copy + PyMuPDF',
            'pdf':str(pdf),'full_images':len(images),'powerpoint_open_and_repair':bool(row.get('open_and_repair')),
            'original_unchanged':True}
        warning='PowerPoint rendered a read-only repair-mode copy; original unchanged; rendered fidelity requires source review.'
        if row.get('open_and_repair') and warning not in document['warnings']:document['warnings'].append(warning)
    return documents
