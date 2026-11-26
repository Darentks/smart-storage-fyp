"""Run the bridge in this same process (so its process record names this PID) and keep one short,
redacted report of why it stopped. Used by the Windows launcher, which starts it hidden with no console."""
from pathlib import Path
import runpy
import sys
import traceback

ROOT=Path(__file__).resolve().parents[1]
BRIDGE=ROOT/'VisionValidation_Module/camera_service.py'
ERROR_LOG=ROOT/'.runtime/bridge-startup-error.log'
sys.path.insert(0,str(ROOT/'VisionValidation_Module/backend'))
from src.bridge_runtime import write_startup_error  # noqa: E402


def main(bridge=BRIDGE,error_log=ERROR_LOG):
    try:Path(error_log).unlink()  # a report always describes the latest launch only
    except OSError:pass
    sys.argv=[str(bridge)]+sys.argv[1:]
    try:runpy.run_path(str(bridge),run_name='__main__')
    except KeyboardInterrupt:return 0  # an ordinary stop
    except SystemExit as exc:
        if exc.code in (0,None):return 0
        write_startup_error(error_log,'Exit code '+str(exc.code));return 1
    except BaseException:
        write_startup_error(error_log,traceback.format_exc());return 1
    return 0


if __name__=='__main__':raise SystemExit(main())
