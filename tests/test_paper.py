import copy,json,unittest
from pathlib import Path
from paper_layers.service import process

class PaperTests(unittest.TestCase):
    def setUp(self):self.data=json.loads(Path('fixtures/input.json').read_text())
    def test_half_and_provenance(self):
        r=process(self.data);self.assertEqual(r['summary'],{'layers':2,'material_area':'8','queries':3})
        top=r['queries'][0]['top'];self.assertEqual(top['face'],'back');self.assertEqual(top['source_point'],['3','1'])
        self.assertEqual(r['queries'][1]['hits'],[])
    def test_second_fold(self):
        self.data['folds'].append({'id':'quarter','a':'0','b':'1','c':'1'})
        self.data['queries']=[{'id':'p','point':['1','0.5']}]
        r=process(self.data);self.assertEqual(len(r['queries'][0]['hits']),4)
        hits=r['queries'][0]['hits']
        self.assertEqual([h['face'] for h in hits],['front','back','front','back'])
        self.assertEqual([h['source_point'] for h in hits],[['1','1/2'],['3','1/2'],['3','3/2'],['1','3/2']])
        self.assertEqual([h['material_area'] for h in r['fold_history']],['8','8'])
    def test_closed_crease(self):self.assertEqual(len(process(self.data)['queries'][2]['hits']),2)
    def test_noop_and_whole_stack_order(self):
        self.data['folds'][0]['c']='5';self.assertEqual(process(self.data)['summary']['layers'],1)
        self.data['sheets'].append({'id':'upper','width':'4','height':'2'});self.data['folds'][0]['c']='-1'
        self.data['queries']=[{'id':'p','point':['-3','1']}]
        r=process(self.data);self.assertEqual([x['sheet'] for x in r['queries'][0]['hits']],['upper','paper'])
    def test_oblique_area(self):
        self.data['folds'][0].update(a='1',b='1',c='2')
        self.assertEqual(process(self.data)['summary']['material_area'],'8')
    def test_invalid_normal_and_dimension(self):
        for field in ('normal','width'):
            d=copy.deepcopy(self.data)
            if field=='normal':d['folds'][0].update(a='0',b='0')
            else:d['sheets'][0]['width']='0'
            with self.assertRaises(ValueError):process(d)
