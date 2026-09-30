import asyncio
import base64
import copy
import hashlib
import json
import os
import pytest
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
            'compensations':{key:{'eligible_ms':1800,'max_value':0,'max_streak_ms':0} for key in ['trunk_lean','shoulder_hike','head_drop']}}}})
    return {'task_id':'T3','steps':steps,'metrics':{}}

def test_movement_rubric_and_completion_only_return_have_exact_evidence():
    rows=server.ASSESSMENT_RUBRICS['T3']['steps']
    assert [(r['criteria'][0]['metric'],r['criteria'][0]['target']) for r in rows[:3]]==[('arm_elevation',30),('elbow_flexion',110),('elbow_flexion',110)]
    assert rows[3]['scoring_method']=='target_completion'
    assert rows[3]['criteria']==[] and rows[3]['compensations']==[]
    result=make_report(task(),server.ASSESSMENT_RUBRICS)['task']
    assert result['score']==100
    assert result['steps'][1]['criteria'][0]['statistic_samples']==10
    assert len(result['steps'][1]['criteria'][0]['series'])==20
    assert result['steps'][1]['compensations'][-1]['unit']=='proxy'

def test_testing_uses_completion_and_compensation_not_angle_points_or_assistance():
    t=task();q=t['steps'][1]['metrics']['quality']
    q['measurements']['elbow_flexion']['value']=55
    t['metrics']['assisted']=True
    result=make_report(t,server.ASSESSMENT_RUBRICS)['task']
    assert result['score']==result['earned_score']==100
    assert result['earned_module_points']==100
    assert result['scoring_method']=='completion_compensation_v1'
    assert result['score_reason']=='completed_without_compensation'
    # Diagnostic step points remain available, but are not averaged into total.
    assert result['steps'][1]['score']==30
    q['measurements']['elbow_flexion']['samples']=0
    assert make_report(t,server.ASSESSMENT_RUBRICS)['task']['score']==100
    q['compensations']['head_drop'].update(max_value=18,max_streak_ms=600)
    result=make_report(t,server.ASSESSMENT_RUBRICS)['task']
    assert result['score']==result['earned_score']==15
    assert result['earned_module_points']==15
    assert result['score_reason']=='compensation_detected'


@pytest.mark.parametrize('step_index',range(4))
@pytest.mark.parametrize('pattern',['trunk_lean','shoulder_hike','head_drop'])
def test_any_sustained_pattern_in_any_active_step_sets_total_to_15(step_index,pattern):
    attempt=task()
    check=attempt['steps'][step_index]['metrics']['quality']['compensations'][pattern]
    check.update(max_value=40,max_streak_ms=499)
    assert make_report(attempt,server.ASSESSMENT_RUBRICS)['task']['score']==100
    check['max_streak_ms']=500
    report=make_report(attempt,server.ASSESSMENT_RUBRICS)['task']
    assert report['score']==15
    assert report['compensation_override']['detected'][0]['id']==pattern
    assert report['steps'][3]['score']==100


def test_incomplete_attempt_is_15_but_missing_posture_is_not_assumed_normal():
    attempt=task()
    attempt['steps'][1]['metrics']['quality']['compensations']['head_drop']['eligible_ms']=400
    report=make_report(attempt,server.ASSESSMENT_RUBRICS)['task']
    assert report['score'] is None
    assert report['score_reason']=='insufficient_evidence'
    attempt['steps'][0]['completed']=False
    assert make_report(attempt,server.ASSESSMENT_RUBRICS)['task']['score']==15
    attempt['steps'].pop(0)
    report=make_report(attempt,server.ASSESSMENT_RUBRICS)['task']
    assert report['score']==15 and report['score_reason']=='incomplete'


def test_normal_assessment_still_uses_its_existing_angle_rubric():
    from backend.assessment_quality import score_assessment
    attempt=task()
    attempt['steps'][1]['metrics']['quality']['measurements']['elbow_flexion']['value']=55
    assert score_assessment([attempt],server.ASSESSMENT_RUBRICS)['tasks'][0]['score']==90

def test_return_completion_earns_100_despite_low_occupancy_compensation_or_assistance():
    t=task();q=t['steps'][3]['metrics']['quality']
    q['measurements']['target_control']={'value':.1,'samples':20}
    q['compensations']['head_drop']={'eligible_ms':1800,'max_value':40,'max_streak_ms':1200}
    t['metrics']['assisted']=True
    step=make_report(t,server.ASSESSMENT_RUBRICS)['task']['steps'][3]
    assert step['score']==100
    assert step['calculation']=={'completion_points':100,'range_points':0,
        'detected_compensations':1,'form_factor':1,'assistance_factor':1}
    t['steps'][3]['metrics']={}
    assert make_report(t,server.ASSESSMENT_RUBRICS)['task']['steps'][3]['score']==100
    t['steps'][3]['completed']=False
    assert make_report(t,server.ASSESSMENT_RUBRICS)['task']['steps'][3]['score']==0
    t['steps'].pop()
    assert make_report(t,server.ASSESSMENT_RUBRICS)['task']['steps'][3]['score'] is None

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


