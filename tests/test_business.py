
import copy,json,unittest,tempfile,subprocess,sys,hashlib,random
from pathlib import Path
from fractions import Fraction
from incident_sla.model import analyze
ROOT=Path(__file__).resolve().parents[1]
def sample():return json.loads((ROOT/'fixtures/input.json').read_text())
class Business(unittest.TestCase):
    def bad(self,d):
        with self.assertRaises((ValueError,TypeError)):analyze(d)
    def test_unknown_field(self):
        d=sample();d['extra']=1;self.bad(d)

    def test_pause_union(self):
        r=analyze(sample())['incidents'][0];self.assertEqual((r['response_active_seconds'],r['response_paused_seconds'],r['response_met']),(100,300,True));self.assertEqual((r['resolution_active_seconds'],r['resolution_paused_seconds'],r['resolution_met']),(3500,500,True))
    def test_open_incident(self):
        r=analyze(sample())['incidents'][1];self.assertIsNone(r['resolution_active_seconds']);self.assertTrue(r['response_met'])
    def test_pause_clipping(self):
        d=sample();d['pauses']=[{'incident':'I-1','start':900,'end':1200}];r=analyze(d)['incidents'][0];self.assertEqual(r['response_paused_seconds'],200);self.assertEqual(r['response_active_seconds'],200)
    def test_boundary_met_inclusive(self):
        d=sample();d['pauses']=[];d['targets']['response_seconds']=400;self.assertTrue(analyze(d)['incidents'][0]['response_met'])
    def test_inverted_lifecycle(self):
        d=sample();d['incidents'][0]['resolved']=1300;self.bad(d)
    def test_response_required_for_resolution(self):
        d=sample();d['incidents'][0]['first_response']=None;self.bad(d)
    def test_unknown_pause(self):
        d=sample();d['pauses'][0]['incident']='missing';self.bad(d)
    def test_duplicate_incident(self):
        d=sample();d['incidents'].append(d['incidents'][0]);self.bad(d)

class Command(unittest.TestCase):
    def call(self,source,output):
        return subprocess.run([sys.executable,'-m','incident_sla.cli','--source',str(source),'--output',str(output)],capture_output=True,text=True)
    def test_cli_replay(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'out.json';src=ROOT/'fixtures/input.json';r=self.call(src,out);self.assertEqual(r.returncode,0,r.stderr);first=out.read_bytes();self.assertEqual(json.loads(first)['source_sha256'],hashlib.sha256(src.read_bytes()).hexdigest());self.assertEqual(self.call(src,out).returncode,0);self.assertEqual(first,out.read_bytes())
    def test_failure_preserves_output(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'bad.json';out=Path(td)/'out.json';out.write_text('prior')
            for body in ['{}','{"a":1,"a":2}','NaN']:
                src.write_text(body);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(out.read_text(),'prior')
    def test_output_safety(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'in.json';src.write_bytes((ROOT/'fixtures/input.json').read_bytes());before=src.read_bytes();self.assertEqual(self.call(src,src).returncode,2);self.assertEqual(src.read_bytes(),before);out=Path(td)/'link.json';out.symlink_to(src);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(src.read_bytes(),before)
