"""Half-open interval union primitives shared by every schema version.

Intervals are half-open [start, end) integer-second spans.  ``merge`` is the
shared canonicalizer: it returns the sorted, pairwise disjoint, non-empty
intervals covering exactly the same time set as its input, so callers can
inspect the outage shape itself instead of only its length.  ``union`` keeps
the original numeric interface (total covered seconds) for the v1/v2
consumers; the v3 fault-impact engine reuses ``merge`` directly so impact
intervals stay auditable and are never expanded per second.
"""

def merge(intervals):
    """Return the canonical sorted disjoint half-open intervals.

    Overlapping or touching inputs coalesce; empty or inverted spans
    contribute nothing.  The output covers exactly the same seconds as the
    valid spans of the input, which keeps ``union`` below numerically
    identical to its previous implementation for every well-formed input.
    """
    merged=[]
    for a,b in sorted(intervals):
        if b<=a:continue
        if merged and a<=merged[-1][1]:
            if b>merged[-1][1]:merged[-1][1]=b
        else:merged.append([a,b])
    return [(a,b) for a,b in merged]

def union(intervals):
    """Total seconds covered by ``intervals`` with overlaps counted once."""
    return sum(b-a for a,b in merge(intervals))

def measure(incidents,pauses,targets):
    by_id={i['id']:[] for i in incidents}
    for p in pauses:by_id[p['incident']].append((p['start'],p['end']))
    output=[]
    for i in sorted(incidents,key=lambda x:x['id']):
        row={'id':i['id']}
        for key,finish in [('response',i['first_response']),('resolution',i['resolved'])]:
            if finish is None:row[key+'_active_seconds']=None;row[key+'_met']=None;continue
            paused=union([(max(i['opened'],a),min(finish,b)) for a,b in by_id[i['id']] if max(i['opened'],a)<min(finish,b)])
            elapsed=finish-i['opened']-paused;row[key+'_active_seconds']=elapsed;row[key+'_paused_seconds']=paused;row[key+'_met']=elapsed<=targets[key]
        output.append(row)
    return {'incidents':output}
