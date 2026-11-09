"""Process-local sliding-window breaker with one fenced half-open probe."""
from collections import deque
from dataclasses import dataclass
import math
import time


@dataclass(frozen=True)
class Permit:
    epoch: int
    probe: bool


class CircuitBreaker:
    def __init__(self, *, window=30.0, minimum=5, failure_ratio=.6, cooldown=10.0, clock=time.monotonic):
        if (not math.isfinite(window) or window <= 0 or minimum < 1 or
            not 0 < failure_ratio <= 1 or not math.isfinite(cooldown) or cooldown <= 0):
            raise ValueError('invalid circuit policy')
        self.window, self.minimum, self.ratio, self.cooldown = window, minimum, failure_ratio, cooldown
        self.clock = clock
        self.samples = deque(maxlen=128)
        self.state, self.epoch, self.open_until, self.probe = 'closed', 0, 0.0, False

    def _prune(self):
        now = self.clock()
        while self.samples and self.samples[0][0] <= now - self.window: self.samples.popleft()

    def acquire(self):
        self._prune()
        if self.state == 'closed': return Permit(self.epoch, False)
        if self.clock() < self.open_until or self.probe: return None
        self.state, self.probe = 'half_open', True
        return Permit(self.epoch, True)

    def _open(self):
        self.epoch += 1
        self.state, self.probe = 'open', False
        self.open_until = self.clock() + self.cooldown

    def record(self, permit, *, success):
        if permit.epoch != self.epoch: return  # Stale completion cannot close/reopen a newer circuit.
        if permit.probe:
            if self.state != 'half_open' or not self.probe: return
            if success:
                self.epoch += 1
                self.state, self.probe = 'closed', False
                self.samples.clear()
            else: self._open()
            return
        if self.state != 'closed': return
        self._prune(); self.samples.append((self.clock(), not success))
        if len(self.samples) >= self.minimum and sum(failed for _, failed in self.samples) / len(self.samples) >= self.ratio:
            self._open()

    def abandon(self, permit):
        # Caller cancellation/admission is not a dependency failure.
        if permit.epoch == self.epoch and permit.probe and self.probe:
            self.epoch += 1
            self.state, self.probe, self.open_until = 'open', False, self.clock()

    def snapshot(self):
        self._prune()
        return {'state': self.state, 'samples': len(self.samples),
                'failures': sum(f for _, f in self.samples), 'probe_inflight': self.probe,
                'retry_after_seconds': max(0.0, self.open_until - self.clock()) if self.state != 'closed' else 0.0}
