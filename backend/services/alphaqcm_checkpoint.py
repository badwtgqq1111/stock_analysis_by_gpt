"""Complete training-state checkpointing for the isolated AlphaQCM worker.

The vendored agent only writes network weights (``save_models``); optimizers,
the replay memory, the expression pool, the in-episode partial expression and
the RNG streams are not part of that file, so loading weights alone silently
restarts training instead of resuming it.  This module snapshots the complete
training state and restores it, and it owns the per-step budget/stop hook.

Design notes
------------
* The tracker replaces ``agent.train_episode`` with a resumable copy of the
  official loop (vendored ``base_agent.train_episode``).  A checkpoint can be
  taken between steps of an unfinished episode because the partial expression
  (wrapper state/counter and ``AlphaEnvCore._tokens``) is part of the snapshot.
* ``AlphaEnvCore._evaluate`` is wrapped to count completed candidate
  expressions.  ``expr_attempts`` is the pre-registered "expression
  evaluation" budget unit for equal-budget comparisons; the official code only
  counts successful evaluations (``pool.eval_cnt`` / ``core.eval_cnt``).
* Checkpoints are self-produced by the research worker; ``torch.load`` is
  called with ``weights_only=False`` for that reason.  Never load a checkpoint
  from an untrusted origin.
* RNG state covers python ``random``, ``numpy`` global, ``torch`` CPU and the
  gym ``action_space`` generator used by ``agent.explore()``.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import os
from pathlib import Path
import random
import time
import types
from typing import Any, Callable

import numpy as np
import torch


class StopTraining(Exception):
    """Internal control-flow signal: a pre-registered budget or stop request fired."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# component (de)serialisation
# ---------------------------------------------------------------------------

def _rng_snapshot() -> dict:
    state: dict[str, Any] = {
        'python': random.getstate(),
        'numpy': np.random.get_state(),
        'torch': torch.get_rng_state(),
    }
    try:  # torch run may not exist; harmless when absent
        state['torch_cuda'] = torch.cuda.get_rng_state_all()
    except Exception:
        state['torch_cuda'] = None
    return state


def _rng_restore(data: dict) -> None:
    random.setstate(data['python'])
    np.random.set_state(data['numpy'])
    torch.set_rng_state(data['torch'].cpu() if isinstance(data['torch'], torch.Tensor) else data['torch'])
    cuda = data.get('torch_cuda')
    if cuda:
        try:
            torch.cuda.set_rng_state_all(cuda)
        except Exception:
            pass


def _space_rng_snapshot(space) -> dict | None:
    generator = space._np_random  # do not create one if sampling never happened
    if generator is None:
        return None
    return generator.bit_generator.state


def _space_rng_restore(space, state: dict | None) -> None:
    if state is None:
        return
    if space._np_random is None:
        space.seed()  # initialise, then overwrite below
    space._np_random.bit_generator.state = state


def _memory_snapshot(memory) -> dict:
    def vector(key: str) -> np.ndarray:
        array = np.array(memory[key], copy=True)
        n = int(memory._n)
        if n < int(memory.capacity):
            # Unwritten circular-buffer slots are np.empty garbage and are
            # never read; zero them so checkpoint bytes are reproducible.
            array[n:] = 0
        return array

    data: dict[str, Any] = {
        'type': type(memory).__name__,
        'n': int(memory._n), 'p': int(memory._p),
        'state': list(memory['state']),
        'next_state': list(memory['next_state']),
        'action': vector('action'),
        'reward': vector('reward'),
        'done': vector('done'),
    }
    buff = getattr(memory, 'buff', None)
    if buff is not None:
        data['buff'] = {
            'maxlen': int(buff.maxlen),
            'states': list(buff.states),
            'actions': list(buff.actions),
            'rewards': list(buff.rewards),
        }
    if hasattr(memory, 'it_sum'):
        data['per'] = {
            'sum': list(memory.it_sum._values),
            'min': list(memory.it_min._values),
            'beta': float(memory.beta),
            'cached': None if memory._cached is None else list(memory._cached),
        }
    return data


