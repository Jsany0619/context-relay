"""Local state projection: never include task text, paths, identifiers or credentials."""
from datetime import datetime, timezone
import math
import platform

from .budget import budget_status


STATES = frozenset(('queued', 'creating', 'running', 'pausing', 'paused', 'idle',
                    'summarizing', 'verifying', 'needs_reconcile', 'blocked', 'completed',
                    'briefing', 'reviewing'))


def diagnostics_report(tasks, inspection_only=False):
    rows = []
    for index, task in enumerate(tasks, 1):
        state = task.get('state') if task.get('state') in STATES else 'unknown'
        mode = task.get('mode') if task.get('mode') in ('read-only', 'workspace-write') else 'unknown'
        numbers = {key: task.get(key, 0) for key in ('usage', 'max_tokens', 'max_minutes', 'elapsed_seconds')}
        started = None if inspection_only else task.get('run_started')
        valid = all((type(value) is int or type(value) is float and math.isfinite(value))
                    and value >= 0 for value in numbers.values())
        valid = valid and (started is None or isinstance(started, (int, float))
                          and not isinstance(started, bool) and math.isfinite(started))
        budget = budget_status(dict(numbers, run_started=started)) if valid else None
        if inspection_only:
            action = 'inspection_only'
        elif state == 'needs_reconcile' or task.get('intent') or task.get('inflight'):
            action = 'reconcile_before_work'
        elif task.get('pending'):
            action = 'review_pending_request'
        elif state in ('creating', 'running', 'pausing', 'summarizing', 'verifying', 'briefing', 'reviewing'):
            action = 'wait_for_terminal_result'
        elif task.get('archived') or state == 'completed':
            action = 'completed_or_archived'
        elif budget and budget['reached']:
            action = 'adjust_budget'
        elif state in ('paused', 'idle', 'queued') and budget:
            action = 'await_explicit_instruction'
        else:
            action = 'inspect_on_computer'
        rows.append({'number': index, 'state': state, 'mode': mode,
                     'archived': bool(task.get('archived')), 'has_error': bool(task.get('error')),
                     'has_native_thread': bool(task.get('thread_id')),
                     'pending_requests': len(task.get('pending') or []),
                     'inflight_operations': len(task.get('inflight') or {}),
                     'budget': ({'elapsed_minutes': round(budget['elapsed_seconds'] / 60, 3),
                                 'max_minutes': numbers['max_minutes'], 'tokens': numbers['usage'],
                                 'max_tokens': numbers['max_tokens'],
                                 'minutes_reached': budget['minutes_reached'],
                                 'token_reached': budget['token_reached']} if budget else None),
                     'next_action': action})
    return {'format': 'context-relay-diagnostics', 'version': 1,
            'captured_at': datetime.now(timezone.utc).isoformat(),
            'runtime': {'python': platform.python_version()}, 'inspection_only': bool(inspection_only),
            'counts': {'tasks': len(rows), 'needs_reconcile': sum(row['state'] == 'needs_reconcile' for row in rows),
                       'has_error': sum(row['has_error'] for row in rows)},
            'tasks': rows,
            'boundaries': ['仅根据管理器当前本地记录生成，不证明原生会话、网络或登录正常。',
                           '未包含聊天正文、名称、文件路径、任务或会话编号、凭据及原始错误。',
                           '编号仅用于此报告内区分任务；不启动任务，不自动修复或上传。',
                           '用量与时间是本地记录，不能作为账单或硬性费用上限。']}
