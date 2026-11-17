"""Restore portable checkpoints locally. No API requests or key transfers."""
import hashlib,json,shutil,zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parent
APP=ROOT/'SmartCampus/vscode'

def relocate(value,prefixes):
 if isinstance(value,dict):return {k:relocate(v,prefixes) for k,v in value.items()}
 if isinstance(value,list):return [relocate(v,prefixes) for v in value]
 if isinstance(value,str):
  normalized=value.replace('\\','/')
  current=str(ROOT).replace('\\','/')
  if normalized.casefold()==current.casefold() or normalized.casefold().startswith(current.casefold()+'/'):return value
  for old,new in prefixes:
   if normalized.casefold().startswith(old.casefold()+'/'):
    return str(Path(new)/normalized[len(old)+1:])
 return value

def prepare():
 manifest=json.loads((ROOT/'checkpoints/manifest.json').read_text(encoding='utf-8'))
 marker=APP/'.handoff_state.json'
 previous=json.loads(marker.read_text(encoding='utf-8')) if marker.exists() else None
 if previous is None:
  for part in manifest['parts']:
   archive=ROOT/'checkpoints'/part['file']
   if hashlib.sha256(archive.read_bytes()).hexdigest()!=part['sha256']:raise ValueError('Checkpoint archive checksum mismatch: '+part['file'])
   with zipfile.ZipFile(archive) as z:
    for member in z.infolist():
     path=(ROOT/member.filename).resolve()
     if not path.is_relative_to(ROOT) or member.is_dir():raise ValueError('Unsafe checkpoint path')
     payload=z.read(member)
     if hashlib.sha256(payload).hexdigest()!=manifest['files'][member.filename]:raise ValueError('Checkpoint member checksum mismatch')
     # Never overwrite later work if setup was interrupted or the checkout has
     # already been used without a marker.
     if not path.exists():path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(payload)
 prefixes=[(manifest['source_root'].replace('\\','/'),str(ROOT))]
 if previous and previous.get('root')!=str(ROOT):prefixes.insert(0,(previous['root'].replace('\\','/'),str(ROOT)))
 operational=[]
 for pattern in ['run/uploads/**/config.json','run/uploads/**/document.json','run/exports/sources.json',
   'run/rendered_slides/exports.json','run/rendered_slides/validation.json',
   'run/lecture_comparison_v3/configs/*.json','run/lecture_comparison_v3/judge_configs/*.json',
   'run/lecture_comparison_v3/manifest.json','run/comparison_v2/corpus.json']:
  operational.extend(APP.glob(pattern))
 for p in set(operational):
  old=json.loads(p.read_text(encoding='utf-8-sig'));new=relocate(old,prefixes)
  if new!=old:p.write_text(json.dumps(new,indent=2,ensure_ascii=False),encoding='utf-8')
 for p in (APP/'run/full_lectures').glob('*/progress.json'):
  data=json.loads(p.read_text(encoding='utf-8'))
  if 'configuration' in data:
   config=relocate(data['configuration'],prefixes)
   if config!=data['configuration']:
    data['configuration']=config;p.write_text(json.dumps(data,indent=2,ensure_ascii=False),encoding='utf-8')
 # Historical raw prompts/records are not rewritten: their original paths and
 # hashes are provenance, not operational file locations.
 env=APP/'.env'
 if not env.exists():shutil.copy2(APP/'.env.example',env)
 marker.write_text(json.dumps({'root':str(ROOT),'restored_parts':[p['file'] for p in manifest['parts']]},indent=2),encoding='utf-8')
 return APP

if __name__=='__main__':print('Ready:',prepare())
