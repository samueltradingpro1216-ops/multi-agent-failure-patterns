# Pattern #36 — Wrong Directory Read

**Category:** I/O & Persistence
**Severity:** Medium
**Tags:** `path-resolution`, `configuration`, `stale-data`, `symlinks`, `mount-points`

---

## 1. Observable Symptoms

The system behaves incorrectly in ways that are difficult to reproduce because the bug activates only when two versions of the same filename coexist in different directories. Symptoms are subtle and often misattributed to configuration drift or human error.

- A module applies settings or processes data that was known to be updated, but continues behaving as if the old values are still in effect.
- Logs show no read errors, no `FileNotFoundError`, and no permission failures. The file is opened and parsed successfully.
- The issue is intermittent across environments: it manifests in production (where directories diverge) but not in development (where only one directory exists).
- Engineers can confirm the correct file is present at the expected path, yet the running process uses different values. Inserting a debug print of the resolved path reveals it points somewhere unexpected.
- Symptoms worsen after a deployment or migration that moved files to a new canonical location without removing the old copy.
- Container restarts temporarily resolve the problem because volume mounts are re-established, pointing the process back to the correct directory — until the next drift event.

---

## 2. Field Story

A media company operated a video transcoding pipeline that processed inbound footage into multiple output resolutions. The pipeline was coordinated by a central orchestrator that read a `codec_profiles.json` file at startup to determine encoding parameters — bitrate ladders, keyframe intervals, and audio normalization targets.

The pipeline had two relevant directories: `/app/config/` (the canonical runtime configuration directory, populated from a Kubernetes ConfigMap) and `/app/data/` (a shared data volume used for intermediate assets). During an infrastructure migration eighteen months earlier, an engineer had temporarily copied `codec_profiles.json` into `/app/data/` to test a new encoding profile. That copy was never removed.

A code change six weeks before the incident introduced a new environment variable, `DATA_DIR`, defaulting to `/app/data/`. A helper function was updated to read auxiliary files from `DATA_DIR`. The developer intended this only for binary asset lookups, but a subtle error caused the `codec_profiles.json` load path to also resolve through `DATA_DIR`.

The production pipeline started consuming the stale codec profile from `/app/data/codec_profiles.json`. The file in that location was eighteen months old and contained outdated bitrate targets. Output video quality degraded. Client complaints about pixelation in fast-motion scenes accumulated over two weeks before an engineer correlated the symptom to an encoding parameter mismatch. The correct file was sitting at `/app/config/codec_profiles.json`, completely ignored.

No alarm had fired. The file read succeeded. The JSON parsed without error. The system was operating exactly as coded — just pointed at the wrong place.

---

## 3. Technical Root Cause

The root cause is path resolution using an incorrect base directory variable, compounded by the silent success of a valid file existing at the wrong location.

```python
import os
import json

# These are set from environment variables
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/app/config")
DATA_DIR   = os.environ.get("DATA_DIR",   "/app/data")

def load_codec_profiles():
    # BUG: uses DATA_DIR — should be CONFIG_DIR
    path = os.path.join(DATA_DIR, "codec_profiles.json")
    with open(path) as f:
        return json.load(f)
```

The function never raises an exception because `/app/data/codec_profiles.json` exists. Python's `open()` and `json.load()` have no knowledge of which file is "correct" — they operate on whatever path they receive.

Several compounding factors make this pattern particularly dangerous:

**Silent success masking the error.** If the wrong file did not exist, a `FileNotFoundError` would surface immediately. The presence of a stale copy in the wrong directory is what makes the bug invisible.

**Relative paths in multi-directory environments.** If the code used `"./codec_profiles.json"` (relative path) and the process working directory was `/app/data/`, the same failure occurs. Relative paths resolve against the process working directory, which can differ across deployment environments, container entrypoints, or systemd unit configurations.

**Symlink indirection.** If `/app/data/` is a symlink to a mounted volume that also contains a `codec_profiles.json`, `os.path.join` and `open()` follow the link without any indication that indirection occurred.

