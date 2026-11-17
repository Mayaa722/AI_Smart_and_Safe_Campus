"""Remember provider retry windows; cached materials remain available."""
import json
import re
import time
from pathlib import Path
from datetime import datetime,timezone

PATH=Path(__file__).resolve().parent/'run/provider_cooldowns.json'
GROQ_PATH=PATH.with_name('groq_cooldowns.json')

def current(model):
    path=GROQ_PATH if model.startswith('qwen/') else PATH
    data=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    item=data.get(model,{})
    return item if item.get('retry_at',0)>time.time() else None

def message(item):
    when=datetime.fromtimestamp(item['retry_at'],timezone.utc).strftime('%d %b %H:%M UTC')
    provider=item.get('provider','Gemini')
    return provider+' '+item['scope']+' quota is exhausted. The provider requested a wait until '+when+'. Saved materials remain available; this wait does not guarantee a full quota refill.'

def remember_groq(model,headers,description,observed_at=None):
    """Keep actual Retry-After, without credentials or organisation identifiers."""
    observed_at=observed_at or time.time()
    retry=next((v for k,v in headers.items() if k.lower()=='retry-after'),None)
    try:delay=float(retry)
    except (TypeError,ValueError):return None
    if delay<=0:return None
    daily=bool(re.search(r'tokens per day|requests per day|\bTPD\b|\bRPD\b',description,re.I))
    item={'provider':'Groq','scope':'daily free-token/request' if daily else 'rate',
          'retry_at':observed_at+delay,'observed_at':observed_at,'retry_after_seconds':delay}
    data=json.loads(GROQ_PATH.read_text(encoding='utf-8')) if GROQ_PATH.exists() else {}
    data[model]=item;GROQ_PATH.parent.mkdir(parents=True,exist_ok=True)
    temporary=GROQ_PATH.with_suffix('.tmp');temporary.write_text(json.dumps(data,indent=2),encoding='utf-8');temporary.replace(GROQ_PATH)
    return item

def remember(model,error,observed_at=None):
    """Store only sanitized quota metadata, never credentials or payloads."""
    observed_at=observed_at or time.time();details=error.get('details',[])
    description=str(error.get('message',''));delay=None
    for item in details:
        found=re.fullmatch(r'(\d+(?:\.\d+)?)s',str(item.get('retryDelay','')))
        if found:delay=float(found[1])
    if delay is None:
        found=re.search(r'retry in\s+(?:(\d+)h)?(?:(\d+)m)?(\d+(?:\.\d+)?)s',description,re.I)
        if found:delay=3600*int(found[1] or 0)+60*int(found[2] or 0)+float(found[3])
    daily=bool(re.search(r'per day|perday|requestsperday',description+json.dumps(details),re.I))
    if delay is None and daily:return None  # Do not invent a daily reset time.
    delay=delay if delay is not None else 60
    data=json.loads(PATH.read_text(encoding='utf-8')) if PATH.exists() else {}
    value={'scope':'daily free-tier' if daily else 'rate','observed_at':observed_at,
           'retry_at':observed_at+max(1,min(86400,delay))+2,'source':'Google HTTP 429 RetryInfo'}
    data[model]=value;PATH.parent.mkdir(parents=True,exist_ok=True)
    temp=PATH.with_suffix('.tmp');temp.write_text(json.dumps(data,indent=2),encoding='utf-8');temp.replace(PATH)
    return value