def _memory_restore(memory, data: dict) -> None:
    if type(memory).__name__ != data['type']:
        raise ValueError(f"memory 类型不一致: checkpoint={data['type']} agent={type(memory).__name__}")
    memory['state'] = list(data['state'])
    memory['next_state'] = list(data['next_state'])
    for key in ('action', 'reward', 'done'):
        target = memory[key]
        source = data[key]
        if target.shape != source.shape:
            raise ValueError(f'memory[{key}] 形状不一致')
        target[...] = source
    memory._n = int(data['n'])
    memory._p = int(data['p'])
    buff_data = data.get('buff')
    if buff_data is not None:
        buff = getattr(memory, 'buff', None)
        if buff is None:
            raise ValueError('checkpoint 含 multi-step buffer，但 agent memory 没有')
        if int(buff.maxlen) != buff_data['maxlen']:
            raise ValueError('multi-step buffer maxlen 不一致')
        buff.states = deque(buff_data['states'], maxlen=buff.maxlen)
        buff.actions = deque(buff_data['actions'], maxlen=buff.maxlen)
        buff.rewards = deque(buff_data['rewards'], maxlen=buff.maxlen)
    per_data = data.get('per')
    if per_data is not None:
        if not hasattr(memory, 'it_sum'):
            raise ValueError('checkpoint 含 PER，但 agent memory 没有')
        memory.it_sum._values = list(per_data['sum'])
        memory.it_min._values = list(per_data['min'])
        memory.beta = float(per_data['beta'])
        memory._cached = None if per_data['cached'] is None else list(per_data['cached'])


def _pool_snapshot(pool) -> dict:
    return {
        'size': int(pool.size),
        'best_ic_ret': float(pool.best_ic_ret),
        'eval_cnt': int(pool.eval_cnt),
        'exprs': [None if e is None else str(e) for e in pool.exprs],
        'single_ics': np.array(pool.single_ics, copy=True),
        'mutual_ics': np.array(pool.mutual_ics, copy=True),
        'weights': np.array(pool.weights, copy=True),
    }


def _pool_restore(pool, data: dict) -> None:
    from backend.services.alphagen_expression import parse_expression

    exprs = [None if source is None else parse_expression(source) for source in data['exprs']]
    if len(exprs) != len(pool.exprs):
        raise ValueError('pool 容量不一致')
    pool.exprs = exprs
    for key in ('single_ics', 'mutual_ics', 'weights'):
        target = getattr(pool, key)
        source = data[key]
        if target.shape != source.shape:
            raise ValueError(f'pool.{key} 形状不一致')
        target[...] = source
    pool.size = int(data['size'])
    pool.best_ic_ret = float(data['best_ic_ret'])
    pool.eval_cnt = int(data['eval_cnt'])


def _agent_snapshot(agent) -> dict:
    nets = {}
    for name in ('online_net', 'target_net', 'online_mean_net', 'target_mean_net'):
        net = getattr(agent, name, None)
        if net is not None:
            nets[name] = {k: v.detach().cpu().clone() for k, v in net.state_dict().items()}
    optims = {}
    for name in ('optim', 'mean_optim', 'optim_online', 'optim_mean'):
        optim = getattr(agent, name, None)
        if optim is not None:
            optims[name] = optim.state_dict()
    return {
        'steps': int(agent.steps),
        'learning_steps': int(agent.learning_steps),
        'episodes': int(agent.episodes),
        'best_eval_score': float(agent.best_eval_score),
        'best_test_score': float(agent.best_test_score),
        'epsilon_train_steps': int(agent.epsilon_train.steps),
        'train_return_stats': list(agent.train_return.stats),
        'nets': nets,
        'optims': optims,
        'memory': _memory_snapshot(agent.memory),
        'pool': _pool_snapshot(agent.env.env.pool),
    }


def _agent_restore(agent, data: dict) -> None:
    for name, state_dict in data['nets'].items():
        net = getattr(agent, name)
        net.load_state_dict({k: v.cpu() for k, v in state_dict.items()})
    for name, state_dict in data['optims'].items():
        optim = getattr(agent, name, None)
        if optim is None:
            raise ValueError(f'agent 缺少优化器 {name}')
        optim.load_state_dict(state_dict)
    _memory_restore(agent.memory, data['memory'])
    _pool_restore(agent.env.env.pool, data['pool'])
    agent.steps = int(data['steps'])
    agent.learning_steps = int(data['learning_steps'])
    agent.episodes = int(data['episodes'])
    agent.best_eval_score = float(data['best_eval_score'])
    agent.best_test_score = float(data['best_test_score'])
    agent.epsilon_train.steps = int(data['epsilon_train_steps'])
    agent.train_return.stats = deque(data['train_return_stats'], maxlen=agent.train_return.n)


