from fractions import Fraction

def area(poly):
    return abs(sum((p[0]*q[1]-p[1]*q[0] for p,q in zip(poly,poly[1:]+poly[:1])),Fraction(0)))/2

def clip(poly,a,b,c,positive):
    result=[]
    for p,q in zip(poly,poly[1:]+poly[:1]):
        u=a*p[0]+b*p[1]-c;v=a*q[0]+b*q[1]-c
        pin=u>=0 if positive else u<=0;qin=v>=0 if positive else v<=0
        if pin:result.append(p)
        if pin!=qin:
            t=u/(u-v);result.append((p[0]+t*(q[0]-p[0]),p[1]+t*(q[1]-p[1])))
    clean=[]
    for p in result:
        if not clean or p!=clean[-1]:clean.append(p)
    if len(clean)>1 and clean[0]==clean[-1]:clean.pop()
    return clean if len(clean)>=3 and area(clean)>0 else []

def reflect(p,a,b,c):
    t=2*(a*p[0]+b*p[1]-c)/(a*a+b*b)
    return (p[0]-t*a,p[1]-t*b)

def contains(poly,p):
    signs=[(q[0]-v[0])*(p[1]-v[1])-(q[1]-v[1])*(p[0]-v[0]) for v,q in zip(poly,poly[1:]+poly[:1])]
    return all(v>=0 for v in signs) or all(v<=0 for v in signs)
