"""Isolated AlphaQCM miner contract: propose / evaluate / export.

The training subprocess receives only the train and valid panels.  Test data
is never written into the miner request directory; evaluation is a separate
process after the factor artifact hash is frozen.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OFFICIAL_REPO = 'https://github.com/ZhuZhouFan/AlphaQCM'
OFFICIAL_COMMIT = 'dffc695f9ea54e6a54e4dac86ee357be314fead4'


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as fh:
        for block in iter(lambda: fh.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


@dataclass(frozen=True)
class MinerBudget:
    model: str = 'iqn'
    seed: int = 0
    pool_capacity: int = 10
    std_lam: float = 1.0
    num_steps: int = 2000
    start_steps: int = 500
    batch_size: int = 32
    N: int = 16
    N_dash: int = 16
    K: int = 8
    num_cosines: int = 16
    kappa: float = 1.0
    lr: float = 1e-4
    memory_size: int = 20000
    gamma: float = 1.0
    multi_step: int = 1
    update_interval: int = 4
    target_update_interval: int = 500
    epsilon_train: float = 0.01
    epsilon_eval: float = 0.001
    epsilon_decay_steps: int = 500
    double_q_learning: bool = False
    dueling_net: bool = False
    noisy_net: bool = False
    use_per: bool = True
    log_interval: int = 100
    eval_interval: int = 500
    num_eval_steps: int = 5000
    max_episode_steps: int = 100
    grad_cliping: float | None = None
    device: str = 'cpu'
    # -- checkpoint / budget controls (consumed by the worker, not the agent) --
    checkpoint_interval: int = 0
    keep_checkpoints: int = 2
    stop_steps: int | None = None
    stop_evals: int | None = None
    max_seconds: float | None = None

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass(frozen=True)
class MiningRequest:
    run_id: str
    train_panel: pd.DataFrame
    valid_panel: pd.DataFrame
    split: dict[str, tuple[str, str]]
    output_dir: Path
    budget: MinerBudget = MinerBudget()
    max_backtrack_days: int = 100
    max_future_days: int = 25
    metadata: dict[str, Any] = field(default_factory=dict)
    resume: bool = False


@dataclass
class MiningArtifact:
    run_id: str
    output_dir: Path
    factors: list[dict]
    ensemble_valid_ic: float | None
    artifact_sha256: str
    status: str
    steps_completed: int
    manifest_path: Path
    evaluations: int = 0
    expr_errors: int = 0
    valid_evals: int = 0
    stop_reason: str | None = None
    resumed: bool = False
    resumed_from_steps: int | None = None
    checkpoint_latest: str | None = None
    features: list[str] = field(default_factory=list)


def run_miner(request: MiningRequest) -> MiningArtifact:
    """Run the official agent in a fresh process with only train/valid files."""
    out = Path(request.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    train_path, valid_path = out / 'train.parquet', out / 'valid.parquet'
    request.train_panel.to_parquet(train_path, index=False)
    request.valid_panel.to_parquet(valid_path, index=False)
    train_start, train_end = request.split['train']
    valid_start, valid_end = request.split['validation']
    payload = {
        'run_id': request.run_id,
        'output_dir': str(out),
        'train_path': str(train_path), 'valid_path': str(valid_path),
        'train_start': train_start, 'train_end': train_end,
        'valid_start': valid_start, 'valid_end': valid_end,
        'max_backtrack_days': request.max_backtrack_days,
        'max_future_days': request.max_future_days,
        'official_commit': OFFICIAL_COMMIT,
        'num_steps': request.budget.num_steps,
        'budget': request.budget.as_dict(),
        'resume': bool(request.resume),
    }
    request_path = out / 'request.json'
    request_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
    # Keep the two data panels on disk for audit, but never a test panel.
    proc = subprocess.run(
        [sys.executable, str(ROOT / 'scripts/alphaqcm_worker.py'),
         '--request', str(request_path)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=24 * 3600)
    if proc.returncode != 0:
        raise RuntimeError(f'AlphaQCM worker 失败: {proc.stderr[-2000:]}')
    factors_path = out / 'factors.json'
    if not factors_path.exists():
        raise RuntimeError('AlphaQCM worker 未生成 factors.json')
    factors_doc = json.loads(factors_path.read_text())
    status_doc = json.loads((out / 'status.json').read_text())
    manifest = {
        'status': status_doc['status'],
        'created_at': datetime.now(timezone.utc).isoformat(),
        'run_id': request.run_id,
        'official_repo': OFFICIAL_REPO,
        'official_commit': OFFICIAL_COMMIT,
        'vendor_commit': OFFICIAL_COMMIT,
        'split': request.split,
        'budget': request.budget.as_dict(),
        'steps_completed': status_doc['steps_completed'],
        'evaluations': status_doc.get('evaluations', 0),
        'expr_errors': status_doc.get('expr_errors', 0),
        'valid_evals': status_doc.get('valid_evals', 0),
        'features': status_doc.get('features', []),
        'stop_reason': status_doc.get('stop_reason'),
        'resumed': status_doc.get('resumed', request.resume),
        'resumed_from_steps': status_doc.get('resumed_from_steps'),
        'checkpoint_latest': status_doc.get('checkpoint_latest'),
        'explore_rng': 'seeded_from_run_seed',
        'artifact_sha256': factors_doc['artifact_sha256'],
        'train_sha256': _sha256(train_path),
        'valid_sha256': _sha256(valid_path),
        'test_visible_to_worker': False,
        'model_selection': 'valid IC only; test calculator not passed to worker',
        'metadata': request.metadata,
        'source_hashes': {str(p.relative_to(ROOT)): _sha256(p) for p in [
            ROOT / 'backend/services/alphaqcm_adapter.py',
            ROOT / 'backend/services/alphaqcm_checkpoint.py',
            ROOT / 'backend/services/alphaqcm_data.py',
            ROOT / 'scripts/alphaqcm_worker.py',
            ROOT / 'vendor/alphaqcm/alphagen/data/parser.py',
        ]},
    }
    manifest_path = out / 'manifest.json'
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + '\n')
    return MiningArtifact(
        run_id=request.run_id, output_dir=out,
        factors=factors_doc['factors'],
        ensemble_valid_ic=factors_doc.get('ensemble_valid_ic'),
        artifact_sha256=factors_doc['artifact_sha256'],
        status=status_doc['status'], steps_completed=status_doc['steps_completed'],
        manifest_path=manifest_path,
        evaluations=int(status_doc.get('evaluations', 0)),
        expr_errors=int(status_doc.get('expr_errors', 0)),
        valid_evals=int(status_doc.get('valid_evals', 0)),
        stop_reason=status_doc.get('stop_reason'),
        resumed=bool(status_doc.get('resumed', request.resume)),
        resumed_from_steps=status_doc.get('resumed_from_steps'),
        checkpoint_latest=status_doc.get('checkpoint_latest'),
        features=status_doc.get('features', []))


def evaluate_artifact(artifact: MiningArtifact, test_panel: pd.DataFrame, *,
                      split: dict[str, tuple[str, str]], output: Path | None = None,
                      label: str = 'paper') -> dict:
    """Evaluate frozen factors in a separate process; no feedback to the miner."""
    output = Path(output or (artifact.output_dir / 'test_eval'))
    output.mkdir(parents=True, exist_ok=True)
    test_path = output / 'test_panel.parquet'
    test_panel.to_parquet(test_path, index=False)
    request = {
        'factors_path': str(artifact.output_dir / 'factors.json'),
        'panel_path': str(test_path),
        'start': split['test'][0], 'end': split['test'][1],
        'label': label, 'output': str(output),
        'max_backtrack_days': 100, 'max_future_days': 25,
    }
    req_path = output / 'eval_request.json'
    req_path.write_text(json.dumps(request, ensure_ascii=False, indent=2) + '\n')
    proc = subprocess.run(
        [sys.executable, str(ROOT / 'scripts/alphaqcm_evaluate.py'),
         '--request', str(req_path)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0:
        raise RuntimeError(f'AlphaQCM 评测失败: {proc.stderr[-2000:]}')
    return json.loads((output / 'metrics.json').read_text())


class AlphaQCMMiner:
    """Minimal propose / evaluate / export contract used by the research CLI."""

    def propose(self, request: MiningRequest) -> MiningArtifact:
        return run_miner(request)

    def evaluate(self, artifact: MiningArtifact, test_panel: pd.DataFrame, *,
                 split: dict[str, tuple[str, str]], output: Path | None = None,
                 label: str = 'paper') -> dict:
        return evaluate_artifact(artifact, test_panel, split=split, output=output, label=label)

    def export(self, artifact: MiningArtifact) -> Path:
        """Return the frozen factor artifact; never rewrite it after testing."""
        path = artifact.output_dir / 'factors.json'
        if not path.exists():
            raise FileNotFoundError(path)
        return path
