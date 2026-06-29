from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from cr_agent.core.git_identity import (
    git_ssh_command_for_key,
    git_env_with_ssh_key,
    git_env_with_identity,
    git_url_for_access_token,
    has_git_access_token,
)


def test_git_ssh_command_for_key() -> None:
    assert git_ssh_command_for_key("") is None
    assert git_ssh_command_for_key("   ") is None
    cmd = git_ssh_command_for_key("~/some_key")
    assert cmd is not None
    assert "-i" in cmd
    assert "some_key" in cmd


def test_git_env_with_ssh_key() -> None:
    env_without_key = git_env_with_ssh_key("")
    assert env_without_key is not None
    assert env_without_key["GIT_TERMINAL_PROMPT"] == "0"
    assert env_without_key["GIT_ALLOW_PROTOCOL"] == "https:ssh"
    assert "GIT_SSH_COMMAND" not in env_without_key
    env = git_env_with_ssh_key("~/some_key")
    assert env is not None
    assert env["GIT_ALLOW_PROTOCOL"] == "https:ssh"
    assert "GIT_SSH_COMMAND" in env


def test_git_env_with_identity() -> None:
    # Test fallback to SSH when token is empty
    env = git_env_with_identity(ssh_key_path="~/some_key", access_token="")
    assert env is not None
    assert env["GIT_ALLOW_PROTOCOL"] == "https:ssh"
    assert "GIT_SSH_COMMAND" in env
    assert "GIT_CONFIG_COUNT" not in env

    # Test token environment variables
    env = git_env_with_identity(ssh_key_path="~/some_key", access_token="my_token")
    assert env is not None
    assert env.get("GIT_TERMINAL_PROMPT") == "0"
    assert env["GIT_ALLOW_PROTOCOL"] == "https:ssh"
    assert env.get("GIT_CONFIG_KEY_0") == "http.https://github.com/.extraheader"
    assert "Authorization: Basic" in env.get("GIT_CONFIG_VALUE_0", "")
    assert env.get("GIT_CONFIG_COUNT") == "1"
    assert "GIT_SSH_COMMAND" not in env


def test_git_url_for_access_token() -> None:
    # SSH urls
    assert git_url_for_access_token("git@github.com:comain/code-review-agent.git") == "https://github.com/comain/code-review-agent.git"
    assert git_url_for_access_token("ssh://git@github.com:22/comain/code-review-agent.git") == "https://github.com:22/comain/code-review-agent.git"

    # HTTPS urls
    assert git_url_for_access_token("https://github.com/comain/code-review-agent.git") == "https://github.com/comain/code-review-agent.git"
    assert git_url_for_access_token("https://user@github.com/comain/code-review-agent.git") == "https://github.com/comain/code-review-agent.git"

    # Non-standard/invalid urls
    assert git_url_for_access_token("/local/path") == "/local/path"


def test_has_git_access_token() -> None:
    assert not has_git_access_token("")
    assert not has_git_access_token("  ")
    assert has_git_access_token("token")
