"""Linux parent lifetime guards for fixed picker children."""

PARENT_GUARD = """
import ctypes,os,signal,sys
parent=int(sys.argv.pop(1))
libc=ctypes.CDLL(None,use_errno=True)
libc.prctl.argtypes=[ctypes.c_int]+[ctypes.c_ulong]*4
libc.prctl.restype=ctypes.c_int
if libc.prctl(1,signal.SIGTERM,0,0,0)!=0 or os.getppid()!=parent:
    raise SystemExit(1)
"""

EXEC_ENTRY = PARENT_GUARD + "os.execv(sys.argv[1],sys.argv[1:])\n"
