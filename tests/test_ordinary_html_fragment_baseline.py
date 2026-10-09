import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from quality_rules import ordinary_html_fragment_baseline as v

def h(p):return {"path":str(p.resolve()),"sha256":v.sha256(p)}
def fixture(d):
 d.mkdir(parents=True,exist_ok=True)
 source=d/'source.html';source.write_text('<p>Before.</p><figure><img src="https://e.test/a.png" alt="a"></figure>',encoding='utf8')
 target=d/'target.html';target.write_text('<p>After.</p><figure><img src="https://e.test/a.png" alt="a"></figure>',encoding='utf8')
 report=d/'changes.json';report.write_text(json.dumps({"task_id":"t_fixture_fragment_001","source_sha256":v.sha256(source),"revised_sha256":v.sha256(target),"changes":[{"before":"Before.","after":"After."}]}))
 identity=d/'identity.json';identity.write_text(json.dumps({"task_id":"t_fixture_fragment_001","run_id":1,"target_id":1536}))
 live=d/'live.json';live.write_text(json.dumps({"id":1536,"status":"draft","content":{"raw":target.read_text()}}))
 receipt=d/'output.json';receipt.write_text(json.dumps({"schema_version":"docs-final-output-receipt-1","status":"pass","entries":[{"artifact_path":str(target.resolve()),"artifact_sha256":v.sha256(target),"status":"pass"}]}))
 contract=d/'contract.json';contract.write_text(json.dumps({"schema_version":v.SCHEMA_VERSION,"route":"cms_prose_fragment","artifact_kind":"cms-prose-fragment","editorial_scope":"text-nodes-only","source":h(source),"target":h(target),"change_evidence":h(report),"source_identity":{"task_id":"t_fixture_fragment_001","run_id":1,"target_id":1536},"receiver_readback":h(live),"final_output_receipts":[h(receipt)]}))
 return contract,source,target,report,identity,live,receipt

def test_pass_and_delivery_exclusion(tmp_path):
 c,*_=fixture(tmp_path);assert v.validate(c,[],trusted_task_id="t_fixture_fragment_001",trusted_run_id=1) is None;assert v.validate(c,[str(tmp_path/'target.html')])=="CMS prose-fragment route does not deliver a standalone HTML artifact"
def test_markup_drift_rejected(tmp_path):
 c,s,t,_,_,live,receipt=fixture(tmp_path);t.write_text('<p>After.</p><figure><img src="https://evil.test/a.png" alt="a"></figure>')
 value=json.loads(c.read_text());value['target']=h(t);value['receiver_readback']=h(live);value['final_output_receipts']=[h(receipt)]
 live_value=json.loads(live.read_text());live_value['content']['raw']=t.read_text();live.write_text(json.dumps(live_value));value['receiver_readback']=h(live)
 output=json.loads(receipt.read_text());output['entries'][0]['artifact_sha256']=v.sha256(t);receipt.write_text(json.dumps(output));value['final_output_receipts']=[h(receipt)];c.write_text(json.dumps(value))
 assert v.validate(c,[],trusted_task_id="t_fixture_fragment_001",trusted_run_id=1)=="source and target markup, attributes, URLs, or media differ"
def test_interactive_and_unbalanced_rejected(tmp_path):
 c,s,*_=fixture(tmp_path);s.write_text('<html><body><script>x()</script></body></html>');value=json.loads(c.read_text());value['source']=h(s);c.write_text(json.dumps(value));assert v.validate(c,[],trusted_task_id="t_fixture_fragment_001",trusted_run_id=1)=="fragment contains disallowed html element"
 c,s,*_=fixture(tmp_path/'other');s.write_text('<p>Before.');value=json.loads(c.read_text());value['source']=h(s);c.write_text(json.dumps(value));assert 'unclosed tags' in v.validate(c,[],trusted_task_id="t_fixture_fragment_001",trusted_run_id=1)
def test_identity_receiver_and_final_output_fail_closed(tmp_path):
 c,*_=fixture(tmp_path);value=json.loads(c.read_text());value['source_identity']['run_id']=548;c.write_text(json.dumps(value));assert v.validate(c,[],trusted_task_id="t_fixture_fragment_001",trusted_run_id=1)=="trusted source run identity mismatch"
 c,*_=fixture(tmp_path/'other');value=json.loads(c.read_text());live=json.loads((tmp_path/'other/live.json').read_text());live['content']['raw']='mismatch';(tmp_path/'other/live.json').write_text(json.dumps(live));value['receiver_readback']=h(tmp_path/'other/live.json');c.write_text(json.dumps(value));assert v.validate(c,[],trusted_task_id="t_fixture_fragment_001",trusted_run_id=1)=="actual receiver readback does not exactly match target draft content"
 c,*_=fixture(tmp_path/'third');value=json.loads(c.read_text());value['final_output_receipts']=[];c.write_text(json.dumps(value));assert v.validate(c,[],trusted_task_id="t_fixture_fragment_001",trusted_run_id=1)=="canonical final-output receipt handle is missing"
