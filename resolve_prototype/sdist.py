import json
import logging
import os
import shutil
from pathlib import Path
from subprocess import CalledProcessError, run
from tempfile import TemporaryDirectory

import aiofiles
from build import ProjectBuilder
from httpx import AsyncClient
from importlib_metadata import Distribution
from pypi_types import pypi_releases, pypi_metadata

from resolve_prototype.common import user_agent, Cache

logger = logging.getLogger(__name__)


class ProjectHooksCaptureOutput:
    """Boilerplate to get stdout and stderr for the project-hooks subprocesses"""

    stdout = ""
    stderr = ""

    def subprocess_runner(self, cmd, cwd=None, extra_environ=None):
        """Modified from pyproject_hooks.default_subprocess_runner"""
        env = os.environ.copy()
        if extra_environ:
            env.update(extra_environ)

        result = run(cmd, cwd=cwd, env=env, text=True, capture_output=True)
        self.stdout += result.stdout
        self.stderr += result.stderr
        result.check_returncode()


async def to_thread(func, /, *args, **kwargs):
    """Backport from python 3.9
    https://github.com/python/cpython/blob/f4c03484da59049eb62a9bf7777b963e2267d187/Lib/asyncio/threads.py

    Asynchronously run function *func* in a separate thread.
    Any *args and **kwargs supplied for this function are directly passed
    to *func*. Also, the current :class:`contextvars.Context` is propagated,
    allowing context variables from the main thread to be accessed in the
    separate thread.
    Return a coroutine that can be awaited to get the eventual result of *func*.
    """

    import functools
    import contextvars

    from asyncio import events

    loop = events.get_running_loop()
    ctx = contextvars.copy_context()
    func_call = functools.partial(ctx.run, func, *args, **kwargs)
    return await loop.run_in_executor(None, func_call)


async def build_sdist(
    client: AsyncClient, file: pypi_releases.File, cache: Cache
) -> pypi_metadata.Metadata:
    # TODO(konstin): Better cache key, outside of pypi this will cause cache collision
    if metadata := cache.get("sdist_build_metadata", file.filename + ".METADATA.json"):
        logger.info(f"Using cached json metadata for {file.filename}")
    else:
        with TemporaryDirectory() as tempdir:
            json_metadata = await build_sdist_impl(client, file, tempdir)
        cache.set(
            "sdist_build_metadata",
            file.filename + ".METADATA.json",
            json.dumps(json_metadata),
        )
        metadata = json.dumps(json_metadata)

    try:
        metadata = pypi_metadata.parse_metadata(metadata)
    except Exception as e:
        raise RuntimeError(
            f"Failed to parse sdist built metadata for {file.filename}, "
            f"this is most likely a bug"
        ) from e
    logger.info(f"sdist {file.filename} {metadata.requires_dist}")
    return metadata


async def build_sdist_impl(client: AsyncClient, file: pypi_releases.File, tempdir: str):
    logger.info(f"Downloading {file.filename}")
    downloaded_file = Path(tempdir).joinpath(file.filename)
    async with aiofiles.open(downloaded_file, mode="wb") as f, client.stream(
        "GET", file.url, headers={"user-agent": user_agent}
    ) as response:
        async for chunk in response.aiter_bytes():
            await f.write(chunk)
    logger.info(f"Extracting {file.filename}")
    extracted = Path(tempdir).joinpath("extracted")
    await to_thread(shutil.unpack_archive, downloaded_file, extracted)
    try:
        [src_dir] = list(extracted.iterdir())
    except ValueError:
        logger.error(str(list(extracted.iterdir())))
        raise
    logger.info(f"Building {file.filename}")
    metadata_dir = Path(tempdir).joinpath("metadata")
    capture = ProjectHooksCaptureOutput()
    try:
        await to_thread(
            lambda: ProjectBuilder(
                src_dir, runner=capture.subprocess_runner
            ).metadata_path(metadata_dir)
        )
    except CalledProcessError as e:
        raise RuntimeError(
            f"Failed to build metadata for {file.filename}: {e}\n"
            f"--- Stdout:\n{capture.stdout}\n"
            f"--- Stderr:\n{capture.stderr}\n"
            "---\n"
        ) from None
    if capture.stderr:
        logger.warning(
            f"Messages from building {file.filename}:\n---"
            f" stderr:\n{capture.stderr.strip()}\n---\n"
        )
    logger.info(f"Finished {file.filename}")
    [dist_info] = filter(
        lambda x: x.name.endswith(".dist-info"), metadata_dir.iterdir()
    )
    return Distribution.at(dist_info).metadata.json
