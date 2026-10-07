"""Official Claude Code loop with Arena's unchanged tool and budget boundary."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import dataclasses
import functools
import fcntl
import json
import math
import os
from pathlib import Path
import time
import traceback

from claude_agent_sdk import (ClaudeAgentOptions, ClaudeSDKClient, ResultMessage,
                             SdkMcpTool, SystemMessage, create_sdk_mcp_server)
from aa_arena.benchmark.runtime import CodexArenaRuntime, experiment_tools, _candidate_preflight_failure
from aa_arena.benchmark.service import BenchmarkService


def official_claude_cli():
    """Use an explicit CLI or the CLI shipped by the official Agent SDK."""
    import shutil
    configured = os.environ.get('AA_ARENA_CLAUDE_CLI')
    if configured and configured != 'claude':
        found = shutil.which(configured)
        if not found:
            raise FileNotFoundError('Configured Claude Code executable is unavailable')
        return found
    pinned = Path(__file__).resolve().parents[3] / ".tools" / "claude" / "2.1.231" / "claude"
    if pinned.is_file():
        return str(pinned)
    import claude_agent_sdk
    bundled = Path(claude_agent_sdk.__file__).parent / '_bundled' / 'claude'
    if bundled.is_file():
        return str(bundled)
    found = shutil.which('claude')
    if found:
        return found
    raise FileNotFoundError('Install the official claude-agent-sdk extra or set AA_ARENA_CLAUDE_CLI')


class ServiceThread:
    """One owner for SQLite and service state, without blocking SDK transport."""
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='arena-ledger')

    async def call(self, function, *args, **kwargs):
        return await asyncio.get_running_loop().run_in_executor(
            self.executor, functools.partial(function, *args, **kwargs))

    def close(self):
        self.executor.shutdown(wait=True)


class ClaudeArenaRuntime:
    def __init__(self, service, *, model, base_url, token, owner=None,
                 max_budget_usd=1.0, max_turns=None, smoke=False, acceptance=False,
                 auth_mode="api_key"):
        if auth_mode not in {"api_key", "bearer"}:
            raise ValueError('auth_mode must be api_key or bearer')
        if isinstance(service, BenchmarkService) and owner is None:
            raise ValueError('BenchmarkService must be created and accessed on its ServiceThread')
        self.service, self.owner = service, owner
        self.model, self.base_url, self.token = model, base_url, token
        self.auth_mode = auth_mode
        self.max_budget_usd = max_budget_usd
        self.max_turns = 20 if (smoke or acceptance) else max_turns
        self.smoke, self.acceptance = smoke, acceptance
        self.lock = asyncio.Lock()
        self.calls, self.compactions = [], []
        self.final = False
        self.recover_receipt = False
        self.tool_seconds = 0.0
        self.events = service.controller / 'claude-events.jsonl'
        if self.events.exists():
            for line in self.events.read_text().splitlines():
                row = json.loads(line)
                if row['type'] == 'arena_tool':
                    self.calls.append(row['name'])
                if row['type'] == 'compaction_verified':
                    self.compactions.append(row['boundary'])

    async def on_service(self, function, *args, **kwargs):
        if self.owner is None:
            # Only for lightweight unit fixtures; real services always have an owner.
            return function(*args, **kwargs)
        return await self.owner.call(function, *args, **kwargs)

    def objective(self):
        context = object.__new__(CodexArenaRuntime)
        context.service = self.service
        return context._objective().replace(
            '只有总上下文达到配置的 200,000 token 门槛时才由 Codex 自动 compact。',
            '上下文窗口配置为 200,000 token，由官方 Claude Code 按原生安全余量自动压缩；预算与工作文件跨压缩保留。')

    def log(self, data):
        with self.events.open('a') as f:
            f.write(json.dumps(data, ensure_ascii=False, default=str) + '\n')

    def tools(self):
        result = []
        for definition in experiment_tools(self.service.experiment):
            name = definition['name']

            async def handle(arguments, tool_name=name):
                async with self.lock:
                    if self.recover_receipt and tool_name != 'large_match':
                        return {'isError':True,'content':[{'type':'text','text':'Committed-result recovery only; call large_match to retrieve the saved result.'}]}
                    if self.final:
                        return {'isError': True, 'content': [{'type': 'text', 'text': 'Strategy frozen; experiment tools disabled.'}]}
                    started = time.monotonic()
                    self.log({'type': 'arena_tool_start', 'name': tool_name, 'arguments': arguments})
                    try:
                        def invoke():
                            if self.recover_receipt and tool_name == 'large_match':
                                rows = [r for r in self.service.ledger.submissions() if r['kind']=='large' and r['status']=='complete']
                                value = self.service.agent_result(rows[-1]['result'], persist=False)
                                self.recover_receipt = False
                            else:
                                value = self.service.model_tool_call(tool_name, arguments)
                            if tool_name == 'restore_champion':
                                self.prepare_working_tree()
                            # Freeze before Claude can issue another writable shell call.
                            if self.service.ledger.state()['status'] != 'running':
                                self.freeze()
                            if tool_name in {'small_match', 'large_match', 'view_dense_trajectory'}:
                                self.service.ledger.mark_delivered(value['match_id'])
                            return value
                        value = await self.on_service(invoke)
                        self.calls.append(tool_name)
                        self.log({'type': 'arena_tool', 'name': tool_name, 'arguments': arguments,
                                  'result': value, 'duration': time.monotonic()-started})
                        return {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}]}
                    except Exception as e:
                        self.log({'type': 'arena_tool_error', 'name': tool_name, 'error': str(e), 'traceback': traceback.format_exc()})
                        return {'isError': True, 'content': [{'type': 'text', 'text': f'{type(e).__name__}: {e}'}]}
                    finally:
                        self.tool_seconds += time.monotonic() - started

            description = definition['description']
            if self.recover_receipt and name == 'large_match':
                description = 'Return the already completed and charged large_match receipt. Recovery only: no new match and zero additional budget.'
            result.append(SdkMcpTool(name=name, description=description,
                                    input_schema=definition['inputSchema'], handler=handle))
        return result

    def freeze(self):
        state = self.service.ledger.state()
        if state['status'] == 'running':
            raise RuntimeError('Cannot freeze a running experiment')
        if not state.get('frozen_snapshot_id'):
            raise RuntimeError('Non-running experiment has no frozen strategy')
        self.final = True
        self.service.restore_frozen_strategy()
        for path in (self.service.workspace, *self.service.workspace.rglob('*')):
            if not path.is_symlink():
                path.chmod(0o555 if path.is_dir() else 0o444)

    def prepare_working_tree(self):
        """SDK copies inherit read-only resource modes; enable only working roots."""
        if self.service.ledger.state()['status'] != 'running':
            return
        for name in ('strategy', 'skills', 'notes', 'replays', 'artifacts'):
            root = self.service.workspace / name
            if root.is_symlink() or not root.is_dir():
                raise ValueError('Writable workspace roots must be real directories')
            for path in (root, *root.rglob('*')):
                if not path.is_symlink():
                    path.chmod((path.stat().st_mode & 0o777) | (0o700 if path.is_dir() else 0o600))

    async def options(self):
        await self.on_service(self.prepare_working_tree)
        definitions = self.tools()
        allowed = ['mcp__arena__' + t.name for t in definitions]
        home = self.service.controller / 'claude-home'
        home.mkdir(mode=0o700, exist_ok=True)
        state = await self.on_service(self.service.ledger.state)
        _, spent = self.usage_audit() if self.events.exists() else ({}, 0.0)
        remaining = self.max_budget_usd - spent
        if remaining <= 0:
            raise RuntimeError(f'Configured cost ceiling reached: {spent:.4f} USD estimated')
        env = {'ANTHROPIC_BASE_URL': self.base_url,
               'ANTHROPIC_AUTH_TOKEN': self.token if self.auth_mode == 'bearer' else '',
               'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1',
               'CLAUDE_CODE_ATTRIBUTION_HEADER': '0', 'CLAUDE_CONFIG_DIR': str(home),
               'MCP_TOOL_TIMEOUT': '21600000',
               'ANTHROPIC_API_KEY': self.token if self.auth_mode == 'api_key' else '',
               'CLAUDE_CODE_AUTO_COMPACT_WINDOW': '200000',
               'CLAUDE_AUTOCOMPACT_PCT_OVERRIDE': '',
               'HTTP_PROXY':'','HTTPS_PROXY':'','ALL_PROXY':'','http_proxy':'','https_proxy':'','all_proxy':''}
        if self.smoke or self.acceptance:
            env['CLAUDE_CODE_MAX_OUTPUT_TOKENS'] = '2048'
        self.log({'type': 'harness_config', 'model': self.model, 'auth_mode': self.auth_mode, 'builtin_tools': [],
                  'allowed_tools': allowed, 'resume': state.get('thread_id'),
                  'remaining_cost_ceiling_usd': remaining, 'acceptance': self.acceptance,
                  'effort': 'low' if (self.smoke or self.acceptance) else 'max',
                  'max_turns': self.max_turns,
                  'output_token_override': 2048 if (self.smoke or self.acceptance) else None})
        return ClaudeAgentOptions(
            model=self.model, tools=[], allowed_tools=allowed, skills=[],
            mcp_servers={'arena': create_sdk_mcp_server(name='arena', version='1.0.0', tools=definitions)},
            strict_mcp_config=True, system_prompt=await self.on_service(self.objective),
            setting_sources=[], permission_mode='dontAsk', cwd=str(home), env=env,
            cli_path=official_claude_cli(),
            max_budget_usd=remaining, max_turns=self.max_turns,
            extra_args={'autocompact': '200k'},
            effort='low' if (self.smoke or self.acceptance) else 'max',
            resume=state.get('thread_id'))

    def usage_audit(self):
        # Pinned CLI 2.1.231: model_usage and cost are cumulative per process,
        # including native compaction; resume starts a fresh process counter.
        epochs, current = [], None
        for line in self.events.read_text().splitlines():
            row = json.loads(line)
            if row['type'] == 'harness_config':
                current = {'cost': 0.0, 'input_tokens': 0, 'output_tokens': 0,
                           'cached_input_tokens': 0}
                epochs.append(current)
            event = row.get('event', {})
            if 'total_cost_usd' not in event or current is None:
                continue
            current['cost'] = max(current['cost'], float(event.get('total_cost_usd') or 0))
            models = event.get('model_usage') or {}
            if not models:
                continue
            for output_key, input_keys in {
                'input_tokens': ['inputTokens', 'cacheReadInputTokens', 'cacheCreationInputTokens'],
                'output_tokens': ['outputTokens'], 'cached_input_tokens': ['cacheReadInputTokens'],
            }.items():
                count = sum(int(usage.get(key) or 0) for usage in models.values() for key in input_keys)
                current[output_key] = max(current[output_key], count)
        total = {key: sum(e[key] for e in epochs) for key in
                 ['input_tokens', 'output_tokens', 'cached_input_tokens']}
        total['total_tokens'] = total['input_tokens']+total['output_tokens']
        return total, sum(e['cost'] for e in epochs)

    def account(self, result, elapsed):
        state = self.service.ledger.state()
        total, cost = self.usage_audit()
        self.service.ledger.update_runtime(
            thread_id=result.session_id, token_usage=total,
            active_seconds=float(state['active_seconds'])+max(0, elapsed),
            metadata={**state['metadata'], 'harness': 'official-claude-code-agent-sdk',
                      'model': self.model, 'claude_estimated_cost_usd': cost,
                      'claude_last_result_usage': result.usage,
                      'claude_accounting': 'CLI 2.1.231 per-process cumulative model_usage, includes compaction',
                      'context_management': 'Claude Code native'})

    async def turn(self, client, prompt):
        started, tools_before = time.monotonic(), self.tool_seconds
        await client.query(prompt)
        result = None
        async for event in client.receive_response():
            self.log({'type': 'sdk_event', 'event': dataclasses.asdict(event)})
            observed = getattr(event, 'model', None)
            if observed and observed != self.model:
                raise RuntimeError(f'Unexpected response model: {observed}')
            if isinstance(event, SystemMessage):
                if event.subtype == 'init':
                    session = event.data.get('session_id')
                    if session:
                        await self.on_service(self.service.ledger.update_runtime, thread_id=session)
                elif event.subtype == 'compact_boundary':
                    self.compactions.append(event.data)
            if isinstance(event, ResultMessage):
                result = event
        if result is None:
            raise RuntimeError('Claude Code exited without a result')
        await self.on_service(self.account, result, time.monotonic()-started-self.tool_seconds+tools_before)
        if result.is_error:
            raise RuntimeError('Claude Code stopped: '+result.subtype+' '+str(result.result))
        return result

    async def compact(self, client):
        before = await self.on_service(self.service.budget_status)
        count = len(self.compactions)
        result = await self.turn(client, '/compact Preserve game, rules, tool names, strategy paths and authoritative remaining budgets. Do not invent match results.')
        after = await self.on_service(self.service.budget_status)
        if len(self.compactions) == count:
            raise RuntimeError('Native compact emitted no compact_boundary: '+str(result.result))
        if before != after:
            raise RuntimeError('Compaction changed authoritative experiment state')
        self.log({'type': 'compaction_verified', 'budget_before': before, 'budget_after': after,
                  'boundary': self.compactions[-1]})

    async def finalize(self, client):
        await self.on_service(self.freeze)
        claimed = await self.on_service(self.service.ledger.begin_final_review)
        if claimed:
            results = await self.on_service(lambda: [self.service.agent_result(r['result'], persist=False) for r in self.service.ledger.submissions() if r['kind']=='large' and r['status']=='complete'])
            await self.turn(client, '正式策略和比赛工具已冻结。权威比赛结果：'+json.dumps(results,ensure_ascii=False)+'。输出简短最终复盘，不得调用工具或修改文件。')
        elif (await self.on_service(self.service.ledger.state))['status'] == 'reviewing':
            # A failed review must not be silently marked successful on restart.
            await self.turn(client, '恢复未完成的最终复盘。策略已冻结，不得调用工具或修改文件。简短报告已有成绩和证据。')
        def complete():
            if self.service.ledger.pending_submission():
                raise RuntimeError('Cannot finish with a pending evaluation')
            self.service.snapshots.verify(self.service.ledger.state()['frozen_snapshot_id'])
            self.service.ledger.mark_complete()
        await self.on_service(complete)

    async def run_acceptance(self):
        initial = await self.on_service(self.service.ledger.state)
        if initial['status'] == 'complete':
            return await self.on_service(self.acceptance_audit)
        if initial['small_used'] == 0:
            async with ClaudeSDKClient(options=await self.options()) as client:
                await self.turn(client, '这是接口验收而不是主表策略实验，简短执行。调用 workspace_manifest、list_opponents；'
                                '用 workspace_shell 读取少量公开规则和 SDK 构建说明，在 notes/harness-acceptance.txt 写入 arena-harness-io-ok 并读回。'
                                '在 strategy/ 内创建、读回、重命名并删除一个临时写入探针文件，确认策略目录确实可写。'
                                '保持初始策略，按公开说明编译验证。只提交一次 small_match，选择公开排名第25的单个对手；'
                                '返回后报告 remaining 并停止。本轮禁止 large_match。')
        first = await self.on_service(self.service.ledger.state)
        if first['small_used'] != 1:
            raise RuntimeError('Small phase did not consume exactly one small point')
        required = {'workspace_manifest', 'list_opponents', 'workspace_shell', 'small_match'}
        if not required.issubset(self.calls):
            raise RuntimeError('Missing first-phase tools: '+str(required-set(self.calls)))
        self.recover_receipt = first['status'] in {'finalizing','reviewing'} and 'large_match' not in self.calls
        async with ClaudeSDKClient(options=await self.options()) as client:
            if first['large_used'] == 0:
                await self.compact(client)
                resumed = await self.on_service(self.service.ledger.state)
                if resumed['thread_id'] != first['thread_id']:
                    raise RuntimeError('Resume/compact changed session identity')
                await self.turn(client, '继续接口验收：根据最近工具返回核对 small_remaining=0、large_remaining=1；'
                                '用 workspace_shell 读取 notes/harness-acceptance.txt 确认输入输出文件保留。'
                                '调用一次 large_match（完整 verified 池），报告结果。不要修改策略，不再提交小对局。')
            elif first['status'] == 'running':
                await self.turn(client, '控制器迁移恢复：上一次 large_match 已接受并扣过预算，但结果尚未完成。请调用 large_match 恢复同一笔待完成评测，不会再扣预算。禁止修改策略或提交小对局。完成后简短报告结果。')
            if first['status'] in {'finalizing','reviewing'} and 'large_match' not in self.calls:
                self.recover_receipt = True
                await self.turn(client, '上一笔 large_match 已经完成并写入权威账本，但回传时中断。现在调用 large_match 取回同一笔已完成结果；这是回传恢复，不会运行比赛或消耗预算。报告结果后停止。')
            state = await self.on_service(self.service.ledger.state)
            if state['large_used'] != 1 or state['status'] not in {'finalizing','reviewing'}:
                raise RuntimeError('Large phase did not finalize the exact acceptance budget')
            if not self.compactions:
                raise RuntimeError('No verified native compaction before large evaluation')
            await self.finalize(client)
        return await self.on_service(self.acceptance_audit)

    def acceptance_audit(self):
        state = self.service.ledger.state()
        usage, cost = self.usage_audit()
        submissions = self.service.ledger.submissions()
        if len(submissions) != 2 or any(r['status'] != 'complete' for r in submissions):
            raise RuntimeError('Acceptance needs exactly two completed submissions')
        marker = self.service.workspace / 'notes/harness-acceptance.txt'
        if 'arena-harness-io-ok' not in marker.read_text():
            raise RuntimeError('Workspace input/output marker missing')
        before = self.service.budget_status()
        rejected = []
        for tool in ['small_match', 'large_match']:
            try:
                args = {'opponent_ids': submissions[0]['request']['opponent_ids']} if tool == 'small_match' else {}
                self.service.model_tool_call(tool, args)
            except ValueError:
                rejected.append(tool)
            else:
                raise RuntimeError('Exhausted budget accepted another '+tool)
        if before != self.service.budget_status():
            raise RuntimeError('Rejected submissions changed budgets')
        return {'passed': True, 'game': self.service.game, 'model': self.model,
                'purpose': 'harness acceptance, not main-table strategy quality',
                'budget': self.service.budget_status(), 'tools': self.calls,
                'session_id': state['thread_id'], 'native_compactions': self.compactions,
                'estimated_cost_usd': cost,
                'token_usage': usage, 'exhausted_calls_rejected': rejected,
                'matches': [{'kind': r['kind'], 'status': r['status'], 'result': r['result']} for r in submissions],
                'resource_metadata': state['metadata']}

    async def finish_clone_learning(self, client):
        if await self.on_service(self.service.ledger.pending_submission):
            raise RuntimeError('Clone has a pending match; recover it before final adaptation')
        state = await self.on_service(self.service.ledger.state)
        metadata = state['metadata']
        if not (metadata.get('clone_adaptation_complete') or metadata.get('clone_adaptation_finished')):
            history = await self.on_service(self.service.ledger.submissions)
            if len(history) != 32 or any(row['status'] != 'complete' for row in history):
                raise RuntimeError('Clone requires 32 completed matches before final adaptation')
            last = await self.on_service(self.service.agent_result, history[-1]['result'])
            await self.turn(client, '32 次固定对手小对局已完成。读取最后反馈并完成最后一次策略适应，'
                            '修改 strategy/ 后做离线构建检查。不得提交比赛，不得声称已测得 Elo。最后反馈：'
                            +json.dumps(last,ensure_ascii=False))
            metadata = dict((await self.on_service(self.service.ledger.state))['metadata'])
            metadata['clone_adaptation_finished'] = True
            await self.on_service(self.service.ledger.update_runtime,metadata=metadata)
        while True:
            try:
                await self.on_service(self.service._preflight)
                metadata = dict((await self.on_service(self.service.ledger.state))['metadata'])
                metadata['clone_adaptation_complete'] = True
                await self.on_service(self.service.ledger.update_runtime,metadata=metadata)
                for row in await self.on_service(self.service.ledger.undelivered_results):
                    await self.on_service(self.service.ledger.mark_delivered,row['match_id'])
                await self.on_service(self.service.finalize_clone)
                return
            except Exception as error:
                if not _candidate_preflight_failure(error):
                    raise
                metadata = dict((await self.on_service(self.service.ledger.state))['metadata'])
                metadata.update(clone_adaptation_complete=False,clone_adaptation_finished=True)
                attempts = int(metadata.get('clone_final_repair_attempts',0))
                if attempts >= 2:
                    await self.on_service(self.service.ledger.update_runtime,metadata=metadata)
                    raise RuntimeError('Clone final candidate failed two bounded repair turns') from error
                metadata['clone_final_repair_attempts'] = attempts+1
                await self.on_service(self.service.ledger.update_runtime,metadata=metadata)
                await self.turn(client,f'最终策略预检失败，执行第 {attempts+1}/2 次离线修复。只修复编译、语法或入口问题，保留已学习策略。不得提交比赛。')

    async def run(self):
        if self.acceptance:
            return await self.run_acceptance()
        async with ClaudeSDKClient(options=await self.options()) as client:
            idle = 0
            for _ in range(1000):
                state = await self.on_service(self.service.ledger.state)
                if state['status'] == 'complete':
                    return {'complete': True}
                if state['status'] != 'running':
                    await self.finalize(client)
                    return {'complete': True}
                if self.service.experiment.is_clone and (await self.on_service(self.service.ledger.budgets)).small_remaining == 0:
                    await self.finish_clone_learning(client)
                    await self.finalize(client)
                    return {'complete': True}
                before = await self.on_service(self.service.ledger.budgets)
                prompt = '继续执行目标。先检查公开规则、workspace_manifest、list_opponents 并编译策略。剩余额度：'+json.dumps(await self.on_service(self.service.budget_status), ensure_ascii=False)
                if self.smoke:
                    prompt += ' 本次仅作低成本测试：用 workspace_shell 读取规则并提交一次 small_match，禁止 large_match，随后停止。'
                result = await self.turn(client, prompt)
                if self.smoke:
                    return {'passed': 'small_match' in self.calls, 'tools': self.calls, 'reported_cost_usd': result.total_cost_usd}
                after = await self.on_service(self.service.ledger.budgets)
                idle = idle+1 if (before.small_used,before.large_used)==(after.small_used,after.large_used) else 0
                if idle >= 8:
                    raise RuntimeError('Eight turns without a match submission or trajectory view')
        raise RuntimeError('Controller iteration limit reached')


def resolve_cost_ceiling(value, *, test_mode):
    if value is None:
        if not test_mode:
            raise ValueError('Formal experiments require an explicit --max-budget-usd; the test default must not truncate training.')
        value = 1.0
    if not math.isfinite(value) or value <= 0:
        raise ValueError('--max-budget-usd must be finite and positive')
    return value


async def main():
    p = argparse.ArgumentParser()
    p.add_argument('--run-dir', required=True)
    p.add_argument('--game', default='rollman')
    p.add_argument('--smoke', action='store_true')
    p.add_argument('--acceptance', action='store_true')
    p.add_argument('--workers', type=int, default=16)
    p.add_argument('--model-profile', required=True)
    p.add_argument('--small-budget', type=int, default=128)
    p.add_argument('--large-budget', type=int, default=16)
    p.add_argument('--extend-budget-once', action='store_true')
    p.add_argument('--skip-baseline', action='store_true')
    p.add_argument('--max-budget-usd', type=float, default=None)
    a = p.parse_args()
    try:
        a.max_budget_usd = resolve_cost_ceiling(a.max_budget_usd, test_mode=a.smoke or a.acceptance)
    except ValueError as error:
        p.error(str(error))
    lock_path = Path(a.run_dir) / 'controller/claude-runtime.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    run_lock = lock_path.open('a')
    fcntl.flock(run_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    import subprocess
    version = subprocess.check_output([official_claude_cli(), '--version'], text=True).strip()
    if not version.startswith('2.1.231 '):
        raise RuntimeError('Unvalidated Claude Code accounting version: '+version)
    from aa_arena.benchmark.profile import load_profile
    profile = load_profile(a.model_profile)
    token = os.environ.get(profile.api_key_env)
    if not token:
        raise ValueError('Missing API credential environment variable: '+profile.api_key_env)
    if profile.reasoning_effort != 'max' and not (a.smoke or a.acceptance):
        raise ValueError('Formal Claude experiments require a max-effort profile')
    owner = ServiceThread()
    service = None
    try:
        service = await owner.call(BenchmarkService, Path(a.run_dir), game=a.game, model_profile=a.model_profile,
                                   small_budget=1 if a.acceptance else (2 if a.smoke else a.small_budget),
                                   large_budget=1 if (a.acceptance or a.smoke) else a.large_budget, workers=a.workers,
                                   extend_budget_once=a.extend_budget_once)
        await owner.call(service.initialize_workspace)
        if not a.acceptance:
            await owner.call(service.recover_pending)
        if not (a.acceptance or a.smoke or a.skip_baseline):
            submissions = await owner.call(service.ledger.submissions)
            if not any(r['kind']=='baseline' and r['status']=='complete' for r in submissions):
                await owner.call(service.baseline)
        runner = ClaudeArenaRuntime(service, owner=owner, model=profile.model,
                                    base_url=profile.base_url, token=token,
                                    max_budget_usd=a.max_budget_usd, smoke=a.smoke, acceptance=a.acceptance,
                                    auth_mode=profile.claude_auth_mode)
        result = await runner.run()
        (service.controller/'claude-test-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
        print(json.dumps({k:v for k,v in result.items() if k not in {'matches','resource_metadata'}}, ensure_ascii=False))
    finally:
        if service is not None:
            await owner.call(service.close)
        owner.close()


def cli_main():
    asyncio.run(main())


if __name__ == '__main__':
    cli_main()
