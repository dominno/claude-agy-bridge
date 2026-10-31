"""Timing measurements; model prose never supplies header facts."""
from __future__ import annotations

from collections import defaultdict
import json
import math
import os
import re
import statistics

from . import events, safety


def read_headers(root, folder):
    rows=[]
    try:
        with safety.directory(folder) as fd:
            names=sorted(n for n in os.listdir(fd) if n.endswith('.md'))
            from .stage import _read_regular
            for name in names:
                text=_read_regular(fd,name,8*1024*1024).decode('utf-8')
                head=re.split(r'^(?:#{2,3} |----\s*$)',text,maxsplit=1,flags=re.M)[0]
                model=re.search(r'^model:\s*(\S+)',head,re.M) or re.search(r'agy-bridge \(([^,]+), read-only\)',head)
                size=re.search(r'(?:^|\| )bytes:\s*(\d+)\s*(?:$|\|)',head,re.M)
                seconds=re.search(r'(?:^|\| )seconds:\s*([\d.]+)\s*(?:$|\|)',head,re.M)
                if model and size and seconds:
                    rows.append(dict(name=name[:-3],model=model[1],bytes=int(size[1]),seconds=float(seconds[1])))
    except FileNotFoundError:
        pass
    return rows


def summarize(points,target=180):
    seconds=sorted(y for x,y in points)
    row=dict(samples=len(points),median=statistics.median(seconds),p90=seconds[math.ceil(.9*len(seconds))-1],intercept=None,slope=None,max_bytes=None)
    if len(points)>=5:
        mx=statistics.mean(x for x,y in points); my=statistics.mean(seconds)
        denom=sum((x-mx)**2 for x,y in points)
        if denom:
            slope=sum((x-mx)*(y-my) for x,y in points)/denom
            if slope>1e-9:
                intercept=my-slope*mx
                row.update(intercept=intercept,slope=slope,max_bytes=max(0,math.floor((target-intercept)/slope)))
    return row


def render(root,folder,target=180):
    groups=defaultdict(list)
    # Events are per-attempt; headers aggregate rounds/retries. Keep populations separate
    # rather than counting a run twice or mixing distinct-file bytes with attempt bytes.
    for text in events.tail(root,None):
        r=json.loads(text)
        if not isinstance(r, dict):
            raise events.EventError('invalid event record')
        if not r.get('dry_run') and r.get('files') and isinstance(r.get('seconds'),(int,float)) and isinstance(r.get('total_bytes'),int):
            groups[(r.get('model','default'),'events')].append((r['total_bytes'],r['seconds']))
    for r in read_headers(root,folder):
        groups[(r['model'],'results')].append((r['bytes'],r['seconds']))
    lines=['| Model | Source | Samples | Median seconds | p90 seconds | Fit seconds | Max bytes for %gs |'%target,
           '| --- | --- | ---: | ---: | ---: | --- | ---: |']
    for (model,source),points in sorted(groups.items()):
        r=summarize(points,target)
        fit='median/p90 fallback' if r['slope'] is None else '%.3f + %.6f * bytes'%(r['intercept'],r['slope'])
        lines.append('| %s | %s | %d | %.3f | %.3f | %s | %s |'%(model,source,r['samples'],r['median'],r['p90'],fit,r['max_bytes'] if r['max_bytes'] is not None else '—'))
    lines.append('\nLinear estimates are not deadlines. Events measure attempts; results count distinct files and the retained answer duration.')
    return '\n'.join(lines)
