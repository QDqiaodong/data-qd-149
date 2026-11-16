def union(intervals):
    total=0;left=right=None
    for a,b in sorted(intervals):
        if left is None:left,right=a,b
        elif a<=right:right=max(right,b)
        else:total+=right-left;left,right=a,b
    return total+(right-left if left is not None else 0)

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
