# Copyright 2014-present PlatformIO <contact@platformio.org>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# pylint: disable=redefined-outer-name

import functools
import re
import sys
import shutil
import subprocess
import importlib.util
from os.path import join, isfile
from pathlib import Path
from penv_setup import setup_python_environment
from littlefs import LittleFS
from fatfs import Partition, RamDisk, create_extended_partition
from platformio.project.helpers import get_project_dir

from SCons.Script import (
    ARGUMENTS, COMMAND_LINE_TARGETS, AlwaysBuild, Builder, Default,
    DefaultEnvironment)

# Initialize environment and configuration
env = DefaultEnvironment()
platform = env.PioPlatform()
config = env.GetProjectConfig()
board = env.BoardConfig()
filesystem = board.get("build.filesystem", "littlefs")
platformio_dir = config.get("platformio", "core_dir")
platform_dir = Path(platform.get_dir())

# Setup Python virtual environment and get executable paths
PYTHON_EXE, esptool_binary_path = setup_python_environment(env, platform, platformio_dir)

# Load SPIFFS generator from local module
spiffsgen_path = platform_dir / "builder" / "spiffsgen.py"
spec = importlib.util.spec_from_file_location("spiffsgen", str(spiffsgen_path))
spiffsgen = importlib.util.module_from_spec(spec)
sys.modules["spiffsgen"] = spiffsgen
spec.loader.exec_module(spiffsgen)
SpiffsFS = spiffsgen.SpiffsFS
SpiffsBuildConfig = spiffsgen.SpiffsBuildConfig

#
# Helpers
#

def BeforeUpload(target, source, env):
    upload_options = {}
    if "BOARD" in env:
        upload_options = env.BoardConfig().get("upload", {})

    if not env.subst("$UPLOAD_PORT"):
        env.AutodetectUploadPort()


def _get_board_f_flash(env):
    frequency = env.subst("$BOARD_F_FLASH")
    frequency = str(frequency).replace("L", "")
    return int(int(frequency) / 1000000)


def _parse_size(value):
    if isinstance(value, int):
        return value
    elif value.isdigit():
        return int(value)
    elif value.startswith("0x"):
        return int(value, 16)
    elif value[-1].upper() in ("K", "M"):
        base = 1024 if value[-1].upper() == "K" else 1024 * 1024
        return int(value[:-1]) * base
    return value


@functools.lru_cache(maxsize=None)
def _parse_ld_sizes(ldscript_path):
    assert ldscript_path
    result = {}
    # get flash size from board's manifest
    result['flash_size'] = int(env.BoardConfig().get("upload.maximum_size", 0))
    # get flash size from LD script path
    match = re.search(r"\.flash\.(\d+[mk]).*\.ld", ldscript_path)
    if match:
        result['flash_size'] = _parse_size(match.group(1))

    appsize_re = re.compile(
        r"irom0_0_seg\s*:.+len\s*=\s*(0x[\da-f]+)", flags=re.I)
    filesystem_re = re.compile(
        r"PROVIDE\s*\(\s*_%s_(\w+)\s*=\s*(0x[\da-f]+)\s*\)" % "FS"
        if "arduino" in env.subst("$PIOFRAMEWORK")
        else "SPIFFS",
        flags=re.I,
    )
    with open(ldscript_path) as fp:
        for line in fp.readlines():
            line = line.strip()
            if not line or line.startswith("/*"):
                continue
            match = appsize_re.search(line)
            if match:
                result['app_size'] = _parse_size(match.group(1))
                continue
            match = filesystem_re.search(line)
            if match:
                result['fs_%s' % match.group(1)] = _parse_size(
                    match.group(2))
    return result


def _get_flash_size(env):
    ldsizes = _parse_ld_sizes(env.GetActualLDScript())
    if ldsizes['flash_size'] < 1048576:
        return "%dK" % (ldsizes['flash_size'] / 1024)
    return "%dM" % (ldsizes['flash_size'] / 1048576)


def fetch_fs_size(env):
    ldsizes = _parse_ld_sizes(env.GetActualLDScript())
    for key in ldsizes:
        if key.startswith("fs_"):
            env[key.upper()] = ldsizes[key]

    assert all([
        k in env
        for k in ["FS_START", "FS_END", "FS_PAGE", "FS_BLOCK"]
    ])

    # esptool flash starts from 0
    for k in ("FS_START", "FS_END"):
        _value = 0
        if env[k] < 0x40300000:
            _value = env[k] & 0xFFFFF
        elif env[k] < 0x411FB000:
            _value = env[k] & 0xFFFFFF
            _value -= 0x200000  # correction
        else:
            _value = env[k] & 0xFFFFFF
            _value += 0xE00000  # correction

        env[k] = _value
    
    # Calculate FS_SIZE for filesystem builders
    env["FS_SIZE"] = env["FS_END"] - env["FS_START"]


