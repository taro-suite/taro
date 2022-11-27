"""
Implementation of job management framework based on :mod:`job` module.
"""
import contextlib
import copy
import logging
from collections import deque, Counter
from itertools import chain
from operator import itemgetter
from threading import RLock
from typing import List, Union, Callable, Tuple

import taro.client
from taro import util
from taro.jobs import persistence
from taro.jobs.execution import ExecutionError, ExecutionState, ExecutionLifecycleManagement, ExecutionOutputObserver, \
    UnexpectedStateError
from taro.jobs.job import ExecutionStateObserver, JobInstance, JobInfo, WarningObserver, JobOutputObserver, Warn, \
    WarnEventCtx, JobInstanceID, DEFAULT_OBSERVER_PRIORITY
from taro.jobs.sync import NoSync, CompositeSync, Latch, Signal

log = logging.getLogger(__name__)


def run(job_id, execution, state_locker):
    instance = RunnerJobInstance(job_id, execution, state_locker)
    instance.run()
    return instance


def _add_prioritized(prioritized_seq, priority, item):
    return sorted(chain(prioritized_seq, [(priority, item)]), key=itemgetter(0))


def _remove_prioritized(prioritized_seq, item):
    return [(priority, i) for priority, i in prioritized_seq if i != item]


def _gen_prioritized(*prioritized_seq):
    return (item for _, item in chain(*prioritized_seq))


