#!/usr/bin/env python

"""
Classes for managing MPI-based processes.
"""

import inspect
import os
import sys

# Use dill for mpi4py object serialization to accomodate a wider range of argument
# possibilities than possible with pickle:
import dill

# Fix for bug https://github.com/uqfoundation/dill/issues/81
@dill.register(property)
def save_property(pickler, obj):
    pickler.save_reduce(property, (obj.fget, obj.fset, obj.fdel), obj=obj)

import twiggy
from mpi4py import MPI
MPI.pickle.dumps = dill.dumps
MPI.pickle.loads = dill.loads

from mixins import LoggerMixin
from tools.logging import set_excepthook
from tools.misc import memoized_property

def funcvars(x):
    """
    Find objects accessed by a function or method.
    """

    # Find symbols accessed by specified function or method:
    results = {}
    if inspect.isfunction(x):
        name_list = x.func_code.co_names
        global_dict = x.func_globals
    elif inspect.ismethod(x):
        name_list = x.im_func.func_code.co_names
        global_dict = x.im_func.func_globals
    else:
        raise ValueError('invalid input')
    for name in name_list:

        # Symbol is a name of a global:
        if name in global_dict:
            results[name] = global_dict[name]
        else:

            # Check if symbol is the name is an attribute of a module (i.e., can
            # be imported):
            for r in results.keys():
                if hasattr(results[r], name) and \
                   inspect.ismodule(getattr(results[r], name)):
                    results[r+'.'+name] = getattr(results[r], name)
    return results

def allglobalvars(x):
    """
    Find all globals accessed by an object.
    """

    results = {}
    if inspect.isroutine(x):
        results = funcvars(x)
    else:
        if inspect.isclass(x):
            for b in x.__bases__:
                results.update(allglobalvars(b))
        for f in inspect.getmembers(x, predicate=inspect.ismethod):
            results.update(allglobalvars(f[1]))
    return results

class Process(LoggerMixin):
    """
    Process class.
    """

    def __init__(self, *args, **kwargs):        
        LoggerMixin.__init__(self, 'prc %s' % MPI.COMM_WORLD.Get_rank())
        set_excepthook(self.logger, True)

        self._args = args
        self._kwargs = kwargs

    @memoized_property
    def intracomm(self):
        """
        Intracommunicator to access peer processes.
        """

        return MPI.COMM_WORLD

    @memoized_property
    def intercomm(self):
        """
        Intercommunicator to access parent process.
        """

        return MPI.Comm.Get_parent()

    @memoized_property
    def rank(self):
        """
        MPI process rank.
        """

        return MPI.COMM_WORLD.Get_rank()

    @memoized_property
    def size(self):
        """
        Number of peer processes.
        """

        return MPI.COMM_WORLD.Get_size()

    def run(self):
        """
        Process body.
        """

        pass

    def send_parent(self, data, tag=0):
        """
        Send data to parent process.
        """

        self.intercomm.send(data, 0, tag=tag)

    def recv_parent(self, tag=MPI.ANY_TAG):
        """
        Receive data from parent process.
        """

        return self.intercomm.recv(tag=tag)

    def send_peer(self, data, dest, tag=0):
        """
        Send data to peer process.
        """

        self.intracomm.send(data, dest, tag=tag)

    def recv_peer(self, source=MPI.ANY_SOURCE, tag=MPI.ANY_TAG):
        return self.intracomm.recv(source=source, tag=tag)

class ProcessManager(LoggerMixin):
    """
    Process manager class.
    """

    def __init__(self):
        LoggerMixin.__init__(self, 'man')
        set_excepthook(self.logger, True)

        self._targets = []
        self._args = []
        self._kwargs = []
        self._intercomm = MPI.COMM_NULL

    @property
    def intercomm(self):
        """
        Intercommunicator to spawned processes.

        Notes
        -----
        Set to COMM_NULL until the run() method is called.
        """

        return self._intercomm

    def add(self, target, *args, **kwargs):
        """
        Add target class or function to manager.

        Parameters
        ----------
        target : Process
            Class instantiate and run in MPI process. 
        args : sequence
            Sequential arguments to pass to target class constructor.
        kwargs : dict
            Named arguments to pass to target class constructor.
        """

        assert issubclass(target, Process)
        self._targets.append(target)
        self._args.append(args)
        self._kwargs.append(kwargs)

    def __len__(self):
        return len(self._targets)

    @memoized_property
    def _is_parent(self):
        """
        True if the current MPI process is the spawning parent.
        """

        return MPI.Comm.Get_parent() == MPI.COMM_NULL

    def run(self):
        """
        Spawn MPI processes for and execute each of the managed targets.
        """

        if self._is_parent:
            # Find the file name of the module in which the Process class
            # is instantiated:
            file_name = inspect.stack()[1][1]

            # Find the path to the mpi_backend.py script (which should be in the
            # same directory as this module:
            parent_dir = os.path.dirname(__file__)
            mpi_backend_path = os.path.join(parent_dir, 'mpi_backend.py')

            # Spawn processes:
            self._intercomm = MPI.COMM_SELF.Spawn(sys.executable,
                                            args=[mpi_backend_path, file_name],
                                            maxprocs=len(self))

            # First, transmit twiggy logging emitters to spawned processes so
            # that they can configure their logging facilities:
            for i in xrange(len(self)):
                self._intercomm.send(twiggy.emitters, i)

            # Transmit class to instantiate, globals required by the class, and
            # the constructor args, and kwargs; the backend will wait to receive
            # them and then start running the targets on the appropriate nodes.
            for i in xrange(len(self)):
                target_globals = allglobalvars(self._targets[i])
                data = (self._targets[i], target_globals, 
                        self._args[i], self._kwargs[i])      
                self._intercomm.send(data, i)

    def send(self, data, dest, tag=0):
        """
        Send data to child process.
        """

        self.intercomm.send(data, dest, tag=0)

    def recv(self, tag=MPI.ANY_TAG):
        """
        Receive data from child process.
        """

        return self.intercomm.recv(tag=tag)

if __name__ == '__main__':
    class MyProcess(Process):
        def __init__(self, *args, **kwargs):
            super(MyProcess, self).__init__(*args, **kwargs)
            self.log_info('I am process %d of %d on %s.' % \
                          (MPI.COMM_WORLD.Get_rank(),
                           MPI.COMM_WORLD.Get_size(),
                           MPI.COMM_WORLD.Get_name()))
        
    from tools.logging import setup_logger

    setup_logger(screen=True, multiline=True)
    
    man = ProcessManager()
    man.add(MyProcess, 1, 2, a=3)
    man.add(MyProcess, 4, b=5, c=6)
    man.run()
