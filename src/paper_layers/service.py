from .model import parse
from .folding import fold_stack,query

def process(data):
    sheets,folds,queries=parse(data);layers,total,history=fold_stack(sheets,folds)
    output=[{'id':key,'hits':query(layers,p)} for key,p in queries]
    for r in output:r['top']=r['hits'][-1] if r['hits'] else None
    return {'layers':[{'sheet':r['sheet'],'path':r['path'],'face':r['face'],'polygon':[[str(v) for v in p] for p in r['poly']]} for r in layers],'queries':output,'fold_history':history,'summary':{'layers':len(layers),'material_area':str(total),'queries':len(queries)}}