**Environment variable shadowing.** When `DATA_DIR` and `CONFIG_DIR` are both present but one is an empty string (e.g., from an incomplete deployment), `os.environ.get("DATA_DIR", "/app/data")` returns the empty string, causing `os.path.join("", "codec_profiles.json")` to resolve to `"codec_profiles.json"` — a relative path. The file found is then whichever version exists in the process working directory.

```python
# Demonstration of the empty-string trap
DATA_DIR = ""
path = os.path.join(DATA_DIR, "codec_profiles.json")
print(path)  # Output: "codec_profiles.json" — resolves relative to cwd
```

---

## 4. Detection

### 4.1 Static Analysis

Scan the codebase for file open calls and verify that configuration files are loaded exclusively through a `CONFIG_DIR`-derived path and never through `DATA_DIR`, `CACHE_DIR`, or any other non-configuration directory variable.

```python
import ast
import sys
from pathlib import Path

CONFIG_FILES = {"codec_profiles.json", "settings.json", "app_config.json"}
WRONG_DIR_VARS = {"DATA_DIR", "CACHE_DIR", "TEMP_DIR", "OUTPUT_DIR"}

def audit_open_calls(source_path: str) -> list[dict]:
    """
    Parse a Python source file and report any open() call that constructs
    a path using a non-CONFIG_DIR variable for a known configuration filename.
    """
    violations = []
    source = Path(source_path).read_text(encoding="utf-8")
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_open = (
            (isinstance(func, ast.Name) and func.id == "open") or
            (isinstance(func, ast.Attribute) and func.attr == "open")
        )
        if not is_open:
            continue
        if not node.args:
            continue
        path_arg = node.args[0]
        # Look for os.path.join(WRONG_VAR, "config_file")
        if not isinstance(path_arg, ast.Call):
            continue
        join_func = path_arg.func
        is_join = (
            isinstance(join_func, ast.Attribute) and
            join_func.attr == "join"
        )
        if not is_join:
            continue
        join_args = path_arg.args
        if len(join_args) < 2:
            continue
        dir_arg  = join_args[0]
        file_arg = join_args[1]
        dir_name  = dir_arg.id  if isinstance(dir_arg,  ast.Name) else None
        file_name = file_arg.s  if isinstance(file_arg, ast.Constant) else None
        if dir_name in WRONG_DIR_VARS and file_name in CONFIG_FILES:
            violations.append({
                "file":     source_path,
                "line":     node.lineno,
                "dir_var":  dir_name,
                "filename": file_name,
            })
    return violations

if __name__ == "__main__":
    for source_file in sys.argv[1:]:
        for v in audit_open_calls(source_file):
            print(f"[VIOLATION] {v['file']}:{v['line']} — "
                  f"{v['dir_var']} used to load config file '{v['filename']}'")
```

### 4.2 Runtime Detection

At application startup, log the resolved absolute path for every configuration file load. Compare the resolved path against the expected canonical prefix and raise an error if it does not match.

```python
import os
import json
import logging

logger = logging.getLogger(__name__)

EXPECTED_CONFIG_PREFIX = os.environ.get("CONFIG_DIR", "/app/config")

def safe_load_config(filename: str) -> dict:
    """
    Load a JSON configuration file and assert that the resolved path
    starts with the canonical CONFIG_DIR prefix. Raises RuntimeError
    if the file resolves to an unexpected location.
    """
    config_dir = os.environ.get("CONFIG_DIR", "/app/config")
    raw_path   = os.path.join(config_dir, filename)
    real_path  = os.path.realpath(raw_path)  # resolves symlinks

    logger.info("Loading config: raw=%s resolved=%s", raw_path, real_path)

    canonical = os.path.realpath(EXPECTED_CONFIG_PREFIX)
    if not real_path.startswith(canonical + os.sep) and real_path != canonical:
        raise RuntimeError(
            f"Config file '{filename}' resolved to '{real_path}', "
            f"which is outside the expected config directory '{canonical}'. "
            f"Check CONFIG_DIR environment variable and symlink targets."
        )

    with open(real_path, encoding="utf-8") as f:
        data = json.load(f)

    logger.info("Config loaded: file=%s keys=%s", filename, list(data.keys()))
    return data
```

