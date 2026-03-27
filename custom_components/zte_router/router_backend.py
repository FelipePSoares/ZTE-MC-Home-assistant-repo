import logging
import subprocess
from pathlib import Path
from typing import Optional

from .const import ROUTER_TYPE_G5_ULTRA
from .g5_ultra_client import G5UltraRouterRunner

LOGGER = logging.getLogger(__name__)
MC_SCRIPT_PATH = str(Path(__file__).resolve().with_name("mc.py"))


def run_router_commands(
    router_type: str,
    ip: str,
    password: str,
    username: Optional[str],
    commands: str,
    phone_number: Optional[str] = None,
    message: Optional[str] = None,
) -> str:
    """Execute router commands using the appropriate backend."""
    if router_type == ROUTER_TYPE_G5_ULTRA:
        runner = G5UltraRouterRunner(ip, password)
        return runner.run_commands(commands, phone=phone_number, message=message)
    return _run_mc_commands(ip, password, username, commands, phone_number, message)


def _run_mc_commands(
    ip: str,
    password: str,
    username: Optional[str],
    commands: str,
    phone_number: Optional[str],
    message: Optional[str],
) -> str:
    """Run the legacy mc.py script via subprocess."""
    username = username or ""
    command_list = [cmd.strip() for cmd in str(commands).split(",") if cmd.strip()]
    cmd = [
        "python3",
        MC_SCRIPT_PATH,
        str(ip),
        str(password),
        ",".join(command_list),
        username,
    ]

    if len(command_list) == 1 and command_list[0] == "8" and phone_number and message:
        cmd.extend([phone_number, message])

    masked_cmd = _mask_sensitive_values(cmd, [3])
    LOGGER.debug("Executing MC router command: %s", masked_cmd)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return result.stdout
    except subprocess.CalledProcessError as err:
        LOGGER.error("MC router command failed: %s", err)
        raise


def _mask_sensitive_values(items, indexes):
    masked = items.copy()
    for index in indexes:
        if 0 <= index < len(masked):
            masked[index] = "*****"
    return masked
