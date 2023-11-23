"""
This module contains the `ProcessExecution` class, an implementation of the `Execution` abstract class, used to
execute code in a separate process using the `multiprocessing` package from the standard library.
"""

import logging
import signal
import sys
import traceback
from contextlib import contextmanager
from multiprocessing import Queue
from multiprocessing.context import Process
from queue import Full
from typing import Union, Tuple

from tarotools.taro.execution import OutputExecution, ExecutionResult, ExecutionException
from tarotools.taro.util.observer import CallableNotification

log = logging.getLogger(__name__)


class ProcessExecution(OutputExecution):

    def __init__(self, target, args=(), tracking=None):
        self.target = target
        self.args = args
        self._tracking = tracking
        self.output_queue: Queue[Tuple[Union[str, _QueueStop], bool]] = Queue(maxsize=2048)  # Create in execute method?
        self._process: Union[Process, None] = None
        self._status = None
        self._stopped: bool = False
        self._interrupted: bool = False
        self._output_notification = CallableNotification()

    def execute(self) -> ExecutionResult:
        if not self._stopped and not self._interrupted:
            self._process = Process(target=self._run)

            try:
                self._process.start()
                self._read_output()
                self._process.join()  # Just in case as it should be completed at this point
            finally:
                self.output_queue.close()

            if self._process.exitcode == 0:
                return ExecutionResult.DONE

        if self._interrupted or self._process.exitcode == -signal.SIGINT:
            # Exit code is -SIGINT only when SIGINT handler is set back to DFL (KeyboardInterrupt gets exit code 1)
            return ExecutionResult.INTERRUPTED
        if self._stopped or self._process.exitcode < 0:  # Negative exit code means terminated by a signal
            return ExecutionResult.STOPPED
        raise ExecutionException("Process returned non-zero code " + str(self._process.exitcode))

    def _run(self):
        with self._capture_stdout():
            try:
                self.target(*self.args)
            except:
                for line in traceback.format_exception(*sys.exc_info()):
                    self.output_queue.put_nowait((line, True))
                raise
            finally:
                self.output_queue.put_nowait((_QueueStop(), False))

    @contextmanager
    def _capture_stdout(self):
        import sys
        original_stdout = sys.stdout
        original_stderr = sys.stderr
        stdout_writer = _CapturingWriter(original_stdout, False, self.output_queue)
        stderr_writer = _CapturingWriter(original_stderr, True, self.output_queue)
        sys.stdout = stdout_writer
        sys.stderr = stderr_writer

        try:
            yield
        finally:
            sys.stdout = original_stdout
            sys.stderr = original_stderr

    @property
    def tracking(self):
        return self._tracking

    @tracking.setter
    def tracking(self, tracking):
        self._tracking = tracking

    @property
    def status(self):
        if self.tracking:
            return str(self.tracking)
        else:
            return self._status

    @property
    def parameters(self):
        return ('execution', 'process'),

    def stop(self):
        self._stopped = True
        self.output_queue.put_nowait((_QueueStop(), False))
        if self._process:
            self._process.terminate()

    def interrupted(self):
        self._interrupted = True

    def add_callback_output(self, callback):
        self._output_notification.add_observer(callback)

    def remove_callback_output(self, callback):
        self._output_notification.remove_observer(callback)

    def _read_output(self):
        while True:
            output_text, is_err = self.output_queue.get()
            if isinstance(output_text, _QueueStop):
                break
            self._status = output_text
            self._output_notification(output_text, is_err)


class _CapturingWriter:

    def __init__(self, out, is_err, output_queue):
        self.out = out
        self.is_err = is_err
        self.output_queue = output_queue

    def write(self, text):
        text_s = text.rstrip()
        if text_s:
            try:
                self.output_queue.put_nowait((text_s, self.is_err))
            except Full:
                # TODO what to do here?
                log.warning("event=[output_queue_full]")
        self.out.write(text)


class _QueueStop:
    """Poison object signalizing no more objects will be put in the queue"""
    pass
