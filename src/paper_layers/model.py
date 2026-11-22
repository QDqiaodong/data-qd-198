from .checks import fields,array,ident,unique,number,vector

def parse(data):
    fields(data,('sheets','folds','queries'));sheets=[];folds=[];queries=[]
    for item in array(data['sheets'],1,10):
        fields(item,('id','width','height'));key=ident(item['id']);w=number(item['width']);h=number(item['height'])
        if w<=0 or h<=0:raise ValueError('positive sheet dimensions required')
        sheets.append((key,w,h))
    unique([s[0] for s in sheets])
    for item in array(data['folds'],0,10):
        fields(item,('id','a','b','c'));key=ident(item['id']);a=number(item['a']);b=number(item['b']);c=number(item['c'])
        if a==b==0:raise ValueError('zero fold normal')
        folds.append((key,a,b,c))
    unique([f[0] for f in folds])
    for item in array(data['queries'],0,100):
        fields(item,('id','point'));queries.append((ident(item['id']),vector(item['point'],2)))
    unique([q[0] for q in queries])
    return sheets,folds,queries
