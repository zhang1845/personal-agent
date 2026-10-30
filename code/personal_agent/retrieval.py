"""Local lexical retrieval with reproducible chunk offsets; no model downloads."""
from collections import Counter
import hashlib
import math
import re

REVISION = 'chunks-bm25-v1'
BASELINE = 'weighted-keyword-v1'
STOP = {'什么','怎么','如何','我的','资料','项目','哪些','关于','一下','请问','查找','搜索','相关'}


def tokens(text):
    value = text.casefold()
    out = re.findall(r'[a-z0-9_]+', value)
    for run in re.findall(r'[\u3400-\u9fff]+', value):
        out.extend([run] if len(run)<2 else [run[i:i+2] for i in range(len(run)-1)])
    return [t for t in out if t not in STOP]


def chunks(content, size=600, overlap=100):
    start = 0
    while start < len(content):
        end = min(start+size, len(content))
        if end < len(content):
            boundary = max(content.rfind('\n',start+size//2,end),content.rfind('。',start+size//2,end))
            if boundary >= 0: end = boundary+1
        yield start, end, content[start:end]
        if end == len(content): break
        start = max(start+1,end-overlap)


def fingerprint(rows):
    values = sorted((r['id'],r['version'],r.get('project_id'),r['title'],r['content'],r['reflection']) for r in rows)
    return hashlib.sha256(repr(values).encode('utf-8')).hexdigest()


def prepare(rows):
    index=[];frequency=Counter()
    for row in rows:
        for number,(start,end,body) in enumerate(chunks(row['content']),1):
            counts=Counter(tokens(body)+tokens(row['title'])*2)
            index.append((row,number,start,end,body,counts,sum(counts.values())))
            frequency.update(counts.keys())
    return index,frequency,sum(x[-1] for x in index)/max(1,len(index))


def rank(rows, query, limit, method=REVISION, prepared=None):
    from .knowledge import terms, excerpt
    keys = terms(query)
    if method == BASELINE:
        result=[]
        for row in rows:
            score=sum(6*(k in row['title'].casefold())+2*(k in row['content'].casefold())+(k in row['reflection'].casefold()) for k in keys)
            if keys and not score or query.strip() and not keys: continue
            value=dict(row,score=score,excerpt=excerpt(row['content'],query),reflection_excerpt=excerpt(row['reflection'],query,220))
            value.pop('content');value.pop('reflection');result.append(value)
        return sorted(result,key=lambda r:-r['score'])[:limit],len(result)
    if not query.strip():
        result=[]
        for row in rows:
            value=dict(row,score=0,excerpt=excerpt(row['content'],''),reflection_excerpt=excerpt(row['reflection'],'',220))
            value.pop('content');value.pop('reflection');result.append(value)
        return result[:limit],len(result)
    if not keys:return [],0
    index,frequency,average=prepared if prepared is not None else prepare(rows)
    found={}
    for row,number,start,end,body,counts,length in index:
        score=0
        for key in keys:
            tf=counts[key]
            if tf:
                idf=math.log(1+(len(index)-frequency[key]+.5)/(frequency[key]+.5))
                score+=idf*tf*2.2/(tf+1.2*(.25+.75*length/max(average,1)))
        # Personal reflections are separate context, never presented as a source quote.
        score+=sum(.1 for key in keys if key in row['reflection'].casefold())
        if score<=0:continue
        if row['id'] in found and found[row['id']]['score']>=score:continue
        value=dict(row,score=score,excerpt=body,reflection_excerpt=excerpt(row['reflection'],query,220),
                   chunk={'id':f"{row['id']}:{row['version']}:{start}:{end}", 'number':number,'start':start,'end':end})
        value.pop('content');value.pop('reflection');found[row['id']]=value
    result=sorted(found.values(),key=lambda r:-r['score'])
    return result[:limit],len(result)
