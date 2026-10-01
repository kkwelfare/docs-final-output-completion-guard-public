"""Logs from the real generated-artifact route, mocked transport only."""
import importlib.util,sys,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location('existing_docs_fixture',ROOT/'tests/test_generated_artifact_jev_minimum_once.py')
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
from quality_rules import jev_post_render as jev,generated_artifact_jev as route
import pytest

@pytest.mark.parametrize('case,expected,reason',[('pass','accepted','ok'),('malformed','unresolved','malformed_response'),('timeout','unresolved','timeout')])
def test_real_route_events(tmp_path,monkeypatch,caplog,case,expected,reason):
 caplog.set_level(20,logger='hermes_plugins.jev_lifecycle.docs')
 fixture.test_zero_candidate_transport_outcomes_and_no_retry(tmp_path,monkeypatch,case,expected,reason)
 rows=[r.getMessage() for r in caplog.records if 'jev.lifecycle' in r.getMessage()]
 ids={x.split('request_id=')[1].split()[0] for x in rows}
 # The existing pass fixture intentionally changes reviewer identity and calls again.
 assert len(ids)==(2 if case=='pass' else 1)
 first_id=rows[0].split('request_id=')[1].split()[0]
 events=[x.split('event=')[1].split()[0] for x in rows if x.split('request_id=')[1].split()[0]==first_id]
 assert events.count('provider_start')==1
 assert events.count('delivered')==1
 assert not any('Private body' in x for x in rows)
 if case=='pass': assert events==['provider_start','transport_response','validated_response','delivered']
 elif case=='timeout': assert events==['provider_start','provider_failure','evaluation_failure','delivered']
 else: assert events==['provider_start','transport_response','evaluation_failure','delivered']
