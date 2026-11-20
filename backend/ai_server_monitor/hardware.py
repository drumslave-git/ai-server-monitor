"""What the hardware is doing now, for the meters in the page's sidebar: CPU and RAM through
psutil, NVIDIA GPUs through one `nvidia-smi` call in the jobs' read-only host shell. Nothing
is stored, logged or traced; every request reads anew."""

from __future__ import annotations

from typing import TYPE_CHECKING

import psutil

from .agent.tools import app_shell
from .api_models import GpuUse, HardwareResponse

if TYPE_CHECKING:
    from .context import AppContext

GPU_QUERY = (
    "nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total "
    "--format=csv,noheader,nounits"
)
MIB = 1024 * 1024

# psutil measures CPU use between two calls; the first call has nothing to compare with and
# answers 0.0. This first call at start makes the first request's answer the use since start.
psutil.cpu_percent(interval=None)


def parse_gpus(output: str) -> list[GpuUse]:
    """One GPU per line of `GPU_QUERY`'s output: "name, util %, used MiB, total MiB". A line
    whose numbers cannot be read (e.g. "[N/A]") is left out."""
    gpus = []
    for line in output.splitlines():
        parts = line.rsplit(",", 3)
        if len(parts) != 4:
            continue
        name, util, used, total = (p.strip() for p in parts)
        try:
            gpus.append(
                GpuUse(
                    name=name,
                    util_percent=float(util),
                    vram_used=int(float(used) * MIB),
                    vram_total=int(float(total) * MIB),
                )
            )
        except ValueError:
            continue
    return gpus


async def gpu_use(app: AppContext) -> list[GpuUse]:
    """The NVIDIA GPUs as `nvidia-smi` lists them; none when it is missing, fails or lists
    nothing. Runs in the same shell and with the same time and output limits as the jobs'
    `bash`, but not as a tool: no approval, no audit or log row."""
    chat = app.settings.chat
    result = await app_shell(app).run(
        GPU_QUERY,
        timeout_s=min(chat.command_timeout.total_seconds(), chat.tool_timeout.total_seconds()),
        max_chars=max(chat.max_tool_result_chars - 400, 400),
    )
    if not result.ok:
        return []
    return parse_gpus(result.output)


async def hardware(app: AppContext) -> HardwareResponse:
    memory = psutil.virtual_memory()
    return HardwareResponse(
        cpu_percent=psutil.cpu_percent(interval=None),
        ram_used=memory.total - memory.available,
        ram_total=memory.total,
        gpus=await gpu_use(app),
    )