def _env_snapshot(agent) -> dict:
    core = agent.env.env
    wrapper = agent.env
    # NOTE: ``wrapper.state`` is the *same array object* the vendored env
    # mutates in place and that the replay memory holds references to.  It is
    # saved without copying so pickle's memo keeps the shared identity on
    # load; a copy here would silently break that aliasing on resume.
    return {
        'core': {
            'tokens': list(core._tokens),
            'eval_cnt': int(core.eval_cnt),
        },
        'wrapper': {
            'state': wrapper.state,
            'counter': int(wrapper.counter),
        },
    }


def _env_restore(agent, data: dict) -> None:
    from alphagen.data.tree import ExpressionBuilder

    core = agent.env.env
    tokens = list(data['core']['tokens'])
    builder = ExpressionBuilder()
    for token in tokens[1:]:
        builder.add_token(token)
    core._tokens = tokens
    core._builder = builder
    core.eval_cnt = int(data['core']['eval_cnt'])
    wrapper = agent.env
    # Keep the unpickled object (shared with the memory lists) instead of
    # copying, so the in-place mutation aliasing matches an uninterrupted run.
    wrapper.state = data['wrapper']['state']
    wrapper.counter = int(data['wrapper']['counter'])


# ---------------------------------------------------------------------------
# tracker
# ---------------------------------------------------------------------------

