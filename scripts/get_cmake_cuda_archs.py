#!/usr/bin/env python3

"""
This script determines and formats CUDA architectures suitable for use with CMake's `CUDA_ARCHITECTURES` variable.
It takes a user request (e.g., 'all', 'all-major', 'native', or a specific list of architectures like '75 80 86a')
and filters them based on the capabilities of the `nvcc` compiler found on the system.
"""

import argparse
import logging
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NoReturn, Optional


def die(msg: str) -> NoReturn:
    """Logs an error message and exits with 1."""
    logging.error(msg)
    sys.exit(1)


def get_nvcc_path() -> Path:
    """Finds the nvcc executable in PATH or default location and returns its real Path object."""
    nvcc_path_str = shutil.which("nvcc")
    nvcc_path = Path(nvcc_path_str) if nvcc_path_str else Path("/usr/local/cuda/bin/nvcc")
    return nvcc_path.resolve()


def get_arch_sort_key(arch: str) -> tuple[int, str]:
    """Extracts (numeric_value, full_string) for sorting architecture strings."""
    match = re.match(r"\d+", arch)
    if not match:
        # only sort by string if no numeric match (unlikely)
        return (0, arch)

    # sort by numeric value first (ex: 90, 100), full string second (ex: 90, 90a)
    return (int(match.group()), arch)


