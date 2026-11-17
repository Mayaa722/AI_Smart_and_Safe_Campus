"""Durable local lecture worker: independent of Streamlit's browser session."""
import os
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime, timezone
from lecture_workflow import PROJECT, LectureWorkflow, read, write
from request_recovery import transient_failure


def process_alive(pid):
    if not pid:return False
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.OpenProcess(0x1000,False,int(pid))
        if not handle:return False
        try:
            code=wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle,ctypes.byref(code))) and code.value==259
        finally:kernel.CloseHandle(handle)
    try:os.kill(int(pid),0);return True
    except OSError:return False


def worker_status(root):
    status=read(root/'worker_status.json',{})
    if status.get('state')=='running' and not process_alive(status.get('pid')):
        return {**status,'state':'paused','message':'Processing stopped. Saved work is intact; resume to continue.'}
    return status


def start_worker(workflow):
    if worker_status(workflow.root).get('state')=='running':return
    # O_EXCL prevents two browser clicks starting two workers.
    launch=workflow.root/'worker_launch.lock'
    try:fd=os.open(launch,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    except FileExistsError:
        if time.time()-launch.stat().st_mtime<30:return
        launch.unlink(missing_ok=True)
        return start_worker(workflow)
    try:
        python=PROJECT.parent/'.venv/Scripts/python.exe' if os.name=='nt' else Path(sys.executable)
        with (workflow.root/'worker.log').open('ab') as log:
            process=subprocess.Popen([str(python),str(PROJECT/'lecture_worker.py'),str(workflow.config_path)],
                cwd=PROJECT,stdout=log,stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        write(workflow.root/'worker_status.json',{'state':'running','pid':process.pid,'message':'Starting lecture processing…'})
    finally:os.close(fd);launch.unlink(missing_ok=True)


def run(config_path):
    from trial_support import prepare_lecture
    w=LectureWorkflow(config_path)
    def status(state,message,**extra):
        write(w.root/'worker_status.json',{'state':state,'pid':os.getpid(),
            'message':message,'updated_at':datetime.now(timezone.utc).isoformat(),**extra})
    try:
        # Automatic recovery has a finite number of rounds and the persistent
        # request/token ledger remains authoritative across launches.
        for cycle in range(3):
            status('running','Resuming saved lecture processing',round=cycle+1)
            if prepare_lecture(w,lambda msg:status('running',str(msg),round=cycle+1)):
                needs_review=any(a.get('content_review',{}).get('status')=='needs_review' for a in w.state['artifacts'].values())
                status('review_required' if needs_review else 'complete',
                    'Materials are generated. Flagged content needs review against the source.' if needs_review else 'Study guide and quiz are ready.');return
            failure=' '.join(w.state.get('last_processing_error',{}).get('warnings',[]))
            content_failure=any(term in failure for term in ['Quiz source review needs attention','Content review needs','content-review flags'])
            if content_failure and all(w.state['artifacts'].get(k,{}).get('parsed') for k in ['study_guide','lecture_quiz']) and any(
                a.get('content_review',{}).get('status')=='needs_review' for a in w.state['artifacts'].values()):
                status('review_required','Materials are generated. The saved draft has content-review flags; inspect them before use.');return
            if not transient_failure(failure):break
            if cycle<2:
                status('running','Service temporarily unavailable. Retrying automatically in 60 seconds; completed work is saved.')
                time.sleep(60)
        status('paused','The service is still unavailable or needs attention. Saved work is intact; see the last error.')
    except Exception as exc:
        # Do not expose credentials or arbitrary exception payloads in the UI.
        message=str(exc) if isinstance(exc,(ValueError,RuntimeError)) else type(exc).__name__
        status('paused',message[:240])


if __name__=='__main__':run(Path(sys.argv[1]))
