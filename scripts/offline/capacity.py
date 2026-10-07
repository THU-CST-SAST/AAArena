"""Dataset workers use the same host admission policy as online experiments."""
import os
from contextlib import contextmanager

@contextmanager
def auxiliary_capacity():
    workers=int(os.environ.get('AA_ARENA_DATASET_WORKERS','4'))
    if workers<1:raise ValueError('AA_ARENA_DATASET_WORKERS must be positive')
    yield workers
