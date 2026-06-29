from __future__ import annotations

import base64
import os
import re
import shlex
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse, urlunparse


def git_ssh_command_for_key(key_path: str) -> Optional[str]:
    if not key_path.strip():
        return None
    resolved = str(Path(key_path).expanduser())
    return (
        f"ssh -F /dev/null -i {shlex.quote(resolved)} "
        "-o IdentitiesOnly=yes -o PreferredAuthentications=publickey "
        "-o StrictHostKeyChecking=accept-new"
    )


def git_env_with_ssh_key(key_path: str) -> Optional[Dict[str, str]]:
    env = _safe_git_env()
    ssh_command = git_ssh_command_for_key(key_path)
    if not ssh_command:
        return env
    env["GIT_SSH_COMMAND"] = ssh_command
    return env


def git_env_with_identity(
    *,
    ssh_key_path: str = "",
    access_token: str = "",
    token_host: str = "github.com",
) -> Optional[Dict[str, str]]:
    env = _safe_git_env()
    token = access_token.strip()
    if token:
        env["GIT_TERMINAL_PROMPT"] = "0"
        _append_git_config(
            env,
            f"http.https://{token_host}/.extraheader",
            _gitlab_access_token_header(token),
        )
        return env
    return git_env_with_ssh_key(ssh_key_path)


def _safe_git_env() -> Dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ALLOW_PROTOCOL"] = "https:ssh"
    return env


def git_url_for_access_token(git_url: str) -> str:
    if "://" in git_url:
        parsed = urlparse(git_url)
        if parsed.scheme == "ssh" and parsed.hostname:
            path = parsed.path.lstrip("/")
            return urlunparse(("https", _netloc_without_userinfo(parsed), f"/{path}", "", "", ""))
        if parsed.scheme == "https" and parsed.hostname:
            return urlunparse(("https", _netloc_without_userinfo(parsed), parsed.path, "", "", ""))
        return git_url
    match = re.match(r"^[^@\s]+@([^:\s]+):(.+)$", git_url)
    if not match:
        return git_url
    host, path = match.groups()
    return f"https://{host}/{path.lstrip('/')}"


def has_git_access_token(access_token: str) -> bool:
    return bool(access_token.strip())


def _gitlab_access_token_header(access_token: str) -> str:
    encoded = base64.b64encode(f"oauth2:{access_token}".encode("utf-8")).decode("ascii")
    return f"Authorization: Basic {encoded}"


def _append_git_config(env: Dict[str, str], key: str, value: str) -> None:
    try:
        index = int(env.get("GIT_CONFIG_COUNT", "0") or "0")
    except ValueError:
        index = 0
    env[f"GIT_CONFIG_KEY_{index}"] = key
    env[f"GIT_CONFIG_VALUE_{index}"] = value
    env["GIT_CONFIG_COUNT"] = str(index + 1)


def _netloc_without_userinfo(parsed) -> str:
    host = parsed.hostname or ""
    if parsed.port is not None:
        return f"{host}:{parsed.port}"
    return host
