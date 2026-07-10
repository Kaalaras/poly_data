from __future__ import annotations

import fnmatch
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _guard() -> ModuleType:
    path = REPO_ROOT / "scripts" / "agent_guard.py"
    spec = importlib.util.spec_from_file_location("agent_guard_regression", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _policy() -> dict[str, object]:
    return json.loads((REPO_ROOT / ".agent-policy.json").read_text(encoding="utf-8"))


def _command_reason(command: str, env: dict[str, str] | None = None) -> str | None:
    return _guard().evaluate(
        {"tool_name": "Bash", "tool_input": {"command": command}},
        _policy(),
        env or {},
    )


@pytest.mark.parametrize(
    "command",
    [
        "git.exe --git-dir=.git push origin main --force",
        "git -C . --no-pager reset --hard HEAD^",
        "git --no-pager clean -fd",
        "git push origin --delete main",
        "git push origin -d main",
        "git push --prune origin",
        "git push origin :main",
        "git push --mirror origin",
        "env curl -X POST https://example.com/api/items",
        "$m='POST'; curl -X $m https://example.com/api/items",
        "curl --request $HTTP_METHOD https://example.com/api/items",
        "cmd /c curl -X POST https://example.com/api/items",
        "bash -lc \"curl -X POST https://example.com/api/items\"",
        "powershell -Command \"Invoke-RestMethod -Method Delete https://example.com/api/items\"",
        "$m='DELETE'; Invoke-RestMethod -Method $m https://example.com/api/items",
        "Start-Process curl -ArgumentList '-X','POST','https://example.com/api/items'",
        "Write-Output $(curl -X POST https://example.com/api/items)",
        "echo ready | xargs curl -X POST https://example.com/api/items",
        "uv run -- python -c \"import requests; requests.post('https://example.com/api/items')\"",
        "python -c \"import requests; s=requests.Session(); s.post('https://example.com/api/items')\"",
        "python -c \"import requests; m='POST'; requests.request(m, 'https://example.com/api/items')\"",
        "python -c \"import urllib3; p=urllib3.PoolManager(); p.request('POST', 'https://example.com/api/items')\"",
        "python -c \"import aiohttp; s=aiohttp.ClientSession(); s.post('https://example.com/api/items')\"",
        "python -c \"import http.client; c=http.client.HTTPSConnection('example.com'); c.request('POST', '/api/items')\"",
        "python -c \"import httpx; httpx.stream('POST', 'https://example.com/api/items')\"",
        "python -c \"import subprocess; subprocess.run(['curl', '-X', 'POST', 'https://example.com/api/items'])\"",
        "python - <<'PY'\nimport requests\nrequests.post('https://example.com/api/items')\nPY",
        "node -e \"require('child_process').execSync('curl -X POST https://example.com/api/items')\"",
        "node -e \"require('child_process').spawnSync('curl',['-X','POST','https://example.com/api/items'],{stdio:'inherit'})\"",
        "node -e \"require('child_process').execFileSync('curl',['-X','POST','https://example.com/api/items'])\"",
        "node -e \"const axios=require('axios'); axios.post('https://example.com/api/items',{})\"",
        "node -e \"const axios=require('axios'); const c=axios.create(); c.post('https://example.com/api/items',{})\"",
        "node -e \"const {post}=require('axios'); post('https://example.com/api/items',{})\"",
        "node -e \"const axios=require('axios'); axios({method:'post',url:'https://example.com/api/items'})\"",
        "node -e \"const got=require('got'); got.post('https://example.com/api/items')\"",
        "node -e \"require('undici').request('https://example.com/api/items',{method:'POST'})\"",
        "node -e \"require('https').request({hostname:'example.com',method:'POST'}).end()\"",
        "node -e \"const m='POST'; fetch('https://example.com/api/items',{method:m})\"",
        "$c=\"Invoke-RestMethod https://example.com/api/items -Method POST\"; Invoke-Expression $c",
        "Invoke-Expression $dynamicCommand",
        "python -",
        "(curl -X POST https://example.com/api/items)",
        "{ curl -X POST https://example.com/api/items; }",
        "if true; then curl -X POST https://example.com/api/items; fi",
        "& { Invoke-RestMethod -Method POST https://example.com/api/items }",
        "Invoke-Command -ScriptBlock { Invoke-RestMethod -Method POST https://example.com/api/items }",
        "case x in x) curl -X POST https://example.com/api/items;; esac",
        "1 | ForEach-Object { Invoke-RestMethod -Method POST https://example.com/api/items }",
        "Start-ThreadJob -ScriptBlock { Invoke-RestMethod -Method POST https://example.com/api/items }",
        "gh -R acme/demo api repos/acme/demo/issues -X POST -f title=test",
        "gh -R acme/demo issue create --title test --body body",
        "gh repo delete acme/demo --yes",
        "gh repo fork acme/demo --clone=false",
        "gh repo sync acme/fork --source acme/upstream --force",
        "gh issue transfer 12 acme/other",
        "gh codespace delete -c name --force",
        "gh ssh-key add key.pub --title test",
        "gh auth login --with-token",
        "gh workflow enable ci.yml",
        "gh auth token",
        "Write-Output $env:HF_TOKEN",
        "Get-ChildItem Env:",
        "ls Env:",
        "gci Env:*TOKEN*",
        "Get-Item Env:HF_TOKEN",
        "gi Env:HF_TOKEN",
        "Get-Content Env:HF_TOKEN",
        "$env:HF_TOKEN",
        "[Environment]::GetEnvironmentVariable('HF_TOKEN')",
        "[Environment]::GetEnvironmentVariables()",
        "[System.Environment]::ExpandEnvironmentVariables('%HF_TOKEN%')",
        "powershell -Command '$env:HF_TOKEN'",
        "printenv HF_TOKEN",
        "export -p",
        "declare -p",
        "typeset -p",
        "compgen -e",
        "cmd /c set HF_TOKEN",
        "python -c \"import os; print(os.environ['HF_TOKEN'])\"",
        "node -e \"console.log(process.env.HF_TOKEN)\"",
        "node -e \"console.log(process.env)\"",
        "echo url=https://example.com | git credential fill",
        "rg . .env",
        "rg -n token .env.local",
        "grep . ~/.ssh/config",
        "Select-String -Path .env -Pattern .",
        "sls . .env",
        "findstr . .env",
        "awk '{print}' .env",
        "sed -n p .env",
        "strings .env",
        "xxd .env",
        "od -c .env",
        "cut -c1- .env",
        "sort .env",
        "source .env",
        ". .env",
        "git show HEAD:.env",
        "cat <.env",
        "CAT <.ENV",
        "cat .e?v",
        "cat .e*",
        "cat .e[n]v",
        "Get-Content .e*",
        "cat cred*",
        "cat config/.e*",
        "cat config/cred*",
        "Get-Content config/CRED*",
        "cat id_*",
        "rg . .e*",
        "& { GET-CONTENT .ENV }",
        "node -e \"require('fs').readFile('.env', () => {})\"",
        "node -e \"require('fs').createReadStream('.env')\"",
        "[IO.File]::OpenText('.env').ReadToEnd()",
        "python - <<'PY'\nprint(open('.env').read())\nPY",
        "node -e \"require('child_process').execSync('cat .env',{stdio:'inherit'})\"",
        "cp .env src/leak.txt",
        "Copy-Item .env src/leak.txt",
        "mv .env src/leak.txt",
        "tar -cf leak.tar .env",
        "zip leak.zip .env",
        "base64 .env",
        "certutil -encode .env leak.txt",
        "git add .env",
        "git add -f .env.local",
        "git add config/credentials.json",
        "git add .",
        "git add --all",
        "git add ':(top)'",
        "git add '*'",
        "git update-index --add .env",
        "git commit -am test",
        "curl file://$PWD/.env",
        "curl file:.env",
        "wget file://$PWD/.ssh/config",
        "Invoke-WebRequest file://$PWD/.env",
        "npm --prefix . publish",
        "uv publish",
        "poetry publish",
        "hatch publish",
        "python -m twine --repository pypi upload dist/*",
        "docker image push example/image:latest",
        "http https://example.com/api/items name=value",
        "git checkout -- src/example.py",
        "git restore src/example.py",
        "git switch --discard-changes feature",
        "git checkout -f main",
        "git checkout --force main",
        "git switch -f main",
        "git switch --force main",
        "git branch -D feature",
        "git branch --delete --force feature",
        "git stash clear",
        "git stash drop",
        "git update-ref -d refs/heads/feature",
        "cmd /c rmdir /s /q build",
        "python -c \"import shutil; shutil.rmtree('build')\"",
    ],
)
def test_common_high_risk_commands_are_denied(command: str) -> None:
    assert _command_reason(command) is not None, command


@pytest.mark.parametrize(
    "command",
    [
        "python -c \"from pathlib import Path; Path('.agent-policy.json').write_text('{}')\"",
        "python -c \"open('.agent-policy.json', 'w').write('{}')\"",
        "node -e \"require('fs').writeFileSync('.agent-policy.json', '{}')\"",
        "node -e \"require('fs').writeFile('.agent-policy.json', '{}', () => {})\"",
        "node -e \"require('fs').promises.writeFile('.agent-policy.json', '{}')\"",
        "node -e \"const fs=require('fs'),p='.agent-policy.json'; fs.writeFileSync(p,'{}')\"",
        "Set-Content .agent-policy.json '{}'",
        "Set-Content .AGENT-POLICY.JSON '{}'",
        "Set-Item .agent-policy.json '{}'",
        "si .agent-policy.json '{}'",
        "$p='.agent-policy.json'; Set-Content $p '{}'",
        "sc .agent-policy.json '{}'",
        "ac .agent-policy.json '{}'",
        "clc .agent-policy.json",
        "ri .agent-policy.json",
        "mi README.md .agent-policy.json -Force",
        "rni README.md .agent-policy.json",
        "ni .agent-policy.json -ItemType File -Force",
        "[System.IO.File]::WriteAllText('.agent-policy.json', '{}')",
        "echo changed > .agent-policy.json",
        "p=.agent-policy.json; echo changed > \"$p\"",
        "printf '{}' | tee .agent-policy.json",
        "sed -i 's/x/y/' .agent-policy.json",
        "perl -pi -e 's/x/y/' .agent-policy.json",
        "git checkout-index -f -- .agent-policy.json",
        "git rm .agent-policy.json",
        "git add .agent-policy.json",
        "git apply policy.patch",
        "dd if=README.md of=.agent-policy.json",
        "install README.md .agent-policy.json",
        "rsync README.md .agent-policy.json",
        "ln -sf README.md .agent-policy.json",
        "chmod 000 .agent-policy.json",
        "rm .agent-*",
    ],
)
def test_command_side_policy_edits_require_amendment(command: str) -> None:
    reason = _command_reason(command)

    assert reason is not None, command
    assert "protected path" in reason


@pytest.mark.parametrize(
    "command",
    [
        "git push origin feature",
        "git add src/example.py",
        "git commit -m test",
        "curl https://example.com/status",
        "Invoke-WebRequest https://example.com/status",
        "python -c \"import requests; requests.get('https://example.com/status')\"",
        "Write-Output 'Get-Content .env'",
        "rg -n 'Get-Content .env' docs",
        "gh api graphql -f query='query { repository(owner: \"x\", name: \"mutation\") { id } }'",
        "gh auth status",
        "gh issue list",
        "gh pr view 12",
        "gh repo view acme/demo",
        "gh run view 123",
    ],
)
def test_common_read_only_commands_remain_autonomous(command: str) -> None:
    assert _command_reason(command) is None, command


@pytest.mark.parametrize(
    "payload",
    [
        {"tool_name": "Grep", "tool_input": {"path": ".env", "pattern": "."}},
        {"tool_name": "mcp__filesystem__read_file", "tool_input": {"path": ".env"}},
        {"tool_name": "mcp__filesystem__read_file", "tool_input": {"path": ".ENV"}},
        {"tool_name": "mcp__filesystem__read_file", "tool_input": {"path": "CREDENTIALS.JSON"}},
        {"tool_name": "mcp__filesystem__read_file", "tool_input": {"path": "ID_RSA"}},
        {"tool_name": "mcp__filesystem__read_file", "tool_input": {"path": "credentials.json"}},
        {"tool_name": "mcp__filesystem__read_file", "tool_input": {"path": "id_rsa"}},
        {"tool_name": "mcp__filesystem__read_media_file", "tool_input": {"path": ".env"}},
        {"tool_name": "mcp__lean_ctx__ctx_read", "tool_input": {"path": ".env"}},
        {
            "tool_name": "mcp__lean_ctx__ctx_compose",
            "tool_input": {"path": ".ssh", "task": "show config"},
        },
        {
            "tool_name": "mcp__lean_ctx__ctx_search",
            "tool_input": {"path": ".env", "pattern": "."},
        },
        {
            "tool_name": "mcp__lean_ctx__ctx_shell",
            "tool_input": {"command": "git push origin main --force"},
        },
        {
            "tool_name": "mcp__lean_ctx__ctx_call",
            "tool_input": {
                "name": "ctx_shell",
                "arguments": {"command": "curl -X POST https://example.com/api/items"},
            },
        },
        {
            "tool_name": "mcp__filesystem__write_file",
            "tool_input": {"path": ".agent-policy.json", "content": "{}"},
        },
        {
            "tool_name": "mcp__filesystem__write_file",
            "tool_input": {"path": ".AGENT-POLICY.JSON", "content": "{}"},
        },
        {
            "tool_name": "mcp__filesystem__move_file",
            "tool_input": {"source": "README.md", "destination": ".agent-policy.json"},
        },
        {
            "tool_name": "mcp__lean_ctx__ctx_edit",
            "tool_input": {
                "path": ".agent-policy.json",
                "old_string": "x",
                "new_string": "y",
            },
        },
    ],
)
def test_native_and_namespaced_tools_cannot_bypass_policy(
    payload: dict[str, object],
) -> None:
    assert _guard().evaluate(payload, _policy(), {}) is not None


def test_external_authority_does_not_bypass_core_invariants() -> None:
    guard = _guard()
    curl = "curl -X POST https://example.com/api/items"
    issue = "gh issue create --title test --body body"
    curl_authority = {
        "AGENT_EXTERNAL_EFFECT_AUTHORITY": guard.external_authority_for_command(curl)
    }
    issue_authority = {
        "AGENT_EXTERNAL_EFFECT_AUTHORITY": guard.external_authority_for_command(issue)
    }

    assert _command_reason(curl, curl_authority) is None
    assert _command_reason(issue, issue_authority) is None
    assert _command_reason(issue, curl_authority) is not None
    assert _command_reason(issue, {"AGENT_EXTERNAL_EFFECT_AUTHORITY": "1"}) is not None
    assert _command_reason("git push origin main --force", issue_authority) is not None
    assert _command_reason("gh auth token", issue_authority) is not None
    assert _command_reason("gh auth login --with-token", issue_authority) is not None
    assert _command_reason("gh repo delete acme/demo --yes", issue_authority) is not None
    assert _command_reason("gh ssh-key add key.pub --title test", issue_authority) is not None
    assert _command_reason("npm publish", issue_authority) is not None
    assert _command_reason("Set-Content .agent-policy.json '{}'", issue_authority) is not None


def test_policy_can_make_external_effects_non_authorizable() -> None:
    guard = _guard()
    policy = _policy()
    external_effects = policy["external_effects"]
    assert isinstance(external_effects, dict)
    external_effects["blocked_hosts"] = [
        "api.openai.com",
        "api.anthropic.com",
        "huggingface.co",
        "hf.co",
    ]
    external_effects.update(
        {
            "authority_blocked_hosts": [
                "api.openai.com",
                "huggingface.co",
                "hf.co",
            ],
            "authority_blocked_clients": [
                "openai",
                "huggingface_hub",
                "transformers",
                "ollama",
            ],
            "authority_blocked_tool_patterns": [
                "github",
                "openai",
                "huggingface",
                "ollama",
            ],
            "authority_blocked_tool_allow_patterns": [
                "ollama__generate",
            ],
        }
    )
    policy["blocked_commands"] = [
        {
            "pattern": r"(?:^|\s)poly-data\s+push-hf\b",
            "reason": "hosted dataset publication is forbidden unattended",
            "non_authorizable": True,
        }
    ]

    for command in (
        "curl https://api.openai.com/v1/models",
        "curl https://huggingface.co/org/model/resolve/main/config.json",
        "openai api responses.create",
        "poly-data push-hf dataset",
        "python -c \"from huggingface_hub import snapshot_download; snapshot_download('org/model')\"",
        "python -c \"from transformers import AutoModel; AutoModel.from_pretrained('org/model')\"",
        "python -c \"import ollama; ollama.pull('gemma3:4b')\"",
        "python -c \"import ollama; client=ollama.Client(); client.pull('gemma3:4b')\"",
        "python -c \"from ollama import Client; client=Client(); client.push('gemma3:4b')\"",
    ):
        authority = {
            "AGENT_EXTERNAL_EFFECT_AUTHORITY": guard.external_authority_for_command(command)
        }
        assert guard.evaluate(
            {"tool_name": "Bash", "tool_input": {"command": command}},
            policy,
            authority,
        ) is not None

    mutation = {
        "tool_name": "mcp__codex_apps__github_create_pull_request",
        "tool_input": {"repository_full_name": "acme/demo", "title": "test"},
    }
    authority = {
        "AGENT_EXTERNAL_EFFECT_AUTHORITY": guard.external_authority_for_payload(mutation)
    }
    assert guard.evaluate(mutation, policy, authority) is not None

    provider_calls = [
        {
            "tool_name": "mcp__openai__responses_create",
            "tool_input": {"model": "gpt-example", "input": "test"},
        },
        {
            "tool_name": "mcp__openai__list_models",
            "tool_input": {},
        },
        {
            "tool_name": "mcp__huggingface__download_model",
            "tool_input": {"repo_id": "org/model"},
        },
        {
            "tool_name": "mcp__huggingface__search_models",
            "tool_input": {"query": "example"},
        },
        {
            "tool_name": "mcp__ollama__pull",
            "tool_input": {"model": "gemma3:4b"},
        },
        {
            "tool_name": "mcp__ollama__generate_and_pull",
            "tool_input": {"model": "gemma3:4b"},
        },
    ]
    for provider_call in provider_calls:
        provider_authority = {
            "AGENT_EXTERNAL_EFFECT_AUTHORITY": guard.external_authority_for_payload(
                provider_call
            )
        }
        assert guard.evaluate(provider_call, policy, {}) is not None
        assert guard.evaluate(provider_call, policy, provider_authority) is not None

    local_generation = {
        "tool_name": "mcp__ollama__generate",
        "tool_input": {"model": "gemma3:4b", "prompt": "test"},
    }
    assert guard.evaluate(local_generation, policy, {}) is None


def test_configured_non_authorizable_examples_reject_exact_authority() -> None:
    guard = _guard()
    policy = _policy()
    external_effects = policy["external_effects"]
    assert isinstance(external_effects, dict)

    for command in external_effects.get("non_authorizable_examples", []):
        assert isinstance(command, str)
        authority = {
            "AGENT_EXTERNAL_EFFECT_AUTHORITY": guard.external_authority_for_command(command)
        }
        assert guard.evaluate(
            {"tool_name": "Bash", "tool_input": {"command": command}},
            policy,
            authority,
        ) is not None


def test_repository_policy_covers_tracked_secret_variants() -> None:
    policy = _policy()
    patterns = policy["forbidden_tracked"]
    exceptions = policy["forbidden_tracked_exceptions"]
    assert isinstance(patterns, list)
    assert isinstance(exceptions, list)

    for relative in (
        ".env.production",
        "config/.env.production",
        "credentials",
        "config/credentials",
        "credentials.yaml",
        "config/credentials.yaml",
    ):
        assert any(
            isinstance(pattern, str)
            and fnmatch.fnmatchcase(relative.casefold(), pattern.casefold())
            for pattern in patterns
        ), relative
    assert any(
        isinstance(pattern, str)
        and fnmatch.fnmatchcase(".env.example", pattern.casefold())
        for pattern in exceptions
    )


def test_external_app_mutations_require_narrow_authority() -> None:
    guard = _guard()
    mutation = {
        "tool_name": "mcp__codex_apps__github_create_pull_request",
        "tool_input": {"repository_full_name": "acme/demo", "title": "test"},
    }
    force_update = {
        "tool_name": "mcp__codex_apps__github_update_ref",
        "tool_input": {"repository_full_name": "acme/demo", "force": True},
    }
    secret_read = {
        "tool_name": "mcp__codex_apps__sites_get_environment_variables",
        "tool_input": {"site_id": "site"},
    }
    read = {
        "tool_name": "mcp__codex_apps__github_get_pull_request",
        "tool_input": {"repository_full_name": "acme/demo", "pull_number": 1},
    }
    other_mutations = [
        "mcp__codex_apps__github_convert_pull_request_to_draft",
        "mcp__codex_apps__github_label_pr",
        "mcp__codex_apps__github_lock_issue_conversation",
        "mcp__codex_apps__github_request_pull_request_reviewers",
        "mcp__codex_apps__linear_update_issue",
        "mcp__codex_apps__linear_delete_status_update",
        "mcp__codex_apps__linear_save_status_update",
        "mcp__codex_apps__codex_document_control_execute_document_command",
    ]

    mutation_authority = {
        "AGENT_EXTERNAL_EFFECT_AUTHORITY": guard.external_authority_for_payload(mutation)
    }
    assert guard.evaluate(mutation, _policy(), {}) is not None
    assert guard.evaluate(mutation, _policy(), mutation_authority) is None
    assert guard.evaluate(force_update, _policy(), mutation_authority) is not None
    assert guard.evaluate(secret_read, _policy(), mutation_authority) is not None
    assert guard.evaluate(read, _policy(), {}) is None
    for tool_name in other_mutations:
        assert guard.evaluate({"tool_name": tool_name, "tool_input": {}}, _policy(), {}) is not None


def test_provider_mcp_mutations_require_exact_authority_when_authorizable() -> None:
    guard = _guard()
    policy = _policy()
    external_effects = policy["external_effects"]
    assert isinstance(external_effects, dict)
    external_effects["authority_blocked_tool_patterns"] = []

    for payload in (
        {
            "tool_name": "mcp__openai__responses_create",
            "tool_input": {"model": "gpt-example", "input": "test"},
        },
        {
            "tool_name": "mcp__anthropic__messages_create",
            "tool_input": {"model": "claude-example", "messages": []},
        },
        {
            "tool_name": "mcp__huggingface__download_model",
            "tool_input": {"repo_id": "org/model"},
        },
        {
            "tool_name": "mcp__ollama__pull",
            "tool_input": {"model": "gemma3:4b"},
        },
    ):
        authority = {
            "AGENT_EXTERNAL_EFFECT_AUTHORITY": guard.external_authority_for_payload(
                payload
            )
        }
        assert guard.evaluate(payload, policy, {}) is not None
        assert guard.evaluate(payload, policy, authority) is None


def test_all_tool_matchers_reach_the_guard() -> None:
    claude = json.loads((REPO_ROOT / ".claude/settings.json").read_text(encoding="utf-8"))
    codex = json.loads((REPO_ROOT / ".codex/hooks.json").read_text(encoding="utf-8"))

    assert all(item["matcher"] == "*" for item in claude["hooks"]["PreToolUse"])
    assert all(item["matcher"] == "*" for item in codex["hooks"]["PreToolUse"])


def test_payload_authority_helper_accepts_utf8_bom_from_powershell() -> None:
    payload = b'\xef\xbb\xbf{"tool_name":"mcp__github__create_issue","tool_input":{"title":"x"}}'

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "agent_guard.py"),
            "--print-payload-authority",
        ],
        input=payload,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stdout.decode().strip().startswith("payload-sha256:")