### 4.3 Integration Test

Write a test that creates conflicting copies of the same file in two directories and verifies that the loader always reads from the canonical configuration directory.

```python
import json
import os
import tempfile
import pytest
from unittest.mock import patch

# Import the function under test
# from pipeline.config import load_codec_profiles

def load_codec_profiles_under_test(config_dir: str, data_dir: str) -> dict:
    """Reference implementation to exercise in the test."""
    path = os.path.join(config_dir, "codec_profiles.json")
    real = os.path.realpath(path)
    canonical = os.path.realpath(config_dir)
    if not real.startswith(canonical):
        raise RuntimeError(f"Unexpected config path: {real}")
    with open(real, encoding="utf-8") as f:
        return json.load(f)

def test_reads_from_config_dir_not_data_dir():
    with tempfile.TemporaryDirectory() as config_dir, \
         tempfile.TemporaryDirectory() as data_dir:

        # Write CORRECT profile to config_dir
        correct_profile = {"bitrate": 4000, "version": "current"}
        with open(os.path.join(config_dir, "codec_profiles.json"), "w") as f:
            json.dump(correct_profile, f)

        # Write STALE profile to data_dir (simulates forgotten copy)
        stale_profile = {"bitrate": 800, "version": "stale-18-months-old"}
        with open(os.path.join(data_dir, "codec_profiles.json"), "w") as f:
            json.dump(stale_profile, f)

        result = load_codec_profiles_under_test(config_dir, data_dir)
        assert result["version"] == "current", (
            f"Expected current profile but got: {result}"
        )
        assert result["bitrate"] == 4000

def test_raises_when_config_dir_resolves_outside_canonical(tmp_path):
    """Verify that symlink traversal into data_dir is detected."""
    config_dir = tmp_path / "config"
    data_dir   = tmp_path / "data"
    config_dir.mkdir()
    data_dir.mkdir()

    stale = {"bitrate": 800, "version": "stale"}
    (data_dir / "codec_profiles.json").write_text(json.dumps(stale))

    # Create a symlink in config_dir pointing into data_dir
    symlink_path = config_dir / "codec_profiles.json"
    symlink_path.symlink_to(data_dir / "codec_profiles.json")

    with pytest.raises(RuntimeError, match="Unexpected config path"):
        load_codec_profiles_under_test(str(config_dir), str(data_dir))
```

---

## 5. Fix

### 5.1 Immediate Fix

Correct every path construction that loads configuration files to use `CONFIG_DIR` exclusively. Add `os.path.realpath` resolution and a prefix assertion so that future path drift is caught at load time rather than silently accepted.

```python
import os
import json
import logging

logger = logging.getLogger(__name__)

def load_codec_profiles() -> dict:
    """
    Load codec profiles from the canonical configuration directory.
    Resolves symlinks and validates the final path is within CONFIG_DIR.
    """
    config_dir = os.environ.get("CONFIG_DIR")
    if not config_dir:
        raise EnvironmentError(
            "CONFIG_DIR environment variable is not set. "
            "Cannot safely determine the configuration directory."
        )

    # Resolve symlinks on both the directory and the full file path
    canonical_dir = os.path.realpath(config_dir)
    raw_path      = os.path.join(canonical_dir, "codec_profiles.json")
    resolved_path = os.path.realpath(raw_path)

    # Guard: resolved path must remain within canonical_dir
    if not resolved_path.startswith(canonical_dir + os.sep):
        raise RuntimeError(
            f"Resolved path '{resolved_path}' escapes CONFIG_DIR "
            f"'{canonical_dir}'. Possible symlink attack or misconfiguration."
        )

    logger.info("Loading codec profiles from: %s", resolved_path)

    with open(resolved_path, encoding="utf-8") as f:
        profiles = json.load(f)

    logger.info(
        "Codec profiles loaded: version=%s profiles=%d",
        profiles.get("version", "unknown"),
        len(profiles.get("profiles", [])),
    )
    return profiles
```