def build_fs_image(target, source, env):
    """
    Build LittleFS filesystem image using littlefs-python.

    Args:
        target: SCons target (output .bin file)
        source: SCons source (directory with files)
        env: SCons environment object

    Returns:
        int: 0 on success, 1 on failure
    """
    # Get parameters
    source_dir = str(source[0])
    target_file = str(target[0])
    fs_size = env["FS_SIZE"]
    block_size = env.get("FS_BLOCK", 8192)
    
    # Safety check: if block_size is 0 or invalid, use default
    if not block_size or block_size == 0:
        print("Warning: FS_BLOCK not found in linker script, using default 8192")
        block_size = 8192

    # Calculate block count
    block_count = fs_size // block_size

    # Get disk version from board config or project options
    # ESP8266 Tasmota Arduino framework uses LittleFS v2.0
    disk_version_str = "2.0"
    
    for section in ["common", "env:" + env["PIOENV"]]:
        if config.has_option(section, "board_build.littlefs_version"):
            disk_version_str = config.get(section, "board_build.littlefs_version")
            break
    
    try:
        version_parts = str(disk_version_str).split(".")
        major = int(version_parts[0])
        minor = int(version_parts[1]) if len(version_parts) > 1 else 0
        disk_version = (major << 16) | minor
    except (ValueError, IndexError):
        print(f"Warning: Invalid littlefs version '{disk_version_str}', using default 2.0")
        disk_version = (2 << 16) | 0

    # ESP8266 Arduino framework uses: read_size=64, prog_size=64, cache_size=64, lookahead_size=64, block_cycles=16
    # ESP8266 Arduino compiles with LFS_NAME_MAX=32
    # name_max=32 -> LFS_NAME_MAX=32
    read_size = 64
    prog_size = 64
    cache_size = 64
    lookahead_size = 64
    name_max = 32
    block_cycles = 16

    try:
        fs = LittleFS(
            block_size=block_size,
            block_count=block_count,
            read_size=read_size,
            prog_size=prog_size,
            cache_size=cache_size,
            lookahead_size=lookahead_size,
            block_cycles=block_cycles,
            name_max=name_max,
            disk_version=disk_version,
            mount=True
        )

        source_path = Path(source_dir)
        if source_path.exists():
            for item in source_path.rglob("*"):
                rel_path = item.relative_to(source_path)
                fs_path = rel_path.as_posix()
                
                if item.is_dir():
                    fs.makedirs(fs_path, exist_ok=True)
                    try:
                        mtime = int(item.stat().st_mtime)
                        fs.setattr(fs_path, 't', mtime.to_bytes(4, 'little'))
                    except Exception:
                        pass
                else:
                    if rel_path.parent != Path("."):
                        fs.makedirs(rel_path.parent.as_posix(), exist_ok=True)
                    with fs.open(fs_path, "wb") as dest:
                        dest.write(item.read_bytes())
                    try:
                        mtime = int(item.stat().st_mtime)
                        fs.setattr(fs_path, 't', mtime.to_bytes(4, 'little'))
                    except Exception:
                        pass

        with open(target_file, "wb") as f:
            f.write(fs.context.buffer)

        return 0

    except Exception as e:
        print(f"Error building LittleFS image: {e}")
        return 1


def build_spiffs_image(target, source, env):
    """Build SPIFFS filesystem image using spiffsgen.py."""
    source_dir = str(source[0])
    target_file = str(target[0])
    fs_size = env["FS_SIZE"]
    page_size = env.get("FS_PAGE", 256)
    block_size = env.get("FS_BLOCK", 4096)

    obj_name_len = 32
    meta_len = 4
    use_magic = True
    use_magic_len = True
    aligned_obj_ix_tables = False

    try:
        spiffs_build_config = SpiffsBuildConfig(
            page_size=page_size,
            page_ix_len=2,
            block_size=block_size,
            block_ix_len=2,
            meta_len=meta_len,
            obj_name_len=obj_name_len,
            obj_id_len=2,
            span_ix_len=2,
            packed=True,
            aligned=True,
            endianness='little',
            use_magic=use_magic,
            use_magic_len=use_magic_len,
            aligned_obj_ix_tables=aligned_obj_ix_tables
        )

        spiffs = SpiffsFS(fs_size, spiffs_build_config)

        source_path = Path(source_dir)
        if source_path.exists():
            for item in source_path.rglob("*"):
                if item.is_file():
                    rel_path = item.relative_to(source_path)
                    img_path = "/" + rel_path.as_posix()
                    spiffs.create_file(img_path, str(item))

        image = spiffs.to_binary()

        with open(target_file, "wb") as f:
            f.write(image)

        print(f"\nSuccessfully created SPIFFS image: {target_file}")
        return 0

    except Exception as e:
        print(f"Error building SPIFFS image: {e}")
        return 1