@pytest.mark.parametrize('method',['pelvis_normalized_shoulder_or_face_v1','pelvis_axis_shoulder_or_face_v2'])
def test_t3_face_only_lean_between_seven_and_twelve_counts_and_records_method(method):
    attempt = task()
    cue = attempt['steps'][1]['metrics']['quality']['compensations']['trunk_lean']
    cue.update(method=method, max_value=8,
               max_streak_ms=600, face_peak=8, shoulder_peak=0,
               cue_evidence={'face': {'duration_ms': 600, 'peak': 8},
                             'shoulder': {'duration_ms': 0, 'peak': None}})
    step = make_report(attempt, server.ASSESSMENT_RUBRICS)['task']['steps'][1]
    assert step['score'] == 80
    assert step['compensations'][0]['status'] == 'detected'
    assert step['compensations'][0]['method'] == method
    assert step['compensations'][0]['confirmed_cues']['face']['threshold'] == 7
    cue['cue_evidence']['face']['duration_ms'] = 400
    assert make_report(attempt, server.ASSESSMENT_RUBRICS)['task']['steps'][1]['score'] == 100


def test_hike_only_is_not_labelled_trunk_lean_and_still_scores_15():
    attempt=task()
    checks=attempt['steps'][1]['metrics']['quality']['compensations']
    checks['shoulder_hike'].update(method='world_shoulder_rise_v2',max_value=25,max_streak_ms=600)
    checks['trunk_lean'].update(method='pelvis_axis_shoulder_or_face_v2',max_value=0,
        cue_evidence={'shoulder':{'duration_ms':0,'peak':None},'face':{'duration_ms':0,'peak':None}})
    report=make_report(attempt,server.ASSESSMENT_RUBRICS)['task']
    assert report['score']==15
    assert [c['id'] for c in report['compensation_override']['detected']]==['shoulder_hike']
    assert next(c for c in report['steps'][1]['compensations'] if c['id']=='shoulder_hike')['method']=='world_shoulder_rise_v2'


def test_nodding_after_shoulder_hiking_records_both_with_the_same_15_score():
    attempt=task()
    checks=attempt['steps'][1]['metrics']['quality']['compensations']
    checks['shoulder_hike'].update(method='world_shoulder_rise_v2',max_value=25,max_streak_ms=600)
    checks['head_drop'].update(method='image_head_nod_v3',max_value=20,max_streak_ms=600)
    report=make_report(attempt,server.ASSESSMENT_RUBRICS)['task']
    assert report['score']==15
    assert {c['id'] for c in report['compensation_override']['detected']}=={'shoulder_hike','head_drop'}
    head=next(c for c in report['steps'][1]['compensations'] if c['id']=='head_drop')
    assert head['method']=='image_head_nod_v3'
    assert head['label']=='Head nodding'
    checks['head_drop']['max_streak_ms']=400
    report=make_report(attempt,server.ASSESSMENT_RUBRICS)['task']
    assert [c['id'] for c in report['compensation_override']['detected']]==['shoulder_hike']


@pytest.mark.parametrize('trunk,head,expected', [(0,20,{'head_drop'}),(20,0,{'trunk_lean'}),(20,20,{'head_drop','trunk_lean'}),(12,10,set())])
def test_size_cues_have_separate_thresholds_labels_and_percent_units(trunk,head,expected):
    attempt=task()
    checks=attempt['steps'][1]['metrics']['quality']['compensations']
    checks['trunk_lean'].update(method='image_shoulder_expansion_v3',max_value=trunk,max_streak_ms=600,
        cue_evidence={'face':{'duration_ms':600,'peak':30}})
    checks['head_drop'].update(method='image_face_expansion_v4',max_value=head,max_streak_ms=600)
    report=make_report(attempt,server.ASSESSMENT_RUBRICS)['task']
    assert report['score']==(15 if expected else 100)
    assert {c['id'] for c in (report.get('compensation_override') or {}).get('detected',[])}==expected
    results={c['id']:c for c in report['steps'][1]['compensations']}
    assert results['trunk_lean']['threshold']==12 and results['trunk_lean']['unit']=='percent'
    assert results['head_drop']['threshold']==10 and results['head_drop']['unit']=='percent'
    assert results['head_drop']['label']=='Head moving forward'
    assert {m['metric']:m['unit'] for m in report['steps'][1]['measurements']}['trunk_lean']=='percent'
    if head>10:
        checks['head_drop']['max_streak_ms']=400
        results=make_report(attempt,server.ASSESSMENT_RUBRICS)['task']['steps'][1]['compensations']
        assert next(c for c in results if c['id']=='head_drop')['status']=='not_detected'


def test_testing_serves_the_same_t3_definition_as_normal_assessment(monkeypatch):
    from starlette.requests import Request
    async def signed_in(_headers):
        return {'id': 'task-definition-qa'}
    async def access(_user, _package, requested):
        return {'task_ids': requested, 'trigger': 'qa'}
    monkeypatch.setattr(server, '_user_from_header', signed_in)
    monkeypatch.setattr(server, '_assessment_access_plan', access)
    monkeypatch.setattr(server, '_record_alira_action', lambda *args, **kwargs: None)
    request = Request({'type': 'http', 'headers': []})
    normal = asyncio.run(server.get_tasks(request, 'upper_limb', 'T3', False))
    testing = asyncio.run(server.get_tasks(request, 'upper_limb', 'T3', True))
    assert normal['tasks'] == testing['tasks']
    assert [step['hold_ms'] for step in testing['tasks'][0]['steps']] == [1000, 1500, 1500, 1500]
    assert [step['target']['landmark'] for step in testing['tasks'][0]['steps']] == ['CHEST', 'MOUTH', 'MOUTH', 'LAP_DYNAMIC']