### 5.2 Defensive Loader Utility

Consolidate all configuration loading through a single utility class that enforces directory boundaries at construction time and exposes no raw path building to callers.

```python
import os
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

class ConfigLoader:
    """
    A configuration file loader that enforces a strict directory boundary.
    All file reads are guaranteed to resolve within the canonical config
    directory, even when symlinks are present.

    Usage:
        loader = ConfigLoader.from_env()
        profiles = loader.load_json("codec_profiles.json")
    """

    def __init__(self, config_dir: str) -> None:
        if not config_dir:
            raise ValueError("config_dir must be a non-empty string.")
        resolved = os.path.realpath(config_dir)
        if not os.path.isdir(resolved):
            raise NotADirectoryError(
                f"CONFIG_DIR '{config_dir}' resolves to '{resolved}', "
                f"which is not a directory."
            )
        self._canonical_dir = resolved
        logger.info("ConfigLoader initialized: canonical_dir=%s", resolved)

    @classmethod
    def from_env(cls, env_var: str = "CONFIG_DIR") -> "ConfigLoader":
        value = os.environ.get(env_var, "")
        if not value:
            raise EnvironmentError(
                f"Environment variable '{env_var}' is not set or is empty."
            )
        return cls(value)

    def resolve(self, filename: str) -> str:
        """
        Return the resolved absolute path for a filename within the config
        directory. Raises RuntimeError if the path escapes the boundary.
        """
        if os.sep in filename or "/" in filename:
            raise ValueError(
                f"filename must be a bare filename, not a path: '{filename}'"
            )
        raw      = os.path.join(self._canonical_dir, filename)
        resolved = os.path.realpath(raw)
        if not resolved.startswith(self._canonical_dir + os.sep):
            raise RuntimeError(
                f"File '{filename}' resolves to '{resolved}', which is "
                f"outside ConfigLoader boundary '{self._canonical_dir}'."
            )
        return resolved

    def load_json(self, filename: str) -> Any:
        path = self.resolve(filename)
        logger.debug("ConfigLoader.load_json: path=%s", path)
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def exists(self, filename: str) -> bool:
        try:
            path = self.resolve(filename)
            return os.path.isfile(path)
        except (RuntimeError, ValueError):
            return False
```

---

## 6. Architectural Prevention

**Explicit directory roles in environment schema.** Define every directory variable in a central schema document (or `pydantic.BaseSettings` model) with a declared role. Enforce at startup that `CONFIG_DIR`, `DATA_DIR`, `CACHE_DIR`, and `OUTPUT_DIR` do not overlap after symlink resolution.

```python
from pydantic import BaseSettings, validator
import os

class DirectorySettings(BaseSettings):
    config_dir: str
    data_dir: str
    cache_dir: str

    @validator("data_dir", "cache_dir", each_item=False)
    def must_not_equal_config_dir(cls, v, values):
        config = os.path.realpath(values.get("config_dir", ""))
        candidate = os.path.realpath(v)
        if candidate == config:
            raise ValueError(
                f"Directory '{v}' resolves to the same path as config_dir "
                f"'{config}'. Directory roles must not overlap."
            )
        return v

    class Config:
        env_file = ".env"
```

**CI artifact hygiene.** In container build pipelines, add a step that checks for configuration files copied into non-configuration directories and fails the build:

```bash
# In Dockerfile or CI script:
# Fail if any *.json config file exists outside /app/config/
find /app -name "*.json" ! -path "/app/config/*" \
  -exec echo "ARTIFACT_LEAK: {}" \; \
  -exec false \;
```

**Immutable configuration mounts.** Mount configuration directories as read-only (`readOnly: true` in Kubernetes) so that no runtime process can write a configuration file into the wrong place.

---

## 7. Anti-patterns to Avoid

