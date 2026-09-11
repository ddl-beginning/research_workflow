"""Two explicit runtime turns in a new disposable workspace; no automatic retries."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.workflow_runtime import WorkflowRuntime
from src.bridge_adapter import ProjectScopedBridgeConsultant
from src.stage_integration import subprocess_bridge_runner


def setup(root, project_url):
    root.mkdir(parents=True, exist_ok=False)
    runtime = WorkflowRuntime(root)
    runtime.start(brief={
        "project_goal": "Research a small local append-only experiment notebook with recoverable writes",
        "observable_outcome": "A bounded comparison of JSON Lines and SQLite for a single-user notebook",
        "scope": ["research and design only"],
        "non_goals": ["implementation", "experiments", "automatic execution"],
        "constraints": ["one user", "local storage", "Python standard library"],
        "available_assets": ["approved brief"],
        "acceptance": ["compare crash recovery and inspection tradeoffs with source references"],
        "human_preferences": ["small design and explicit unknowns"],
        "chatgpt_project_url": project_url,
    })
    runtime.answer(approve=True)
    return runtime


def run_pair(root, project_url, consultant):
    runtime = WorkflowRuntime(root)
    first = runtime.run_research_turn(consultant,
        "Research the approved notebook design. Compare JSON Lines and SQLite using known primary documentation. Give concise evidence and uncertainties, without implementing anything. Choose the routing that follows from your findings.",
        project_url=project_url)
    (root / 'fresh-result.json').write_text(json.dumps(first, ensure_ascii=False, indent=2), encoding='utf-8')
    # The second explicitly requested E2E turn checks continuity, not a routing loop.
    # Restart the runtime and rebuild assets from current project state.
    second = WorkflowRuntime(root).run_research_turn(consultant,
        "In this same notebook research conversation, compare the recovery guarantees in your previous findings and clarify one remaining uncertainty. Keep this bounded and choose an honest final routing; do not execute the next action.",
        project_url=project_url)
    r1, r2 = first['consultation']['receipt'], second['consultation']['receipt']
    if not (r1['mode'] == 'fresh' and r2['mode'] == 'continue'
            and r1['request_count'] == r2['request_count'] == 1
            and r1['conversation_id'] and r1['conversation_id'] == r2['conversation_id']
            and r2['parent_consultation_id'] == r1['consultation_id']
            and r2['conversation_root_consultation_id'] == r1['consultation_id']
            and r1['conversation_validated'] is True and r2['conversation_validated'] is True):
        raise RuntimeError('conversation continuity verification failed')
    result = {'status': 'PASS', 'first': first, 'second': second, 'conversation_continuity': True}
    (root / 'research-e2e-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-real', action='store_true', required=True)
    parser.add_argument('--workspace', required=True, help='New directory; existing workspace is refused')
    parser.add_argument('--project-url', required=True)
    parser.add_argument('--profile-dir', required=True)
    parser.add_argument('--bridge-root', required=True)
    parser.add_argument('--timeout-ms', type=int, default=300000)
    args = parser.parse_args()
    root = Path(args.workspace).resolve()
    setup(root, args.project_url)
    consultant = ProjectScopedBridgeConsultant(root, bridge_runner=subprocess_bridge_runner,
        profile_dir=args.profile_dir, bridge_root=args.bridge_root, timeout_ms=args.timeout_ms)
    try:
        result = run_pair(root, args.project_url, consultant)
    except Exception as exc:
        result = {'status': 'FAIL', 'code': getattr(exc, 'code', type(exc).__name__), 'message': str(exc)}
        (root / 'research-e2e-result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(json.dumps(result)); return 1
    print(json.dumps({'status': result['status'], 'workspace': str(root), 'conversation_id': result['second']['consultation']['receipt']['conversation_id']}))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