def build_fatfs_image(target, source, env):
    """Build FatFS filesystem image with ESP32 Wear Leveling support."""
    source_dir = str(source[0])
    target_file = str(target[0])
    fs_size = env["FS_SIZE"]
    sector_size = env.get("FS_SECTOR", 4096)

    # Calculate wear-leveling overhead
    # ESP32 WL uses ~2 sectors for state + additional overhead
    wl_sectors_overhead = 2 + (fs_size // sector_size) // 16
    fat_sectors = (fs_size // sector_size) - wl_sectors_overhead
    fat_size = fat_sectors * sector_size
    
    wl_info = {
        'fat_size': fat_size,
        'fat_sectors': fat_sectors
    }

    fat_fs_size = wl_info['fat_size']
    sector_count = wl_info['fat_sectors']

    try:
        storage = bytearray(fat_fs_size)
        disk = RamDisk(storage, sector_size=sector_size, sector_count=sector_count)
        base_partition = Partition(disk)

        from fatfs.wrapper import pyf_mkfs
        FR_OK = 0  # PY_FR_OK constant
        workarea_size = sector_size * 2
        
        ret = pyf_mkfs(
            base_partition.pname, 
            n_fat=2, 
            align=0,
            n_root=512,
            au_size=0,
            workarea_size=workarea_size
        )
        if ret != FR_OK:
            raise Exception(f"Failed to format filesystem: error code {ret}")

        base_partition.mount()

        from fatfs.partition_extended import PartitionExtended
        partition = PartitionExtended(base_partition)

        skipped_files = []

        source_path = Path(source_dir)
        if source_path.exists():
            for item in source_path.rglob("*"):
                rel_path = item.relative_to(source_path)
                fs_path = "/" + rel_path.as_posix()

                if item.is_dir():
                    try:
                        partition.mkdir(fs_path)
                    except Exception:
                        pass
                else:
                    if rel_path.parent != Path("."):
                        parent_path = "/" + rel_path.parent.as_posix()
                        try:
                            partition.mkdir(parent_path)
                        except Exception:
                            pass

                    try:
                        with partition.open(fs_path, "w") as dest:
                            dest.write(item.read_bytes())
                    except Exception as e:
                        print(f"Warning: Failed to write file {rel_path}: {e}")
                        skipped_files.append(str(rel_path))

        base_partition.unmount()
        
        from fatfs import create_esp32_wl_image
        wl_image = create_esp32_wl_image(bytes(storage), fs_size, sector_size)

        with open(target_file, "wb") as f:
            f.write(wl_image)

        if skipped_files:
            print(f"\nWarning: {len(skipped_files)} file(s) skipped")
        
        print(f"\nSuccessfully created FAT image: {target_file}")

        return 0

    except Exception as e:
        print(f"Error building FatFS image: {e}")
        return 1


def build_fs_router(target, source, env):
    """Route to appropriate filesystem builder based on filesystem type."""
    fs_type = board.get("build.filesystem", "littlefs")
    if fs_type == "littlefs":
        return build_fs_image(target, source, env)
    elif fs_type == "fatfs":
        return build_fatfs_image(target, source, env)
    elif fs_type == "spiffs":
        return build_spiffs_image(target, source, env)
    else:
        print(f"Error: Unknown filesystem type '{fs_type}'. Supported types: littlefs, fatfs, spiffs")
        return 1


def __fetch_fs_size(target, source, env):
    fetch_fs_size(env)
    return (target, source)


def _update_max_upload_size(env):
    ldsizes = _parse_ld_sizes(env.GetActualLDScript())
    if ldsizes and "app_size" in ldsizes:
        env.BoardConfig().update("upload.maximum_size", ldsizes['app_size'])


def get_esptoolpy_reset_flags(resetmethod):
    # no dtr, no_sync
    resets = ("no-reset-no-sync", "soft-reset")
    if resetmethod == "nodemcu":
        # dtr
        resets = ("default-reset", "hard-reset")
    elif resetmethod == "ck":
        # no dtr
        resets = ("no-reset", "soft-reset")

    return ["--before", resets[0], "--after", resets[1]]


########################################################

# Take care of possible whitespaces in path
uploader_path = (
    f'"{esptool_binary_path}"' 
    if ' ' in esptool_binary_path 
    else esptool_binary_path
)

env.Replace(
    __get_flash_size=_get_flash_size,
    __get_board_f_flash=_get_board_f_flash,

    AR="xtensa-lx106-elf-gcc-ar",
    AS="xtensa-lx106-elf-as",
    CC="xtensa-lx106-elf-gcc",
    CXX="xtensa-lx106-elf-g++",
    GDB="xtensa-lx106-elf-gdb",
    OBJCOPY="xtensa-lx106-elf-objcopy",
    RANLIB="xtensa-lx106-elf-gcc-ranlib",
    SIZETOOL="xtensa-lx106-elf-size",

    ARFLAGS=["rc"],

    #
    # Filesystem
    #

    ESP8266_FS_IMAGE_NAME=env.get("ESP8266_FS_IMAGE_NAME", env.get(
        "SPIFFSNAME", filesystem)),

    #
    # Misc
    #

    SIZEPROGREGEXP=r"^(?:\.irom0\.text|\.text|\.text1|\.data|\.rodata|)\s+([0-9]+).*",
    SIZEDATAREGEXP=r"^(?:\.data|\.rodata|\.bss)\s+([0-9]+).*",
    SIZECHECKCMD="$SIZETOOL -A -d $SOURCES",
    SIZEPRINTCMD='$SIZETOOL -B -d $SOURCES',
    ERASEFLAGS=["--chip", "esp8266", "--port", '"$UPLOAD_PORT"'],
    ERASETOOL=uploader_path,
    ERASECMD='$ERASETOOL $ERASEFLAGS erase-flash',
    PROGSUFFIX=".elf"
)

# Check if lib_archive is set in platformio.ini and set it to False
# if not found. This makes weak defs in framework and libs possible.
def check_lib_archive_exists():
    for section in config.sections():
        if "lib_archive" in config.options(section):
            return True
    return False

if not check_lib_archive_exists():
    env_section = "env:" + env["PIOENV"]
    config.set(env_section, "lib_archive", "False")

# Allow user to override via pre:script
if env.get("PROGNAME", "program") == "program":
    env.Replace(PROGNAME="firmware")

#
# Keep support for old LD Scripts
#

env.Replace(BUILD_FLAGS=[
    f.replace("esp8266.flash", "eagle.flash") if "esp8266.flash" in f else f
    for f in env.get("BUILD_FLAGS", [])
])

env.Append(
    BUILDERS=dict(
        DataToBin=Builder(
            action=env.VerboseAction(
                build_fs_router,
                "Building FS image from '$SOURCES' directory to $TARGET",
            ),
            emitter=__fetch_fs_size,
            source_factory=env.Dir,
            suffix=".bin"
        )
    )
)


#
# Target: Build executable and linkable firmware or file system image
#

target_elf = None
if "nobuild" in COMMAND_LINE_TARGETS:
    target_elf = join("$BUILD_DIR", "${PROGNAME}.elf")
    if set(["uploadfs", "uploadfsota"]) & set(COMMAND_LINE_TARGETS):
        fetch_fs_size(env)
        target_firm = join("$BUILD_DIR", "${ESP8266_FS_IMAGE_NAME}.bin")
    else:
        target_firm = join("$BUILD_DIR", "${PROGNAME}.bin")
else:
    target_elf = env.BuildProgram()
    if set(["buildfs", "uploadfs", "uploadfsota"]) & set(COMMAND_LINE_TARGETS):
        if filesystem not in ("littlefs", "spiffs", "fatfs"):
            sys.stderr.write("Filesystem %s is not supported!\n" % filesystem)
            env.Exit(1)
        target_firm = env.DataToBin(
            join("$BUILD_DIR", "${ESP8266_FS_IMAGE_NAME}"), "$PROJECT_DATA_DIR")
        env.NoCache(target_firm)
        AlwaysBuild(target_firm)
    else:
        target_firm = env.ElfToBin(
            join("$BUILD_DIR", "${PROGNAME}"), target_elf)
        env.Depends(target_firm, "checkprogsize")

env.AddPlatformTarget("buildfs", target_firm, target_firm, "Build Filesystem Image")
AlwaysBuild(env.Alias("nobuild", target_firm))
target_buildprog = env.Alias("buildprog", target_firm, target_firm)

# update max upload size based on CSV file
if env.get("PIOMAINPROG"):
    env.AddPreAction(
        "checkprogsize",
        env.VerboseAction(
            lambda source, target, env: _update_max_upload_size(env),
            "Retrieving maximum program size $SOURCE"))


#
# Filesystem Download Functions
#

def _get_unpack_dir(env):
    """Get the unpack directory from project configuration."""
    unpack_dir = "unpacked_fs"
    for section in ["common", "env:" + env["PIOENV"]]:
        if config.has_option(section, "board_build.unpack_dir"):
            unpack_dir = config.get(section, "board_build.unpack_dir")
            break
    return unpack_dir


def _prepare_unpack_dir(unpack_dir):
    """Prepare the unpack directory by removing old content and creating fresh directory."""
    unpack_path = Path(get_project_dir()) / unpack_dir
    if unpack_path.exists():
        shutil.rmtree(unpack_path)
    unpack_path.mkdir(parents=True, exist_ok=True)
    return unpack_path


def _download_fs_image(env):
    """Download filesystem image from ESP8266 device."""    
    # Ensure upload port is set
    if not env.subst("$UPLOAD_PORT"):
        env.AutodetectUploadPort()

    upload_port = env.subst("$UPLOAD_PORT")
    download_speed = board.get("download.speed", "115200")

    # Get FS parameters from LD script
    fetch_fs_size(env)
    fs_start = env["FS_START"]
    fs_size = env["FS_SIZE"]

    print(f"\nDownloading filesystem from {upload_port}...")
    print(f"  Start: {hex(fs_start)}")
    print(f"  Size: {hex(fs_size)} ({fs_size} bytes)")

    # Download filesystem image
    build_dir = Path(get_project_dir()) / ".pio" / "build" / env["PIOENV"]
    build_dir.mkdir(parents=True, exist_ok=True)
    fs_file = build_dir / f"downloaded_fs_{hex(fs_start)}_{hex(fs_size)}.bin"

    esptool_cmd = [
        uploader_path.strip('"'),
        "--port", upload_port,
        "--baud", str(download_speed),
        "--before", "default-reset",
        "--after", "hard-reset",
        "read-flash",
        hex(fs_start),
        hex(fs_size),
        str(fs_file)
    ]

    try:
        result = subprocess.run(esptool_cmd, check=False)
        if result.returncode != 0:
            print(f"Error: Download failed with code {result.returncode}")
            return None, None, None
    except Exception as e:
        print(f"Error: {e}")
        return None, None, None

    print(f"\nDownloaded to {fs_file}")
    return fs_file, fs_start, fs_size


def _extract_littlefs(fs_file, fs_size, unpack_path, unpack_dir):
    """Extract LittleFS filesystem."""
    # Read the downloaded filesystem image
    with open(fs_file, 'rb') as f:
        fs_data = f.read()

    # Try common ESP8266/ESP32 LittleFS configurations
    configs = [
        # ESP8266 Tasmota default (most common)
        {'block_size': 8192, 'block_count': fs_size // 8192, 'read_size': 256, 'prog_size': 256},
        # ESP8266 alternative configurations
        {'block_size': 4096, 'block_count': fs_size // 4096, 'read_size': 256, 'prog_size': 256},
        # ESP-IDF defaults
        {'block_size': 4096, 'block_count': fs_size // 4096, 'read_size': 1, 'prog_size': 1},
        # Alternative configurations
        {'block_size': 4096, 'block_count': fs_size // 4096, 'read_size': 128, 'prog_size': 128},
    ]

    print("\nAttempting to mount LittleFS with different configurations...")
    
    fs = None
    for i, cfg in enumerate(configs):
        try:
            print(f"  Try {i+1}: block_size={cfg['block_size']}, read_size={cfg['read_size']}, prog_size={cfg['prog_size']}")
            
            fs = LittleFS(
                block_size=cfg['block_size'],
                block_count=cfg['block_count'],
                read_size=cfg['read_size'],
                prog_size=cfg['prog_size'],
                cache_size=cfg['block_size'],
                lookahead_size=32,
                block_cycles=500,
                name_max=32,
                mount=False
            )
            fs.context.buffer = bytearray(fs_data)
            fs.mount()
            print(f"  Successfully mounted with configuration {i+1}")
            break
        except Exception as e:
            print(f"  Failed: {e}")
            fs = None
            continue
    
    if fs is None:
        print("\nError: Could not mount LittleFS with any known configuration.")
        print("The filesystem may be:")
        print("  - Empty or unformatted")
        print("  - Corrupted")
        print("  - Using a non-standard configuration")
        return 1

    # Extract all files
    file_count = 0
    print("\nExtracted files:")
    try:
        for root, dirs, files in fs.walk("/"):
            if not root.endswith("/"):
                root += "/"

            # Create directories
            for dir_name in dirs:
                src_path = root + dir_name
                dst_path = unpack_path / src_path[1:]
                dst_path.mkdir(parents=True, exist_ok=True)
                print(f"  [DIR]  {src_path}")

            # Extract files
            for file_name in files:
                src_path = root + file_name
                dst_path = unpack_path / src_path[1:]
                dst_path.parent.mkdir(parents=True, exist_ok=True)

                with fs.open(src_path, "rb") as src:
                    file_data = src.read()
                    dst_path.write_bytes(file_data)

                print(f"  [FILE] {src_path} ({len(file_data)} bytes)")
                file_count += 1

        fs.unmount()
        
        if file_count == 0:
            print("\nNo files were extracted.")
            print("The filesystem may be empty or freshly formatted.")
        else:
            print(f"\nSuccessfully extracted {file_count} file(s) to {unpack_dir}")
        
        return 0
    except Exception as e:
        print(f"\nError during extraction: {e}")
        try:
            fs.unmount()
        except Exception:
            pass
        return 1


def _parse_spiffs_config(fs_data, fs_size):
    """
    Auto-detect SPIFFS configuration from the image.
    Tries common configurations and validates against the image.
    
    Returns:
        dict: SPIFFS configuration parameters or None
    """
    # Common ESP32/ESP8266 SPIFFS configurations (ordered by likelihood for Tasmota/ESP8266)
    common_configs = [
        # ESP8266 Tasmota defaults
        {'page_size': 256, 'block_size': 8192, 'obj_name_len': 32},
        # ESP32/ESP8266 standard defaults
        {'page_size': 256, 'block_size': 4096, 'obj_name_len': 32},
        # Alternative configurations
        {'page_size': 512, 'block_size': 4096, 'obj_name_len': 32},
        {'page_size': 256, 'block_size': 4096, 'obj_name_len': 64},
        {'page_size': 512, 'block_size': 8192, 'obj_name_len': 32},
    ]
    
    print("\nAuto-detecting SPIFFS configuration...")
    
    for i, cfg in enumerate(common_configs, 1):
        try:
            print(f"  Try {i}: page_size={cfg['page_size']}, block_size={cfg['block_size']}, obj_name_len={cfg['obj_name_len']}")
            
            # Try to parse with this configuration
            spiffs_build_config = SpiffsBuildConfig(
                page_size=cfg['page_size'],
                page_ix_len=2,
                block_size=cfg['block_size'],
                block_ix_len=2,
                meta_len=4,
                obj_name_len=cfg['obj_name_len'],
                obj_id_len=2,
                span_ix_len=2,
                packed=True,
                aligned=True,
                endianness='little',
                use_magic=True,
                use_magic_len=True,
                aligned_obj_ix_tables=False
            )
            
            # Try to create and parse the filesystem
            spiffs = SpiffsFS(fs_size, spiffs_build_config)
            spiffs.from_binary(fs_data)
            
            # If we got here without exception, this config works
            print(f"  Successfully detected SPIFFS configuration {i}")
            
            return {
                'page_size': cfg['page_size'],
                'block_size': cfg['block_size'],
                'obj_name_len': cfg['obj_name_len'],
                'meta_len': 4,
                'use_magic': True,
                'use_magic_len': True,
                'aligned_obj_ix_tables': False
            }
        except Exception as e:
            print(f"  Failed: {e}")
            continue
    
    # If no config worked, return defaults
    print("  Could not auto-detect configuration, using ESP8266 defaults")
    return {
        'page_size': 256,
        'block_size': 8192,
        'obj_name_len': 32,
        'meta_len': 4,
        'use_magic': True,
        'use_magic_len': True,
        'aligned_obj_ix_tables': False
    }


def _extract_spiffs(fs_file, fs_size, unpack_path, unpack_dir):
    """Extract SPIFFS filesystem with auto-detected configuration."""
    # Read the downloaded filesystem image
    with open(fs_file, 'rb') as f:
        fs_data = f.read()

    # Auto-detect SPIFFS configuration
    spiffs_config = _parse_spiffs_config(fs_data, fs_size)
    
    # Create SPIFFS build configuration
    spiffs_build_config = SpiffsBuildConfig(
        page_size=spiffs_config['page_size'],
        page_ix_len=2,
        block_size=spiffs_config['block_size'],
        block_ix_len=2,
        meta_len=spiffs_config['meta_len'],
        obj_name_len=spiffs_config['obj_name_len'],
        obj_id_len=2,
        span_ix_len=2,
        packed=True,
        aligned=True,
        endianness='little',
        use_magic=spiffs_config['use_magic'],
        use_magic_len=spiffs_config['use_magic_len'],
        aligned_obj_ix_tables=spiffs_config['aligned_obj_ix_tables']
    )

    # Create SPIFFS filesystem and parse the image
    spiffs = SpiffsFS(fs_size, spiffs_build_config)
    spiffs.from_binary(fs_data)

    # Extract files
    file_count = spiffs.extract_files(str(unpack_path))

    if file_count == 0:
        print("\nNo files were extracted.")
        print("The filesystem may be empty, freshly formatted, or contain only deleted entries.")
    else:
        print(f"\nSuccessfully extracted {file_count} file(s) to {unpack_dir}")

    return 0


def _extract_fatfs(fs_file, unpack_path, unpack_dir):
    """Extract FatFS filesystem."""
    with open(fs_file, 'rb') as f:
        fs_data = bytearray(f.read())

    if len(fs_data) < 512:
        print("Error: Downloaded image is too small to be a valid FAT filesystem")
        return 1

    from fatfs import is_esp32_wl_image, extract_fat_from_esp32_wl
    
    # Try common sector sizes for ESP8266/ESP32
    sector_sizes_to_try = [4096, 512, 1024, 2048]
    
    print("\nAttempting to detect FAT filesystem configuration...")
    
    wl_detected = False
    for sector_size in sector_sizes_to_try:
        if is_esp32_wl_image(fs_data, sector_size):
            print(f"  Detected Wear Leveling layer with sector_size={sector_size}")
            fat_data = extract_fat_from_esp32_wl(fs_data, sector_size)
            if fat_data is None:
                print("  Failed to extract FAT data from wear-leveling image")
                continue
            fs_data = bytearray(fat_data)
            print(f"  Extracted FAT data: {len(fs_data)} bytes")
            wl_detected = True
            break
    
    if not wl_detected:
        print("  No Wear Leveling layer detected, treating as raw FAT image...")

    # Read sector size from FAT boot sector
    sector_size = int.from_bytes(fs_data[0x0B:0x0D], byteorder='little')

    if sector_size not in [512, 1024, 2048, 4096]:
        print(f"Error: Invalid sector size {sector_size}. Must be 512, 1024, 2048, or 4096")
        print("The filesystem may be corrupted or not a valid FAT filesystem")
        return 1

    print(f"  Detected sector size: {sector_size} bytes")

    fs_size_adjusted = len(fs_data)
    sector_count = fs_size_adjusted // sector_size
    
    try:
        disk = RamDisk(fs_data, sector_size=sector_size, sector_count=sector_count)
        partition = create_extended_partition(disk)
        partition.mount()
    except Exception as e:
        print(f"Error: Failed to mount FAT filesystem: {e}")
        return 1

    print("\nExtracting files:\n")
    extracted_count = 0
    for root, _dirs, files in partition.walk("/"):
        rel_root = root[1:] if root.startswith("/") else root
        abs_root = unpack_path / rel_root
        abs_root.mkdir(parents=True, exist_ok=True)
        for filename in files:
            src_file = root.rstrip("/") + "/" + filename if root != "/" else "/" + filename
            dst_file = abs_root / filename
            try:
                data = partition.read_file(src_file)
                dst_file.write_bytes(data)
                print(f"  [FILE] {src_file} ({len(data)} bytes)")
                extracted_count += 1
            except Exception as e:
                print(f"  Warning: Failed to extract {src_file}: {e}")
    partition.unmount()
    
    if extracted_count == 0:
        print("\nNo files were extracted.")
        print("The filesystem may be empty, freshly formatted, or contain only deleted entries.")
    else:
        print(f"\nSuccessfully extracted {extracted_count} file(s) to {unpack_dir}")
    
    return 0


def download_fs_action(target, source, env):
    """Download and extract filesystem from device."""
    # Get unpack directory (use global env, not the parameter)
    unpack_dir = _get_unpack_dir(env)
    
    # Download filesystem image
    fs_file, _fs_start, fs_size = _download_fs_image(env)
    
    if fs_file is None:
        return 1
    
    # Detect filesystem type
    with open(fs_file, 'rb') as f:
        header = f.read(16384)  # Read more to check for offset FAT
    
    # Check for filesystem signatures
    fs_type = None
    
    # 1. Check for LittleFS magic at offset 8 of the superblock
    if len(header) >= 16 and header[8:16] == b'littlefs':
        fs_type = "littlefs"
    
    # 2. Check for FAT filesystem (with or without Wear Leveling)
    if fs_type is None:
        # Check multiple possible offsets for FAT boot sector
        # ESP8266 with WL often has FAT at offset 0x1000 (4096)
        fat_offsets = [0, 4096, 8192]
        
        for offset in fat_offsets:
            if len(header) >= offset + 512:
                boot_sector = header[offset:offset+512]
                
                # Check for FAT boot signature at offset 510-511
                if boot_sector[510:512] == b'\x55\xAA':
                    # Additional validation: check for FAT filesystem markers
                    # Check for "FAT" string or "MSDOS" in boot sector
                    if (b'FAT' in boot_sector[0:90] or 
                        b'MSDOS' in boot_sector[0:90] or
                        b'MSWIN' in boot_sector[0:90]):
                        # Verify bytes per sector
                        bytes_per_sector = int.from_bytes(boot_sector[11:13], byteorder='little')
                        if bytes_per_sector in [512, 1024, 2048, 4096]:
                            fs_type = "fatfs"
                            print(f"  FAT boot sector found at offset 0x{offset:x}")
                            break
    
    # 3. Default to SPIFFS for ESP8266
    if fs_type is None:
        fs_type = "spiffs"
    
    print(f"\nDetected filesystem: {fs_type.upper()}")
    
    # Prepare unpack directory
    unpack_path = _prepare_unpack_dir(unpack_dir)
    
    # Extract filesystem
    try:
        if fs_type == "littlefs":
            return _extract_littlefs(fs_file, fs_size, unpack_path, unpack_dir)
        elif fs_type == "fatfs":
            return _extract_fatfs(fs_file, unpack_path, unpack_dir)
        else:
            return _extract_spiffs(fs_file, fs_size, unpack_path, unpack_dir)
    except Exception as e:
        print(f"Error: {e}")
        return 1


#
# Target: Print binary size
#

target_size = env.AddPlatformTarget(
    "size",
    target_elf,
    env.VerboseAction("$SIZEPRINTCMD", "Calculating size $SOURCE"),
    "Program Size",
    "Calculate program size",
)


#
# Target: Upload firmware or filesystem image
#

upload_protocol = env.subst("$UPLOAD_PROTOCOL") or "esptool"
upload_actions = []

# Compatibility with old OTA configurations
if (upload_protocol != "espota"
        and re.match(r"\"?((([0-9]{1,3}\.){3}[0-9]{1,3})|[^\\/]+\.local)\"?$",
                     env.get("UPLOAD_PORT", ""))):
    upload_protocol = "espota"
    sys.stderr.write(
        "Warning! We have just detected `upload_port` as IP address or host "
        "name of ESP device. `upload_protocol` is switched to `espota`.\n"
        "Please specify `upload_protocol = espota` in `platformio.ini` "
        "project configuration file.\n")

if upload_protocol == "espota":
    if not env.subst("$UPLOAD_PORT"):
        sys.stderr.write(
            "Error: Please specify IP address or host name of ESP device "
            "using `upload_port` for build environment or use "
            "global `--upload-port` option.\n"
            "See https://docs.platformio.org/page/platforms/"
            "espressif8266.html#over-the-air-ota-update\n")
    env.Replace(
        UPLOADER=join(
            platform.get_package_dir("framework-arduinoespressif8266") or "",
            "tools", "espota.py"),
        UPLOADERFLAGS=["--debug", "--progress", "-i", "$UPLOAD_PORT"],
        UPLOADCMD='"$PYTHONEXE" "$UPLOADER" $UPLOADERFLAGS -f $SOURCE'
    )
    if set(["uploadfs", "uploadfsota"]) & set(COMMAND_LINE_TARGETS):
        env.Append(UPLOADERFLAGS=["-s"])
    upload_actions = [env.VerboseAction("$UPLOADCMD", "Uploading $SOURCE")]

elif upload_protocol == "esptool":
    env.Replace(
        UPLOADER=uploader_path,
        UPLOADERFLAGS=[
            "--chip", "esp8266",
            "--port", '"$UPLOAD_PORT"',
            "--baud", "$UPLOAD_SPEED",
            "write-flash"
        ],
        UPLOADCMD='$UPLOADER $UPLOADERFLAGS 0x0 $SOURCE'
    )
    for image in env.get("FLASH_EXTRA_IMAGES", []):
        env.Append(UPLOADERFLAGS=[image[0], env.subst(image[1])])

    if "uploadfs" in COMMAND_LINE_TARGETS:
        env.Replace(
            UPLOADERFLAGS=[
                "--chip", "esp8266",
                "--port", '"$UPLOAD_PORT"',
                "--baud", "$UPLOAD_SPEED",
                "write-flash",
                "$FS_START"
            ],
            UPLOADCMD='$UPLOADER $UPLOADERFLAGS $SOURCE',
        )

    env.Prepend(
        UPLOADERFLAGS=get_esptoolpy_reset_flags(env.subst("$UPLOAD_RESETMETHOD"))
    )

    upload_actions = [
        env.VerboseAction(BeforeUpload, "Looking for upload port..."),
        env.VerboseAction("$UPLOADCMD", "Uploading $SOURCE")
    ]

# custom upload tool
elif upload_protocol == "custom":
    upload_actions = [env.VerboseAction("$UPLOADCMD", "Uploading $SOURCE")]

else:
    sys.stderr.write("Warning! Unknown upload protocol %s\n" % upload_protocol)

env.AddPlatformTarget("upload", target_firm, upload_actions, "Upload")
env.AddPlatformTarget("uploadfs", target_firm, upload_actions, "Upload Filesystem Image")
env.AddPlatformTarget(
    "uploadfsota", target_firm, upload_actions, "Upload Filesystem Image OTA")


# Target: Download Filesystem (auto-detect type)
env.AddPlatformTarget(
    "download_fs",
    None,
    [
        env.VerboseAction(BeforeUpload, "Looking for upload port..."),
        env.VerboseAction(download_fs_action, "Downloading and extracting filesystem")
    ],
    "Download and extract filesystem from device",
)

#
# Target: Erase Flash and Upload
#

env.AddPlatformTarget(
    "erase_upload",
    target_firm,
    [
        env.VerboseAction(BeforeUpload, "Looking for upload port..."),
        env.VerboseAction("$ERASECMD", "Erasing..."),
        env.VerboseAction("$UPLOADCMD", "Uploading $SOURCE")
    ],
    "Erase Flash and Upload",
)

#
# Target: Erase Flash
#

env.AddPlatformTarget(
    "erase",
    None,
    [
        env.VerboseAction(BeforeUpload, "Looking for upload port..."),
        env.VerboseAction("$ERASECMD", "Erasing...")
    ],
    "Erase Flash",
)


#
# Default targets
#

Default([target_buildprog, target_size])