- **Using a single `BASE_DIR` for everything.** A single variable used for config, data, and cache makes it impossible to enforce boundaries between file roles. Separate directory variables for separate concerns.
- **Constructing paths inline at every call site.** `os.path.join(os.environ.get("DATA_DIR", "."), "config.json")` scattered through the codebase creates dozens of independent failure points. Centralize path construction.
- **Ignoring the working directory for relative paths.** `open("config.json")` silently reads from `os.getcwd()`, which changes between test runs, Docker containers, and systemd service invocations.
- **Leaving stale copies of files after migrations.** Never leave the old copy in place and assume the code will find the new one. Delete or archive the old copy as part of the migration script.
- **Relying on `os.path.exists()` to validate the correct path.** Existence is not correctness. A file can exist at the wrong location. Validate the path boundary, not just existence.

---

## 8. Edge Cases and Variants

**Double-mount in Docker Compose.** When two services share a volume and both write a file with the same name, the reading service loads whichever version was written last — which depends on startup ordering. This is non-deterministic across deployments.

**`os.path.expanduser` with wrong HOME.** If `HOME` is overridden in a container environment, `~/config.json` resolves to an unexpected directory. Always use absolute environment-variable-backed paths, not `~`.

**Case-insensitive filesystems.** On macOS or Windows, `Config.json` and `config.json` refer to the same file. A mixed-case filename in the code loads a differently-named stale copy on a case-insensitive filesystem without raising any error.

**Bind mounts that shadow directories.** In Kubernetes, a `volumeMount` with `mountPath: /app/config` silently shadows any files baked into the container image at that path. If the mounted ConfigMap does not include all expected files, the code falls back to... nothing, and the `open()` raises `FileNotFoundError` — but only in the production cluster, not in local development.

**`pathlib.Path` joins with absolute second argument.** `Path("/app/data") / "/app/config/file.json"` returns `Path("/app/config/file.json")` — the first component is discarded. This can cause a path-construction bug where the intent was to build a data path but the absolute second argument forces resolution to the config directory (or vice versa).

---

## 9. Audit Checklist

- [ ] Every `open()` call that loads a configuration file uses `CONFIG_DIR`, not `DATA_DIR`, `CACHE_DIR`, or any other non-configuration variable.
- [ ] All directory variables are validated at startup: non-empty, exist as directories, and are mutually non-overlapping after `os.path.realpath()` resolution.
- [ ] The resolved path of every configuration file is logged at `INFO` level at startup.
- [ ] A path-boundary assertion (`startswith(canonical_dir)`) is present in every configuration loader function.
- [ ] No configuration files exist in data, cache, or output directories in any environment (verified by CI).
- [ ] Relative paths (`"./file.json"`, `"../config/file.json"`) are absent from all configuration loading code.
- [ ] Symlink targets for configuration directory mounts point to the correct canonical location and are tested in the deployment verification script.
- [ ] Integration tests create conflicting copies of configuration files in multiple directories and assert that the correct version is loaded.
- [ ] `CONFIG_DIR` is a required environment variable with no default value; absence causes a hard startup failure.
- [ ] Container images are built with read-only configuration directory mounts enforced in the manifest.

---

## 10. Further Reading

- Python documentation — `os.path.realpath`: https://docs.python.org/3/library/os.path.html#os.path.realpath
- Python documentation — `pathlib.Path.resolve`: https://docs.python.org/3/library/pathlib.html#pathlib.Path.resolve
- The Twelve-Factor App — Config: https://12factor.net/config
- OWASP Path Traversal: https://owasp.org/www-community/attacks/Path_Traversal
- Kubernetes ConfigMaps and volume mounts: https://kubernetes.io/docs/concepts/configuration/configmap/
- Linux `mount --bind` and overlay filesystem semantics: https://www.kernel.org/doc/html/latest/filesystems/overlayfs.html
- `pydantic` `BaseSettings` for environment-driven configuration: https://docs.pydantic.dev/latest/concepts/pydantic_settings/
