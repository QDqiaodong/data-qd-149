from datetime import datetime
from .common import require,ident,number,rows
from .sla import measure

def timestamp(v):
    if type(v) is not int:raise ValueError('timestamps must be integer UTC seconds')
    return v

def validate_lifecycle(opened,first_response,resolved):
    """Shared incident-lifecycle rule (v1 semantics, reused by schema v4).

    ``first_response`` and ``resolved`` may be ``None`` (still open / not yet
    responded).  When present, integers must be ordered
    ``opened <= first_response <= resolved``; a resolution requires a response.
    The function accepts exactly the four presence combinations v1 always
    accepted or rejected:

    * open and unresolved (both ``None``) -> accepted;
    * responded, unresolved -> accepted iff ``first_response >= opened``;
    * resolved without a response -> rejected;
    * responded and resolved -> accepted iff
      ``opened <= first_response <= resolved``.

    Callers are responsible for integer type validation.  There is no fuzzy or
    interval substitution: comparisons are exact integer ordering.
    """
    if first_response is not None and first_response<opened:
        raise ValueError('invalid lifecycle: response precedes opened')
    if resolved is not None and (first_response is None or resolved<opened or resolved<first_response):
        raise ValueError('invalid lifecycle: resolution without ordered response')
    return True

def analyze(data):
    require(data,'targets incidents pauses');t=data['targets'];require(t,'response_seconds resolution_seconds');targets={'response':number(t['response_seconds'],0,10**12),'resolution':number(t['resolution_seconds'],0,10**12)}
    incidents=[];ids=set()
    for i in rows(data['incidents'],10000):
        require(i,'id opened first_response resolved');iid=ident(i['id']);opened=timestamp(i['opened']);response=None if i['first_response'] is None else timestamp(i['first_response']);resolved=None if i['resolved'] is None else timestamp(i['resolved'])
        if iid in ids:raise ValueError('duplicate incident id')
        validate_lifecycle(opened,response,resolved)
        ids.add(iid);incidents.append({'id':iid,'opened':opened,'first_response':response,'resolved':resolved})
    pauses=[]
    for p in rows(data['pauses'],20000):
        require(p,'incident start end');iid=ident(p['incident']);start=timestamp(p['start']);end=timestamp(p['end'])
        if iid not in ids or end<=start:raise ValueError('unknown incident or invalid pause')
        pauses.append({'incident':iid,'start':start,'end':end})
    return measure(incidents,pauses,targets)