class TrainingTracker:
    """Per-step hook: budgets, stop requests, periodic full-state snapshots."""

    def __init__(self, agent, output_dir: Path | str, *,
                 interval_steps: int = 0, keep_last: int = 2,
                 stop_steps: int | None = None, stop_evals: int | None = None,
                 max_seconds: float | None = None,
                 meta: dict[str, Any] | None = None):
        self.agent = agent
        self.output_dir = Path(output_dir)
        self.checkpoint_dir = self.output_dir / 'checkpoints'
        self.interval_steps = int(interval_steps or 0)
        self.keep_last = int(keep_last)
        self.stop_steps = stop_steps
        self.stop_evals = stop_evals
        self.max_seconds = max_seconds
        self.meta = dict(meta or {})
        # counters persisted across resume
        self.expr_attempts = 0
        self.expr_errors = 0
        self.valid_evals = 0
        # in-episode state persisted across resume
        self.in_episode = False
        self.episode_steps = 0
        self.episode_return = 0.0
        self.done = False
        self._stop_reason: str | None = None
        self._started_at = time.time()
        self._elapsed_before_resume = 0.0
        self._installed = False

    # -- lifecycle ---------------------------------------------------------

    def install(self) -> None:
        if self._installed:
            return
        agent = self.agent
        core = agent.env.env

        original_evaluate = core._evaluate

        def counted_evaluate():
            self.expr_attempts += 1
            try:
                return original_evaluate()
            except Exception:
                self.expr_errors += 1
                raise

        core._evaluate = counted_evaluate

        previous_agent_evaluate = agent.evaluate
        tracker = self

        def counted_agent_evaluate():
            tracker.valid_evals += 1
            return previous_agent_evaluate()

        agent.evaluate = types.MethodType(
            lambda _self: counted_agent_evaluate(), agent)

        agent.train_episode = types.MethodType(
            lambda _self: tracker._train_episode(), agent)

        self._installed = True

    def request_stop(self, reason: str) -> None:
        if self._stop_reason is None:
            self._stop_reason = reason

    def elapsed_seconds(self) -> float:
        return self._elapsed_before_resume + (time.time() - self._started_at)

    # -- resumable episode loop (mirrors vendored BaseAgent.train_episode) --

    def _train_episode(self) -> None:
        agent = self.agent
        if not self.in_episode:
            agent.online_net.train()
            agent.target_net.train()
            agent.episodes += 1
            self.episode_return = 0.0
            self.episode_steps = 0
            self.done = False
            self.state, _info = agent.env.reset()
            self.in_episode = True

        while (not self.done) and self.episode_steps <= agent.max_episode_steps:
            agent.online_net.sample_noise()
            if agent.is_random(eval=False):
                action = agent.explore()
            else:
                action = agent.exploit(self.state)
            next_state, reward, done, _truncated, _info = agent.env.step(action)
            agent.memory.append(self.state, action, reward, next_state, done)
            agent.steps += 1
            self.episode_steps += 1
            self.episode_return += reward
            self.state = next_state
            self.done = bool(done)
            agent.train_step_interval()
            self._after_step()

        agent.train_return.append(self.episode_return)
        self.in_episode = False
        if agent.episodes % agent.log_interval == 0:
            agent.writer.add_scalar(
                'ic/train', agent.env.env.pool.state['best_ic_ret'], agent.steps)

    def _after_step(self) -> None:
        if self._stop_reason is not None:
            raise StopTraining(self._stop_reason)
        if self.max_seconds is not None and self.elapsed_seconds() > self.max_seconds:
            raise StopTraining('wall_clock_budget')
        if self.stop_steps is not None and self.agent.steps >= self.stop_steps:
            raise StopTraining('step_budget')
        if self.stop_evals is not None and self.expr_attempts >= self.stop_evals:
            raise StopTraining('eval_budget')
        if self.interval_steps and self.agent.steps % self.interval_steps == 0:
            self.save(reason='periodic')

    # -- snapshots ---------------------------------------------------------

    def save(self, reason: str = 'manual') -> Path:
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            'format_version': 1,
            'created_at': _now(),
            'reason': reason,
            'meta': dict(self.meta),
            'agent': _agent_snapshot(self.agent),
            'env': _env_snapshot(self.agent),
            'rng': _rng_snapshot(),
            'space_rng': _space_rng_snapshot(self.agent.env.action_space),
            'tracker': {
                'expr_attempts': int(self.expr_attempts),
                'expr_errors': int(self.expr_errors),
                'valid_evals': int(self.valid_evals),
                'in_episode': bool(self.in_episode),
                'episode_steps': int(self.episode_steps),
                'episode_return': float(self.episode_return),
                'done': bool(self.done),
                'elapsed_seconds': float(self.elapsed_seconds()),
            },
        }
        steps = int(self.agent.steps)
        tmp = self.checkpoint_dir / f'.tmp-{os.getpid()}-{steps}.pt'
        torch.save(payload, tmp)
        step_path = self.checkpoint_dir / f'step_{steps:09d}.pt'
        os.replace(tmp, step_path)
        latest_tmp = self.checkpoint_dir / f'.tmp-latest-{os.getpid()}.pt'
        if latest_tmp.exists():
            latest_tmp.unlink()
        os.link(step_path, latest_tmp)
        os.replace(latest_tmp, self.checkpoint_dir / 'latest.pt')
        self._prune(step_path)
        return step_path

    def _prune(self, protected: Path) -> None:
        if self.keep_last <= 0:
            return
        files = sorted(self.checkpoint_dir.glob('step_*.pt'))
        for path in files[:-self.keep_last] if self.keep_last < len(files) else []:
            if path == protected:
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def restore(self, path: Path | str) -> dict:
        path = Path(path)
        payload = torch.load(path, map_location='cpu', weights_only=False)
        if payload.get('format_version') != 1:
            raise ValueError('checkpoint 格式版本不支持')
        self._check_compatible(payload)
        _agent_restore(self.agent, payload['agent'])
        _env_restore(self.agent, payload['env'])
        _rng_restore(payload['rng'])
        _space_rng_restore(self.agent.env.action_space, payload.get('space_rng'))
        tracker = payload['tracker']
        self.expr_attempts = int(tracker['expr_attempts'])
        self.expr_errors = int(tracker['expr_errors'])
        self.valid_evals = int(tracker['valid_evals'])
        self.in_episode = bool(tracker['in_episode'])
        self.episode_steps = int(tracker['episode_steps'])
        self.episode_return = float(tracker['episode_return'])
        self.done = bool(tracker['done'])
        if self.in_episode:
            # continue the interrupted episode with the restored live buffer
            self.state = self.agent.env.state
        self._elapsed_before_resume = float(tracker.get('elapsed_seconds', 0.0))
        self._started_at = time.time()
        return payload

    def _check_compatible(self, payload: dict) -> None:
        expected = self.meta
        actual = payload.get('meta', {})
        for key in ('run_id', 'model', 'seed', 'pool_capacity', 'std_lam',
                    'official_commit', 'train_sha256', 'valid_sha256', 'features'):
            if key in expected and expected[key] is not None and expected[key] != actual.get(key):
                raise ValueError(
                    f'checkpoint 不兼容: {key} checkpoint={actual.get(key)!r} '
                    f'request={expected[key]!r}')

    # -- status helpers ----------------------------------------------------

    def counters(self) -> dict:
        return {
            'expr_attempts': int(self.expr_attempts),
            'expr_errors': int(self.expr_errors),
            'valid_evals': int(self.valid_evals),
            'elapsed_seconds': float(self.elapsed_seconds()),
        }