def get_nvcc_archs(nvcc_path: Path) -> list[str]:
    """Runs `nvcc -code-ls` and parses the output."""
    cmd = [str(nvcc_path), "-code-ls"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except FileNotFoundError:
        die(f"nvcc command '{str(nvcc_path)}' not found or not executable.")
    except subprocess.CalledProcessError as e:
        die(f"Command '{' '.join(cmd)}' failed:\n{e.stderr}")

    raw_archs: set[str] = set()
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if line.startswith("sm_"):
            raw_archs.add(line[3:])

    if not raw_archs:
        die("Could not parse any architectures (sm_XX) from 'nvcc -code-ls' output.")

    # Sort architectures numerically, then alphabetically (for suffixes)
    sorted_archs = sorted(raw_archs, key=get_arch_sort_key)
    logging.debug(f"nvcc supported archs: {', '.join(sorted_archs)}")
    return sorted_archs


def parse_requested_archs(req_str: str) -> list[str]:
    """Parses comma or space separated architecture string into a list."""
    if not req_str:
        return []
    archs = re.split(r"[\s,]+", req_str.strip())
    filtered_archs = list(filter(None, archs))
    return filtered_archs


def filter_archs_with_min_arch(archs: list[str], min_arch: Optional[int]) -> list[str]:
    """Filters architecture list based on the minimum required major version."""
    if min_arch is None or min_arch <= 0:
        return list(archs)

    filtered_list = [arch for arch in archs if int(re.match(r"(\d+)", arch).group(1)) >= min_arch]
    logging.debug(f"Architectures >= sm_{min_arch}: {', '.join(filtered_list)}")
    return filtered_list


def filter_archs_for_platform(archs: list[str]) -> list[str]:
    """Filters out architectures not suitable for the current platform (iGPUs on x86_64)."""
    current_platform = platform.machine().lower()

    if current_platform not in ["x86_64", "amd64"]:
        return list(archs)  # Return a copy for consistency

    # Potential iGPU architectures (adjust if necessary for future hardware)
    igpu_archs: set[str] = {
        "72",
        "87",
        "101",
        "101a",
    }  # Xavier, Orin, Thor, DGX Spark (placeholders)

    # Log iGPU architectures to be removed
    if removed_igpus := list(set(archs) & igpu_archs):
        logging.debug(f"Removed iGPU archs from x86_64 build: {', '.join(removed_igpus)}")

    # Filter out iGPU architectures
    filtered_list = [arch for arch in archs if arch not in igpu_archs]
    logging.debug(f"Platform supported archs: {', '.join(filtered_list)}")

    return filtered_list


def filter_major_archs(archs: list[str]) -> list[str]:
    """Filters architecture list to include only major versions (ending in 0)."""
    filtered_list = [arch for arch in archs if re.match(r"^\d+0$", arch)]
    logging.debug(f"Major architectures only: {', '.join(filtered_list)}")
    return filtered_list


def validate_user_archs(
    user_archs: list[str],
    nvcc_supported_archs: list[str],
    min_filtered_archs: list[str],
    platform_filtered_archs: list[str],
    min_arch_value: Optional[int],
) -> list[str]:
    """Validates user-provided architectures against supported and filtered lists."""
    if not user_archs:
        die("Requested architecture list is empty.")

    nvcc_supported_set = set(nvcc_supported_archs)
    min_filtered_set = set(min_filtered_archs)
    platform_filtered_set = set(platform_filtered_archs)

    final_valid_archs_str = ", ".join(platform_filtered_archs)
    if not final_valid_archs_str:
        final_valid_archs_str = "<None>"

    validated_user_archs: list[str] = []
    for arch in user_archs:
        if arch not in nvcc_supported_set:
            die(
                f"Requested architecture '{arch}' is not supported by this version of nvcc. "
                f"Valid architectures: {final_valid_archs_str}"
            )
        if arch not in min_filtered_set:
            die(
                f"Requested architecture '{arch}' does not meet minimum requirement "
                f"(sm_{min_arch_value}). Valid architectures: {final_valid_archs_str}"
            )
        if arch not in platform_filtered_set:
            die(
                f"Requested architecture '{arch}' corresponds to an iGPU not supported "
                f"on this platform (x86_64). Valid architectures: {final_valid_archs_str}"
            )
        validated_user_archs.append(arch)
    return validated_user_archs


def generate_sass_ptx_arch_list(target_archs: list[str]) -> list[str]:
    """Formats the final list with -real and -virtual suffixes for CMake."""
    if not target_archs:
        die("Cannot generate SASS/PTX list from empty target architectures.")

    # Ensure architectures are sorted numerically, then alphabetically
    sorted_archs = sorted(target_archs, key=get_arch_sort_key)

    # Find the highest non-specific (a, f suffixes) architecture
    ptx_target_base = ""
    for arch in reversed(sorted_archs):
        if not re.search(r"[a-zA-Z]$", arch):
            ptx_target_base = arch
            break

    # If no specific architecture found, use the highest available
    if not ptx_target_base:
        ptx_target_base = sorted_archs[-1]

    # Generate SASS and PTX targets
    sass_targets = [f"{arch}-real" for arch in sorted_archs]
    ptx_target = f"{ptx_target_base}-virtual"

    final_archs = sass_targets + [ptx_target]

    return final_archs


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Determine and format CUDA architectures based on nvcc output and user request."
    )
    parser.add_argument(
        "requested_archs",
        help=(
            "Requested architectures: 'all', 'all-major', 'native', or a comma/space-separated "
            "list (e.g., '75 86 90a')."
        ),
    )
    parser.add_argument(
        "--nvcc-path",
        "-n",
        help=(
            "Path to the nvcc executable. Defaults to checking hint, PATH, "
            "then /usr/local/cuda/bin/nvcc."
        ),
    )
    parser.add_argument(
        "--min-arch",
        "-m",
        type=int,
        default=None,
        help=(
            "Minimum major CUDA architecture to consider (e.g., 70 for Volta+). "
            "Set to 0 or omit to disable."
        ),
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable verbose debug logging to stderr."
    )

    args = parser.parse_args()

    # Configure logging so verbose -> debug
    log_level = logging.DEBUG if args.verbose else logging.INFO
    log_format = "%(message)s" if log_level == logging.DEBUG else "%(levelname)s: %(message)s"
    logging.basicConfig(level=log_level, format=log_format, stream=sys.stderr)

    logging.debug(f"Requested CUDA architectures: {args.requested_archs}")

    # Return right away if requesting native
    req_lower = args.requested_archs.lower()
    if req_lower == "native":
        print("native", end="")
        sys.exit(0)

    # Get nvcc path
    nvcc_path: Optional[Path] = None
    if args.nvcc_path:
        nvcc_path = Path(args.nvcc_path).resolve()
        logging.debug(f"Using user-provided nvcc path: {nvcc_path}")
    else:
        logging.debug("No nvcc path provided, attempting automatic search...")
        try:
            nvcc_path = get_nvcc_path()
            logging.debug(f"Using nvcc at: {nvcc_path}")
        except FileNotFoundError:
            die(
                "Could not find 'nvcc' automatically. Please provide path via --nvcc-path "
                "or ensure it's in PATH or /usr/local/cuda/bin/nvcc."
            )

    # Get supported architectures
    nvcc_supported_archs = get_nvcc_archs(nvcc_path)
    min_filtered_archs = filter_archs_with_min_arch(nvcc_supported_archs, args.min_arch)
    platform_filtered_archs = filter_archs_for_platform(min_filtered_archs)

    # Filter based on requested architectures
    target_archs: list[str] = []
    if req_lower == "all":
        target_archs = platform_filtered_archs
        logging.debug(f"Using platform supported cuda architectures: {', '.join(target_archs)}")
    elif req_lower == "all-major":
        target_archs = filter_major_archs(platform_filtered_archs)
    else:
        user_archs = parse_requested_archs(args.requested_archs)
        target_archs = validate_user_archs(
            user_archs,
            nvcc_supported_archs,
            min_filtered_archs,
            platform_filtered_archs,
            args.min_arch,
        )

    # Error if no valid architectures
    if not target_archs:
        error_msg = (
            f"No valid CUDA architectures could be determined for request '{args.requested_archs}' "
            f"with current filters (min_arch={args.min_arch}, platform={platform.machine()})."
        )
        die(error_msg)

    # Generate final list with SASS/PTX suffixes
    final_archs_list = generate_sass_ptx_arch_list(target_archs)

    # Print final list
    final_archs_str = ";".join(final_archs_list)
    logging.debug(f"Selected CUDA architectures: {final_archs_str}")
    print(final_archs_str, end="")


if __name__ == "__main__":
    main()
