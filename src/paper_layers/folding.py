from fractions import Fraction
from .geometry import area,clip,reflect,contains

def fold_stack(sheets,folds):
    zero=Fraction(0)
    layers=[{'sheet':key,'path':[],'face':'front','poly':[(zero,zero),(w,zero),(w,h),(zero,h)],'reflections':[]} for key,w,h in sheets]
    initial=sum((area(r['poly']) for r in layers),zero);history=[]
    for key,a,b,c in folds:
        stationary=[];moving=[]
        for r in layers:
            keep=clip(r['poly'],a,b,c,False);turn=clip(r['poly'],a,b,c,True)
            if keep:stationary.append({**r,'poly':keep,'path':r['path']+[key+':stay']})
            if turn:moving.append({**r,'poly':[reflect(p,a,b,c) for p in turn],'path':r['path']+[key+':move'],'face':'back' if r['face']=='front' else 'front','reflections':r['reflections']+[(a,b,c)]})
        layers=stationary+list(reversed(moving))
        if len(layers)>4096:raise ValueError('layer limit exceeded')
        total=sum((area(r['poly']) for r in layers),zero)
        if total!=initial:raise ValueError('area conservation failed')
        history.append({'fold':key,'layers':len(layers),'material_area':str(total)})
    return layers,initial,history

def query(layers,point):
    hits=[]
    for index,r in enumerate(layers):
        if not contains(r['poly'],point):continue
        source=point
        for a,b,c in reversed(r['reflections']):source=reflect(source,a,b,c)
        hits.append({'layer':index,'sheet':r['sheet'],'face':r['face'],'source_point':[str(v) for v in source],'path':r['path']})
    return hits
