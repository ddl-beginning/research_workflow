"""Offline runtime/adapter tests using the real Node context-pack validator."""
import json
import subprocess
from pathlib import Path

import pytest

from scripts.research_turn_real_e2e import setup, run_pair
from src.bridge_adapter import ProjectScopedBridgeConsultant
from src.workflow_runtime import WorkflowRuntime, WorkflowRuntimeError
from tests.test_bridge_adapter import completed_envelope, PROJECT_URL

ROUTE = 'ROUTING_CONTRACT_V1: ' + json.dumps(dict(research_status='CONTINUE', unresolved_gaps=['recovery'], next_action='TARGETED_RESEARCH', design_status='DRAFT'))


def runner_for(calls, responses=None):
    def runner(prompt, **kwargs):
        calls.append(dict(prompt=prompt, **kwargs))
        n = len(calls)
        envelope = completed_envelope(response=responses[n-1] if responses else ROUTE)
        identity = f'CONSULT-20260908-00000{n}-aabbccdd'
        envelope['consultationId'] = identity
        r = envelope['receipt']
        r.update(consultation_id=identity, mode=kwargs['mode'], conversation_validated=True,
                 parent_consultation_id=kwargs.get('continue_from'),
                 conversation_root_consultation_id='CONSULT-20260908-000001-aabbccdd')
        return envelope
    return runner


def test_clean_runtime_pair_rebuilds_current_context(tmp_path):
    root = tmp_path / 'workspace'
    setup(root, PROJECT_URL)
    calls = []
    consultant = ProjectScopedBridgeConsultant(root, bridge_runner=runner_for(calls))
    result = run_pair(root, PROJECT_URL, consultant)
    assert result['conversation_continuity']
    assert [c['mode'] for c in calls] == ['fresh', 'continue']
    assert [c['context_pack']['mode'] for c in calls] == ['fresh', 'normal']
    checkpoint = json.loads((root / '.research/workflow-state.json').read_text(encoding='utf-8'))
    assert str(root) not in json.dumps(checkpoint['research_session'])
    assert 'previousPack' not in calls[1]['context_pack']
    (root / '.research/PROJECT_CONTEXT.md').write_text('Current evidence changed', encoding='utf-8')
    WorkflowRuntime(root).run_research_turn(consultant, 'check update')
    assert any(e['content'] == 'Current evidence changed' for e in calls[2]['context_pack']['evidence'])


def test_invalid_routing_keeps_identity_for_explicit_correction(tmp_path):
    root = tmp_path / 'workspace'
    setup(root, PROJECT_URL)
    calls = []
    consultant = ProjectScopedBridgeConsultant(root, bridge_runner=runner_for(calls, ['invalid', ROUTE]))
    with pytest.raises(WorkflowRuntimeError, match='marker'):
        WorkflowRuntime(root).run_research_turn(consultant, 'research')
    checkpoint = json.loads((root / '.research/workflow-state.json').read_text(encoding='utf-8'))
    assert checkpoint['research_session']['consultation_id']
    assert checkpoint['research_routing_status'] == 'INVALID'
    assert 'latest_routing_decision' not in checkpoint
    with pytest.raises(WorkflowRuntimeError):
        WorkflowRuntime(root).advance_research(consultant)
    assert len(calls) == 1
    WorkflowRuntime(root).run_research_turn(consultant, 'correct')
    assert calls[1]['mode'] == 'continue'
    assert 'Do not research again' in calls[1]['prompt']
    assert len(calls) == 2


def test_real_node_validator_reproduces_mode_failure_and_rejects_tamper(tmp_path):
    root = tmp_path / 'workspace'
    setup(root, PROJECT_URL)
    calls = []
    consultant = ProjectScopedBridgeConsultant(root, bridge_runner=runner_for(calls))
    run_pair(root, PROJECT_URL, consultant)
    spec = tmp_path / 'pack.json'
    spec.write_text(json.dumps(dict(calls[1]['context_pack'], rootDir=str(root))), encoding='utf-8')
    module = (Path(__file__).resolve().parents[2] / 'chatgpt_browser_bridge/src/context-pack.mjs').as_uri()
    script = """
import fs from 'node:fs/promises';
const {buildContextPack,validateContextPackForConsult}=await import(process.argv[1]);
const spec=JSON.parse(await fs.readFile(process.argv[2],'utf8'));
const normal=await buildContextPack(spec);
await validateContextPackForConsult(normal,{expectedConversationMode:'continue'});
const fresh=await buildContextPack({...spec,mode:'fresh'});
let error;
try {await validateContextPackForConsult(fresh,{expectedConversationMode:'continue'});}catch(e){error=e.message;}
if(error!=='A fresh context pack must use a fresh ChatGPT conversation.')throw Error('missing mode rejection');
await fs.appendFile(normal.manifestPath,' ');
try {await validateContextPackForConsult(normal,{expectedConversationMode:'continue'});throw Error('accepted tamper');}catch(e){if(e.code!=='CONTEXT_PACK_INTEGRITY_FAILED')throw e;}
console.log('PASS');
"""
    result = subprocess.run(['node', '--input-type=module', '-e', script, module, str(spec)], capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr
    assert 'PASS' in result.stdout

