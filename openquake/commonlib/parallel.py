# -*- coding: utf-8 -*-
# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright (c) 2010-2014, GEM Foundation.
#
# OpenQuake is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenQuake is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenQuake.  If not, see <http://www.gnu.org/licenses/>.

"""
TODO: write documentation.
"""

import os
import sys
import cPickle
import logging
import operator
import traceback
import itertools
import time
from datetime import datetime
from concurrent.futures import as_completed, ProcessPoolExecutor

import psutil

from openquake.baselib.general import split_in_blocks, AccumDict


executor = ProcessPoolExecutor()

ONE_MB = 1024 * 1024


def no_distribute():
    """
    True if the variable OQ_NO_DISTRIBUTE is true
    """
    nd = os.environ.get('OQ_NO_DISTRIBUTE', '').lower()
    return nd in ('1', 'true', 'yes')


def check_mem_usage(soft_percent=80, hard_percent=100):
    """
    Display a warning if we are running out of memory

    :param int mem_percent: the memory limit as a percentage
    """
    used_mem_percent = psutil.phymem_usage().percent
    if used_mem_percent > soft_percent:
        logging.warn('Using over %d%% of the memory!', used_mem_percent)
    if used_mem_percent > hard_percent:
        raise MemoryError('Using more memory than allowed by configuration '
                          '(Used: %d%% / Allowed: %d%%)! Shutting down.' %
                          (used_mem_percent, hard_percent))


def safely_call(func, args, pickle=False):
    """
    Call the given function with the given arguments safely, i.e.
    by trapping the exceptions. Return a pair (result, exc_type)
    where exc_type is None if no exceptions occur, otherwise it
    is the exception class and the result is a string containing
    error message and traceback.

    :param func: the function to call
    :param args: the arguments
    :param pickle:
        if set, the input arguments are unpickled and the return value
        is pickled; otherwise they are left unchanged
    """
    if pickle:
        args = [a.unpickle() for a in args]
    try:
        res = func(*args), None
    except:
        etype, exc, tb = sys.exc_info()
        tb_str = ''.join(traceback.format_tb(tb))
        res = '\n%s%s: %s' % (tb_str, etype.__name__, exc), etype
    if pickle:
        return Pickled(res)
    return res


def log_percent_gen(taskname, todo, progress):
    """
    Generator factory. Each time the generator object is called
    log a message if the percentage is bigger than the last one.
    Yield the number of calls done at the current iteration.

    :param str taskname:
        the name of the task
    :param int todo:
        the number of times the generator object will be called
    :param progress:
        a logging function for the progress report
    """
    progress('spawned %d tasks of kind %s', todo, taskname)
    yield 0
    done = 1
    prev_percent = 0
    while done < todo:
        percent = int(float(done) / todo * 100)
        if percent > prev_percent:
            progress('%s %3d%%', taskname, percent)
            prev_percent = percent
        yield done
        done += 1
    progress('%s 100%%', taskname)
    yield done


class Pickled(object):
    """
    An utility to manually pickling/unpickling objects.
    The reason is that celery does not use the HIGHEST_PROTOCOL,
    so relying on celery is slower. Moreover Pickled instances
    have a nice string representation and length giving the size
    of the pickled bytestring.

    :param obj: the object to pickle
    """
    def __init__(self, obj):
        self.clsname = obj.__class__.__name__
        self.pik = cPickle.dumps(obj, cPickle.HIGHEST_PROTOCOL)

    def __repr__(self):
        """String representation of the pickled object"""
        return '<Pickled %s %dK>' % (self.clsname, len(self) / 1024)

    def __len__(self):
        """Length of the pickled bytestring"""
        return len(self.pik)

    def unpickle(self):
        """Unpickle the underlying object"""
        return cPickle.loads(self.pik)


def get_pickled_sizes(obj):
    """
    Return the pickled sizes of an object and its direct attributes,
    ordered by decreasing size. Here is an example:

    >> total_size, partial_sizes = get_pickled_sizes(PerformanceMonitor(''))
    >> total_size
    345
    >> partial_sizes
    [('_procs', 214), ('exc', 4), ('mem', 4), ('start_time', 4),
    ('_start_time', 4), ('duration', 4)]

    Notice that the sizes depend on the operating system and the machine.
    """
    sizes = []
    attrs = getattr(obj, '__dict__',  {})
    for name, value in attrs.iteritems():
        sizes.append((name, len(Pickled(value))))
    return len(Pickled(obj)), sorted(
        sizes, key=lambda pair: pair[1], reverse=True)


