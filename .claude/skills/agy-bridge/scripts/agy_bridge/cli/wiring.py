"""The composition root: bind each port to the adapter that implements it. Only this module names adapters."""
from __future__ import annotations

from ..port import edit_sandbox, launcher, model_runner


def wire() -> None:
    from ..adapters import agy, edit_apply, jobstore, sandbox, workspace
    from ..adapters import edit_sandbox as edit_sandbox_adapter
    from ..adapters import launcher as launcher_adapter
    from ..port import grading_sandbox, job_store, patch_apply
    from ..port import workspace as workspace_port

    model_runner.bind(agy)
    grading_sandbox.bind(sandbox)
    edit_sandbox.bind(edit_sandbox_adapter)
    job_store.bind(jobstore)
    workspace_port.bind(workspace)
    patch_apply.bind(edit_apply)
    launcher.bind(launcher_adapter)