class RunnerJobInstance(JobInstance, ExecutionOutputObserver):

    def __init__(self, job_id, execution, state_locker, sync=NoSync(), *, pending_value=None, depends_on=None, **params):
        self._id = JobInstanceID(job_id, util.unique_timestamp_hex())
        self._params = params
        self._execution = execution
        self._global_state_locker = state_locker
        self._pending_value = pending_value
        if pending_value:
            self._latch = Latch(ExecutionState.PENDING)
            self._sync = CompositeSync((self._latch, sync))
        else:
            self._sync = sync
        self._depends_on = depends_on
        self._lifecycle: ExecutionLifecycleManagement = ExecutionLifecycleManagement()
        self._last_output = deque(maxlen=10)
        self._exec_error = None
        self._executing = False
        self._stopped_or_interrupted: bool = False
        self._state_lock: RLock = RLock()
        self._warnings = Counter()
        self._state_observers = []
        self._warning_observers = []
        self._output_observers = []

        self._state_change(ExecutionState.CREATED)

    @property
    def id(self):
        return self._id

    @property
    def lifecycle(self):
        return self._lifecycle

    @property
    def status(self):
        return self._execution.status

    @property
    def last_output(self) -> List[str]:
        return list(self._last_output)

    @property
    def warnings(self):
        return dict(self._warnings)

    @property
    def exec_error(self) -> Union[ExecutionError, None]:
        return self._exec_error

    def create_info(self):
        with self._state_lock:
            return JobInfo(self._id, copy.deepcopy(self._lifecycle), self.status, self.warnings, self.exec_error,
                           **self._params)

    def add_state_observer(self, observer, priority=DEFAULT_OBSERVER_PRIORITY):
        self._state_observers = _add_prioritized(self._state_observers, priority, observer)

    def remove_state_observer(self, observer):
        self._state_observers = _remove_prioritized(self._state_observers, observer)

    def add_warning_observer(self, observer, priority=DEFAULT_OBSERVER_PRIORITY):
        self._warning_observers = _add_prioritized(self._warning_observers, priority, observer)

    def remove_warning_observer(self, observer):
        self._warning_observers = _remove_prioritized(self._warning_observers, observer)

    def add_output_observer(self, observer, priority=DEFAULT_OBSERVER_PRIORITY):
        self._output_observers = _add_prioritized(self._output_observers, priority, observer)

    def remove_output_observer(self, observer):
        self._output_observers = _remove_prioritized(self._output_observers, observer)

    def release(self, pending_value=None):
        if not self._pending_value or (pending_value and pending_value != self._pending_value):
            return False

        self._latch.release()
        return True

    def stop(self):
        """
        Cancel not yet started execution or stop started execution.
        Due to synchronous design there is a small window when an execution can be stopped before it is started.
        All execution implementations must cope with such scenario.
        """
        self._stopped_or_interrupted = True

        self._sync.release()
        if self._executing:
            self._execution.stop()

    def interrupt(self):
        """
        Cancel not yet started execution or interrupt started execution.
        Due to synchronous design there is a small window when an execution can be interrupted before it is started.
        All execution implementations must cope with such scenario.
        """
        self._stopped_or_interrupted = True

        self._sync.release()
        if self._executing:
            self._execution.interrupt()

    def add_warning(self, warning):
        self._warnings.update([warning.name])
        log.warning('event=[new_warning] warning=[%s]', warning)
        self._notify_warning_observers(self.create_info(), warning, WarnEventCtx(self._warnings[warning.name]))  # Lock?

    def run(self):
        # TODO Check executed only once

        try:
            synchronized = self._synchronize()
        except Exception as e:
            log.error('event=[sync_error]', exc_info=e)
            self._state_change(ExecutionState.ERROR)
            return

        if not synchronized:
            return

        if self._depends_on:
            try:
                jobs = taro.client.read_jobs_info()
                if self._depends_on and not any(j for j in jobs
                                                if any(j.matches(dependency) for dependency in self._depends_on)):
                    self._state_change(ExecutionState.DEPENDENCY_NOT_RUNNING)
                    return
            except Exception as e:
                log.warning("event=[overlap_check_failed] error=[%s]", e)

        # Forward output from execution to the job instance for the instance's output listeners
        self._execution.add_output_observer(self)

        try:
            new_state = self._execution.execute()
            self._state_change(new_state)
        except Exception as e:
            exec_error = e if isinstance(e, ExecutionError) else ExecutionError.from_unexpected_error(e)
            self._state_change(exec_error.exec_state, exec_error)
        except SystemExit as e:
            state = ExecutionState.COMPLETED if e.code == 0 else ExecutionState.FAILED  # TODO Different states?
            self._state_change(state)
            raise

    def _synchronize(self) -> bool:
        while True:
            with self._global_state_locker() as state_lock:
                if self._stopped_or_interrupted:
                    signal = Signal.TERMINATE
                    state = ExecutionState.CANCELLED
                else:
                    signal = self._sync.set_signal(self.create_info())
                    if signal is Signal.NONE:
                        assert False  # TODO raise exception

                    if signal is Signal.CONTINUE:
                        # TODO remove the async condition
                        state = ExecutionState.TRIGGERED if self._execution.is_async else ExecutionState.RUNNING
                    else:
                        state = self._sync.exec_state
                        if not (state.is_waiting() or state.is_terminal()):
                            raise UnexpectedStateError(f"Unsupported state returned from sync: {state}")

                self._state_change(state, use_global_lock=False)

                if signal is Signal.WAIT:
                    self._sync.wait_and_unlock(state_lock)
                    continue  # Repeat as waiting can be still needed or another waiting condition must be evaluated
                if signal is Signal.TERMINATE:
                    return False

                self._executing = True
                return True

    # Inline?
    def _log(self, event: str, msg: str):
        return "event=[{}] instance=[{}] {}".format(event, self._id, msg)

    def _state_change(self, new_state, exec_error: ExecutionError = None, *, use_global_lock=True):
        if exec_error:
            self._exec_error = exec_error
            if exec_error.exec_state == ExecutionState.ERROR or exec_error.unexpected_error:
                log.exception(self._log('job_error', "reason=[{}]".format(exec_error)), exc_info=True)
            else:
                log.warning(self._log('job_not_completed', "reason=[{}]".format(exec_error)))

        global_state_lock = self._global_state_locker() if use_global_lock else contextlib.nullcontext()
        job_info = None
        # It is not necessary to lock all this code, but it would be if this method is not confined to one thread
        # However locking is still needed for correct creation of job info when job_info method is called (anywhere)
        with self._state_lock:
            with global_state_lock:
                prev_state = self._lifecycle.state
                if self._lifecycle.set_state(new_state):
                    level = logging.WARN if new_state.is_failure() or new_state.is_unexecuted() else logging.INFO
                    log.log(level, self._log('job_state_changed', "prev_state=[{}] new_state=[{}]".format(
                        prev_state.name, new_state.name)))
                    job_info = self.create_info()  # Be sure both new_state and exec_error are already set

        if job_info:
            if new_state.is_terminal() and persistence.is_enabled():
                persistence.store_job(job_info)
            self._notify_state_observers(job_info)

    def _notify_state_observers(self, job_info: JobInfo):
        for observer in _gen_prioritized(self._state_observers, _state_observers):
            # noinspection PyBroadException
            try:
                if isinstance(observer, ExecutionStateObserver):
                    observer.state_update(job_info)
                elif callable(observer):
                    observer(job_info)
                else:
                    log.warning("event=[unsupported_state_observer] observer=[%s]", observer)
            except BaseException:
                log.exception("event=[state_observer_exception]")

    def _notify_warning_observers(self, job_info: JobInfo, warning: Warn, event_ctx: WarnEventCtx):
        for observer in _gen_prioritized(self._warning_observers, _warning_observers):
            # noinspection PyBroadException
            try:
                if isinstance(observer, WarningObserver):
                    observer.new_warning(job_info, warning, event_ctx)
                elif callable(observer):
                    observer(job_info, warning, event_ctx)
                else:
                    log.warning("event=[unsupported_warning_observer] observer=[%s]", observer)
            except BaseException:
                log.exception("event=[warning_observer_exception]")

    def output_update(self, output):
        """Executed when new output line is available"""
        self._last_output.append(output)
        self._notify_output_observers(self.create_info(), output)

    def _notify_output_observers(self, job_info: JobInfo, output):
        for observer in _gen_prioritized(self._output_observers, _output_observers):
            # noinspection PyBroadException
            try:
                if isinstance(observer, JobOutputObserver):
                    observer.output_update(job_info, output)
                elif callable(observer):
                    observer(job_info, output)
                else:
                    log.warning("event=[unsupported_output_observer] observer=[%s]", observer)
            except BaseException:
                log.exception("event=[output_observer_exception]")


_state_observers: List[Union[Tuple[int, ExecutionStateObserver], Tuple[int, Callable]]] = []
_warning_observers: List[Union[Tuple[int, WarningObserver], Tuple[int, Callable]]] = []
_output_observers: List[Union[Tuple[int, JobOutputObserver], Tuple[int, Callable]]] = []


def register_state_observer(observer, priority=DEFAULT_OBSERVER_PRIORITY):
    global _state_observers
    _state_observers = _add_prioritized(_state_observers, priority, observer)


def deregister_state_observer(observer):
    global _state_observers
    _state_observers = _remove_prioritized(_state_observers, observer)


def register_warning_observer(observer, priority=DEFAULT_OBSERVER_PRIORITY):
    global _warning_observers
    _warning_observers = _add_prioritized(_warning_observers, priority, observer)


def deregister_warning_observer(observer):
    global _warning_observers
    _warning_observers = _remove_prioritized(_warning_observers, observer)


def register_output_observer(observer, priority=DEFAULT_OBSERVER_PRIORITY):
    global _output_observers
    _output_observers = _add_prioritized(_output_observers, priority, observer)


def deregister_output_observer(observer):
    global _output_observers
    _output_observers = _remove_prioritized(_output_observers, observer)