def pickle_sequence(objects):
    """
    Convert an iterable of objects into a list of pickled objects.
    If the iterable contains copies, the pickling will be done only once.
    If the iterable contains objects already pickled, they will not be
    pickled again.

    :param objects: a sequence of objects to pickle
    """
    cache = {}
    out = []
    for obj in objects:
        obj_id = id(obj)
        if obj_id not in cache:
            if isinstance(obj, Pickled):  # already pickled
                cache[obj_id] = obj
            else:  # pickle the object
                cache[obj_id] = Pickled(obj)
        out.append(cache[obj_id])
    return out


class TaskManager(object):
    """
    A manager to submit several tasks of the same type.
    The usage is::

      tm = TaskManager(do_something, logging.info)
      tm.send(arg1, arg2)
      tm.send(arg3, arg4)
      print tm.reduce()

    Progress report is built-in.
    """
    executor = executor

    @classmethod
    def restart(cls):
        cls.executor.shutdown()
        cls.executor = ProcessPoolExecutor()

    @classmethod
    def starmap(cls, task_func, task_args, progress=logging.info, name=None):
        """
        Spawn a bunch of tasks with the given list of arguments

        :returns: a TaskManager object with a .result method.
        """
        self = cls(task_func, progress, name)
        for i, a in enumerate(task_args, 1):
            progress('Submitting task %s #%d', self.name, i)
            self.submit(*a)
        return self

    def __init__(self, oqtask, progress=logging.info, name=None):
        self.oqtask = oqtask
        self.progress = progress
        self.name = name or oqtask.__name__
        self.results = []
        self.sent = 0
        self.received = 0

    def submit(self, *args):
        """
        Submit a function with the given arguments to the process pool
        and add a Future to the list `.results`. If the variable
        OQ_NO_DISTRIBUTE is set, the function is run in process and the
        result is returned.
        """
        check_mem_usage()  # log a warning if too much memory is used
        if no_distribute():
            res = safely_call(self.oqtask, args)
        else:
            res = self.executor.submit(safely_call, self.oqtask, args)
        self.sent += len(Pickled(args))
        self.results.append(res)

    def aggregate_result_set(self, agg, acc):
        """
        Loop on a set of futures and update the accumulator
        by using the aggregation function.

        :param agg: the aggregation function, (acc, val) -> new acc
        :param acc: the initial value of the accumulator
        :returns: the final value of the accumulator
        """
        for future in as_completed(self.results):
            check_mem_usage()  # log a warning if too much memory is used
            acc = agg(acc, future.result())
        return acc

    def reduce(self, agg=operator.add, acc=None):
        """
        Loop on a set of results and update the accumulator
        by using the aggregation function.

        :param agg: the aggregation function, (acc, val) -> new acc
        :param acc: the initial value of the accumulator
        :returns: the final value of the accumulator
        """
        if acc is None:
            acc = AccumDict()
        self.progress('Sent %dM of data', self.sent // ONE_MB)
        log_percent = log_percent_gen(
            self.name, len(self.results), self.progress)
        log_percent.next()

        def agg_and_percent(acc, (val, exc)):
            if exc:
                raise RuntimeError(val)
            res = agg(acc, val)
            log_percent.next()
            return res

        if no_distribute():
            agg_result = reduce(agg_and_percent, self.results, acc)
        else:
            agg_result = self.aggregate_result_set(agg_and_percent, acc)

        self.progress('Received %dM of data', self.received // ONE_MB)
        self.results = []
        return agg_result

    def wait(self):
        """
        Wait until all the task terminate. Discard the results.

        :returns: the total number of tasks that were spawned
        """
        return self.reduce(self, lambda acc, res: acc + 1, 0)

    def __iter__(self):
        """
        An iterator over the results
        """
        return iter(self.results)

# a convenient alias
starmap = TaskManager.starmap


def apply_reduce(task_func, task_args, agg=operator.add, acc=None,
                 concurrent_tasks=executor._max_workers,
                 weight=lambda item: 1,
                 key=lambda item: 'Unspecified',
                 name=None):
    """
    Apply a function to a tuple of the form (sequence, \*other_args)
    by first splitting the sequence in chunks, according to the weight
    of the elements and possibly to a key (see :function:
    `openquake.baselib.general.split_in_blocks`).
    Then reduce the results with an aggregation function. Here is an example:

    >>> apply_reduce(sum, ([1, 2, 3, 4, 5],), lambda acc, x: acc + x,
    ...             acc=0, concurrent_tasks=2)
    15

    The chunks which are generated internally can be seen directly (
    useful for debugging purposes) by looking at the attribute `._chunks`,
    right after the `apply_reduce` function has been called:

    >>> apply_reduce._chunks
    [<WeightedSequence [1, 2, 3], weight=3>, <WeightedSequence [4, 5], weight=2>]

    :param task_func: a function to run in parallel
    :param task_args: the arguments to be passed to the task function
    :param agg: the aggregation function
    :param acc: initial value of the accumulator (default empty AccumDict)
    :param concurrent_tasks: hint about how many tasks to generate
    :param weight: function to extract the weight of an item in arg0
    :param key: function to extract the kind of an item in arg0
    """
    arg0 = task_args[0]
    args = task_args[1:]
    if acc is None:
        acc = AccumDict()
    if not arg0:
        return acc
    elif len(arg0) == 1 or not concurrent_tasks:
        return agg(acc, task_func(arg0, *args))
    chunks = list(split_in_blocks(arg0, concurrent_tasks, weight, key))
    apply_reduce._chunks = chunks
    tm = starmap(task_func, [(chunk,) + args for chunk in chunks],
                 logging.info, name)
    return tm.reduce(agg, acc)


def do_not_aggregate(acc, value):
    """
    Do nothing aggregation function, use it in
    :class:`openquake.commonlib.parallel.apply_reduce` calls
    when no aggregation is required.

    :param acc: the accumulator
    :param value: the value to accumulate
    :returns: the accumulator unchanged
    """
    return acc


# this is not thread-safe
class PerformanceMonitor(object):
    """
    Measure the resident memory occupied by a list of processes during
    the execution of a block of code. Should be used as a context manager,
    as follows::

     with PerformanceMonitor('do_something') as mm:
         do_something()
     deltamemory, = mm.mem

    At the end of the block the PerformanceMonitor object will have the
    following 5 public attributes:

    .start_time: when the monitor started (a datetime object)
    .duration: time elapsed between start and stop (in seconds)
    .exc: None unless an exception happened inside the block of code
    .mem: an array with the memory deltas (in bytes)

    The memory array has the same length as the number of processes.
    The behaviour of the PerformanceMonitor can be customized by subclassing it
    and by overriding the method on_exit(), called at end and used to display
    or store the results of the analysis.
    """
    def __init__(self, operation, pid=None, monitor_csv='performance_csv'):
        self.operation = operation
        self.pid = pid
        self.monitor_csv = monitor_csv
        if pid:
            self._proc = psutil.Process(pid)
        else:
            self._proc = None

    def write(self, row):
        """Write a row on the performance file"""
        open(self.monitor_csv, 'a').write('\t'.join(row) + '\n')

    def measure_mem(self):
        """A memory measurement (in bytes)"""
        try:
            if self._proc:
                return self._proc.get_memory_info().rss
        except psutil.AccessDenied:
            # no access to information about this process
            # don't not try to check it anymore
            self._proc = None

    @property
    def start_time(self):
        """
        Datetime instance recording when the monitoring started
        """
        return datetime.fromtimestamp(self._start_time)

    def __enter__(self):
        """Call .start"""
        if self._proc is None:
            pid = os.getpid()
            self.pid = pid
            self._proc = psutil.Process(pid)
        self.duration = None  # seconds
        self.mem = None  # bytes
        self.exc = None  # exception
        self._start_time = time.time()
        self.start_mem = self.measure_mem()
        return self

    def __exit__(self, etype, exc, tb):
        "Call .stop"
        self.exc = exc
        self.stop_mem = self.measure_mem()
        self.mem = self.stop_mem - self.start_mem
        self.duration = time.time() - self._start_time
        self.on_exit()

    def on_exit(self):
        "Save the results: to be overridden in subclasses"
        time_sec = str(self.duration)
        memory_mb = str(self.mem / 1024. / 1024.)
        self.write([self.operation, str(self.pid), time_sec, memory_mb])

    def __call__(self, operation):
        """
        Return a copy of the monitor usable for a different operation
        in the same task.
        """
        return self.__class__(operation, monitor_csv=self.monitor_csv)


class DummyMonitor(PerformanceMonitor):
    """
    This class makes it easy to disable the monitoring in client code.
    Disabling the monitor can improve the performance.
    """
    def __init__(self, operation='', *args, **kw):
        self.operation = operation

    def write(self, row):
        """Do nothing"""

    def __call__(self, operation):
        return self.__class__(operation)

    def __enter__(self):
        return self

    def __exit__(self, etype, exc, tb):
        pass