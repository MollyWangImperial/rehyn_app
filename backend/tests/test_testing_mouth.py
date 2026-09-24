import asyncio
import base64
import copy
import hashlib
import json
import os
os.environ.setdefault('MONGO_URL','mongodb://127.0.0.1:27017')
os.environ.setdefault('DB_NAME','rehyn_mouth_tests')
from fastapi.testclient import TestClient
from backend import server, deploy_server, testing_mouth_voice
from backend.assessment_quality import testing_task_report as make_report
from backend.testing_mouth_voice_lines import LINES, STEPS

def task():
    steps=[]
    for rule in server.ASSESSMENT_RUBRICS['T3']['steps']:
        steps.append({'step_id':rule['id'],'completed':True,'duration_ms':2000,'metrics':{'quality':{
            'version':server.ASSESSMENT_QUALITY_VERSION,
            'measurements':{r['metric']:{'value':r['target'],'samples':20,'statistic_samples':10,'statistic_source':'sample_proportion' if r['metric']=='target_control' else 'target_median','series':[{'elapsed_ms':i*100,'value':r['target'],'in_target':True} for i in range(20)]} for r in rule['criteria']},
            'compensations':{key:{'eligible_ms':1800,'max_value':0,'max_streak_ms':0} for key in rule['compensations']}}}})
    return {'task_id':'T3','steps':steps,'metrics':{}}

def test_rubric_preserved_and_explanations_have_exact_evidence():
    rows=server.ASSESSMENT_RUBRICS['T3']['steps']
    assert [(r['criteria'][0]['metric'],r['criteria'][0]['target']) for r in rows]==[('arm_elevation',30),('elbow_flexion',110),('elbow_flexion',110),('target_control',.8)]
    result=make_report(task(),server.ASSESSMENT_RUBRICS)['task']
    assert result['score']==100
    assert result['steps'][1]['criteria'][0]['statistic_samples']==10
    assert len(result['steps'][1]['criteria'][0]['series'])==20
    assert result['steps'][1]['compensations'][-1]['unit']=='proxy'

def test_proportional_form_assistance_and_missing_evidence():
    t=task();q=t['steps'][1]['metrics']['quality']
    q['measurements']['elbow_flexion']['value']=55
    q['compensations']['head_drop'].update(max_value=18,max_streak_ms=600)
    result=make_report(t,server.ASSESSMENT_RUBRICS)['task']
    assert result['steps'][1]['score']==48 # (20+40)*.8, not 60-20
    assert result['score']==87
    t['metrics']['assisted']=True
    assert make_report(t,server.ASSESSMENT_RUBRICS)['task']['score']==43.5
    q['measurements']['elbow_flexion']['samples']=4
    assert make_report(t,server.ASSESSMENT_RUBRICS)['task']['score'] is None

def test_return_is_target_control_and_short_compensation_does_not_penalize():
    t=task();q=t['steps'][3]['metrics']['quality']
    q['measurements']['target_control']['value']=.4
    q['compensations']['head_drop'].update(max_value=40,max_streak_ms=499)
    assert make_report(t,server.ASSESSMENT_RUBRICS)['task']['steps'][3]['score']==60

def test_route_and_private_voice_complete_coverage(tmp_path,monkeypatch):
    path=tmp_path/'mouth.json'
    audio=base64.b64encode(b'ID3'+b'a'*1100).decode()
    entries={hashlib.sha256(line.encode()).hexdigest():audio for line in LINES}
    path.write_text(json.dumps({'version':1,'voice':'Molly','entries':entries}))
    monkeypatch.setenv('TESTING_MOUTH_VOICE_BUNDLE',str(path));testing_mouth_voice.bundle.cache_clear()
    async def user(_):return {'id':'mouth-qa'}
    monkeypatch.setattr(server,'_task_video_user',user)
    try:
        with TestClient(deploy_server.app) as client:
            assert 'start_task=T3' in client.get('/testing/hand-to-mouth',follow_redirects=False).headers['location']
            assert client.get('/api/testing/mouth/voice/health').json()['available_cues']==len(LINES)
            for line in LINES:assert client.post('/api/testing/mouth/voice',json={'text':line}).json()['audio_b64']==audio
            assert client.post('/api/testing/mouth/voice',json={'text':'arbitrary'}).status_code==403
            async def no_user(_):return None
            monkeypatch.setattr(server,'_task_video_user',no_user)
            assert client.post('/api/testing/mouth/voice',json={'text':LINES[0]}).status_code==401
        entries.pop(next(iter(entries)));path.write_text(json.dumps({'version':1,'voice':'Molly','entries':entries}))
        testing_mouth_voice.bundle.cache_clear()
        assert not asyncio.run(testing_mouth_voice.health())['ready']
    finally:testing_mouth_voice.bundle.cache_clear()

def test_runtime_loads_t3_voice_and_diagnostics():
    html=server.POSE_RUNNER_HTML
    assert 'function testingMouthEnabled()' in html
    assert 'function drawTestingMouth(' in html
    assert STEPS[2] in html
    assert 'if(testingMouthEnabled()) return mouthSay(text)' in html
