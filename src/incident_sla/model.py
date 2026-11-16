from datetime import datetime
from .common import require,ident,number,rows
from .sla import measure

def timestamp(v):
    if type(v) is not int:raise ValueError('timestamps must be integer UTC seconds')
    return v

def analyze(data):
    require(data,'targets incidents pauses');t=data['targets'];require(t,'response_seconds resolution_seconds');targets={'response':number(t['response_seconds'],0,10**12),'resolution':number(t['resolution_seconds'],0,10**12)}
    incidents=[];ids=set()
    for i in rows(data['incidents'],10000):
        require(i,'id opened first_response resolved');iid=ident(i['id']);opened=timestamp(i['opened']);response=None if i['first_response'] is None else timestamp(i['first_response']);resolved=None if i['resolved'] is None else timestamp(i['resolved'])
        if iid in ids or (response is not None and response<opened) or (resolved is not None and (resolved<opened or response is None or resolved<response)):raise ValueError('duplicate incident or invalid lifecycle')
        ids.add(iid);incidents.append({'id':iid,'opened':opened,'first_response':response,'resolved':resolved})
    pauses=[]
    for p in rows(data['pauses'],20000):
        require(p,'incident start end');iid=ident(p['incident']);start=timestamp(p['start']);end=timestamp(p['end'])
        if iid not in ids or end<=start:raise ValueError('unknown incident or invalid pause')
        pauses.append({'incident':iid,'start':start,'end':end})
    return measure(incidents,pauses,targets)
